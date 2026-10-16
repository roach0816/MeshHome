"""Software version, update availability and (native installs) in-place upgrades."""

import json
import re
import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, require_auth, require_session
from app.config import APP_VERSION, get_settings
from app.db import get_db
from app.models import AuditEvent
from app.security import RateLimiter
from app.services import radio_hat, system_config, updates

router = APIRouter(prefix="/api/system", tags=["system"])

_manual_checks = RateLimiter(limit=3, window_seconds=60)
ACTIVE_STATES = {"queued", "downloading", "installing", "migrating", "restarting"}
STALE_AFTER = 30 * 60  # an "active" status older than this is assumed dead


class UpdateRequest(BaseModel):
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")


def _in_progress(st: dict | None) -> bool:
    return bool(
        st
        and st.get("state") in ACTIVE_STATES
        and time.time() - float(st.get("updated_at") or 0) < STALE_AFTER
    )


@router.get("/update")
async def update_info(refresh: bool = Query(default=False), ctx: AuthContext = Depends(require_auth)):
    force = False
    if refresh:
        key = str(ctx.user.id)
        if _manual_checks.blocked(key):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Checked recently; try again in a minute")
        _manual_checks.hit(key)
        force = True
    await updates.checker.check(force=force)
    return {**updates.checker.summary(), "status": updates.read_status()}


@router.get("/update/status")
async def update_status(ctx: AuthContext = Depends(require_auth)):
    return {"current_version": APP_VERSION, "status": updates.read_status()}


@router.post("/update", status_code=202)
async def request_update(
    body: UpdateRequest, ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)
):
    s = get_settings()
    if s.install_kind != "native" or not s.state_dir:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "In-place upgrades are only available on a native install. Container installs are updated by redeploying.",
        )
    await updates.checker.check()
    info = updates.checker.summary()
    latest = info["latest"]
    if not latest or latest["version"] != body.version or not info["update_available"]:
        raise HTTPException(status.HTTP_409_CONFLICT, "That version is not the latest available release")
    if not latest["has_native_package"]:
        raise HTTPException(status.HTTP_409_CONFLICT, "That release has no installable package yet")
    if _in_progress(updates.read_status()):
        raise HTTPException(status.HTTP_409_CONFLICT, "An update is already in progress")
    try:
        updates.write_request(body.version, ctx.user.username)
    except OSError as exc:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, f"Could not queue the update: {exc}"
        ) from exc
    db.add(AuditEvent(kind="system.update_requested", detail={"from": APP_VERSION, "to": body.version}))
    await db.commit()
    return {"queued": True, "version": body.version}


# ---- network & HTTPS (native installs) ------------------------------------------------------

HOSTNAME_RE = r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"


class NetworkConfigIn(BaseModel):
    # The app runs unprivileged, so it can only listen on ports >= 1024 (nginx handles 80/443).
    app_port: int = Field(ge=1024, le=65535)
    https_enabled: bool
    hostname: str | None = Field(default=None, max_length=253)
    https_port: int = Field(default=443, ge=1, le=65535)
    redirect_http: bool = True
    email: str | None = Field(default=None, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    staging: bool = False
    # 0: lego checks DNS propagation itself; otherwise wait this many seconds (10–600).
    propagation_seconds: int = Field(default=0, ge=0, le=600)
    # One of the providers in dns-providers.json (GET /api/system/network lists them).
    dns_provider: str = Field(default="cloudflare", pattern=r"^[a-z0-9]{2,32}$")
    # Write-only {ENV name: value} for that provider. Omit (or null) to keep the saved values;
    # a blank value keeps that field's saved value.
    credentials: dict[str, str] | None = None
    # Older clients (v0.6.4 to v0.7.3): a Cloudflare API token. Same as dns_provider "cloudflare"
    # with credentials {"CF_DNS_API_TOKEN": token}.
    cf_token: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_\-]{20,200}$")


def _check_credentials(body: NetworkConfigIn) -> None:
    """The provider exists, only its own fields are sent, and required ones are present."""
    provider = system_config.dns_provider(body.dns_provider)
    if provider is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"Unknown DNS provider: {body.dns_provider}"
        )
    fields = {f["env"]: f for f in provider["fields"]}
    given = {k: v.strip() for k, v in (body.credentials or {}).items() if v and v.strip()}
    unknown = sorted(set(body.credentials or {}) - set(fields))
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"{provider['name']} does not use: {', '.join(unknown)}"
        )
    for env, value in given.items():
        f = fields[env]
        if f.get("kind") == "json":
            try:
                json.loads(value)
            except ValueError as exc:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT, f"{f['label']}: this is not valid JSON"
                ) from exc
        elif len(value) > 500 or re.search(r"[\x00-\x1f\x7f']", value):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, f"{f['label']}: unexpected characters or too long"
            )
        elif f.get("choices") and value not in f["choices"]:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"{f['label']}: choose one of {', '.join(f['choices'])}",
            )
    snap = system_config.snapshot() or {}
    if snap.get("credentials_provider") == provider["id"]:
        return  # saved values stay unless replaced
    missing = [
        f["label"]
        for f in provider["fields"]
        if f.get("required") and not f.get("default") and f["env"] not in given
    ]
    if missing:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"{provider['name']}: enter the {', '.join(missing)}"
        )


def _native_or_409() -> None:
    s = get_settings()
    if s.install_kind != "native" or not s.state_dir:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Network and HTTPS settings are managed by your deployment for container installs.",
        )


@router.get("/network")
async def network_info(ctx: AuthContext = Depends(require_auth)):
    s = get_settings()
    native = s.install_kind == "native" and bool(s.state_dir)
    snap = system_config.snapshot() if native else None
    if native and snap is None and not system_config.in_progress():
        # First visit after upgrading from a version without the snapshot: ask the root helper.
        try:
            system_config.write_request({"action": "refresh"})
        except OSError:
            pass
    return {
        "install_kind": s.install_kind,
        "configurable": native,
        "config": snap,
        "certificate": system_config.certificate() if native else None,
        "status": system_config.status() if native else None,
        "in_progress": system_config.in_progress() if native else False,
        "providers": system_config.dns_providers() if native else [],
    }


@router.put("/network", status_code=202)
async def apply_network(
    body: NetworkConfigIn, ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)
):
    _native_or_409()
    if system_config.in_progress():
        raise HTTPException(status.HTTP_409_CONFLICT, "A settings change is already being applied")
    hostname = (body.hostname or "").strip().lower()
    if body.https_enabled:
        if not re.fullmatch(HOSTNAME_RE, hostname):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "Enter a full hostname such as meshcore.example.com"
            )
        if body.app_port == body.https_port:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "The app port and HTTPS port must differ"
            )
        if body.redirect_http and 80 in (body.app_port, body.https_port):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "Port 80 is used for the HTTP→HTTPS redirect"
            )
        if 0 < body.propagation_seconds < 10:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "The DNS wait must be 0 (automatic) or 10–600 seconds"
            )
        if body.cf_token:
            body.dns_provider, body.credentials = "cloudflare", {"CF_DNS_API_TOKEN": body.cf_token}
        _check_credentials(body)
    try:
        system_config.write_request(
            {
                "action": "apply",
                "app_port": body.app_port,
                "https_enabled": body.https_enabled,
                "hostname": hostname,
                "https_port": body.https_port,
                "redirect_http": body.redirect_http,
                "email": body.email or "",
                "staging": body.staging,
                "propagation_seconds": body.propagation_seconds,
                "dns_provider": body.dns_provider,
                "credentials": {k: v for k, v in (body.credentials or {}).items() if v.strip()},
            }
        )
    except OSError as exc:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, f"Could not queue the change: {exc}"
        ) from exc
    db.add(
        AuditEvent(
            kind="system.network_change_requested",
            detail={
                "https_enabled": body.https_enabled,
                "hostname": hostname or None,
                "app_port": body.app_port,
                "https_port": body.https_port,
                "dns_provider": body.dns_provider,
                "credentials_changed": bool(body.credentials),
            },
        )
    )
    await db.commit()
    return {"queued": True}


@router.post("/network/renew", status_code=202)
async def renew_certificate(ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)):
    _native_or_409()
    snap = system_config.snapshot() or {}
    if not snap.get("https_enabled"):
        raise HTTPException(status.HTTP_409_CONFLICT, "HTTPS is not enabled")
    if system_config.in_progress():
        raise HTTPException(status.HTTP_409_CONFLICT, "A settings change is already being applied")
    system_config.write_request({"action": "renew"})
    db.add(AuditEvent(kind="system.certificate_renew_requested", detail={}))
    await db.commit()
    return {"queued": True}


@router.get("/network/status")
async def network_status(ctx: AuthContext = Depends(require_auth)):
    return {
        "status": system_config.status(),
        "config": system_config.snapshot(),
        "certificate": system_config.certificate(),
        "in_progress": system_config.in_progress(),
    }


# ---- radio HAT (native Raspberry Pi installs) ------------------------------------------------


class RadioHatAction(BaseModel):
    # install: download and set up ZephCore; remove: uninstall it (the radio's identity is kept);
    # restart: restart the radio service; reboot: restart the Pi (needed once after SPI is enabled).
    action: Literal["install", "remove", "restart", "reboot"]


@router.get("/radio-hat")
async def radio_hat_info(ctx: AuthContext = Depends(require_auth)):
    data = await radio_hat.info()
    data["request_pending"] = radio_hat.native() and system_config.in_progress()
    data["helper_status"] = system_config.status() if radio_hat.native() else None
    return data


@router.post("/radio-hat", status_code=202)
async def radio_hat_action(
    body: RadioHatAction, ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)
):
    if not radio_hat.native():
        raise HTTPException(
            status.HTTP_409_CONFLICT, "The radio HAT is available on Raspberry Pi installs only"
        )
    if system_config.in_progress():
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Another system change is in progress; try again shortly"
        )
    info = await radio_hat.info()
    if body.action == "install" and not info["available"]:
        raise HTTPException(
            status.HTTP_409_CONFLICT, info["unavailable_reason"] or "The radio HAT is not available"
        )
    if body.action in ("remove", "restart") and not info["service"].get("installed"):
        raise HTTPException(status.HTTP_409_CONFLICT, "The radio HAT software is not set up")
    system_config.write_request({"action": f"hat-{body.action}" if body.action != "reboot" else "reboot"})
    db.add(AuditEvent(kind=f"radio_hat.{body.action}", detail={}))
    await db.commit()
    return {"accepted": True}
