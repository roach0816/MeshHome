"""Status, device information, radio connection settings and maintenance controls."""

import asyncio
import time

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, is_https, require_auth, require_session
from app.config import APP_VERSION, get_settings
from app.db import get_db
from app.models import AuditEvent, Channel, CollectionGap, Contact, Message, Radio
from app.radio.base import NotSupported, RadioError
from app.radio.supervisor import supervisor
from app.realtime import hub
from app.services import app_settings, radio_hat

router = APIRouter(prefix="/api", tags=["radio"])


class RadioSettingsIn(BaseModel):
    mode: app_settings.RadioMode
    host: str = ""
    port: int = Field(default=5000, ge=1, le=65535)
    sim_interval_seconds: int = Field(default=60, ge=0, le=3600)


class TestConnectionIn(BaseModel):
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=5000, ge=1, le=65535)


@router.get("/status")
async def get_status(ctx: AuthContext = Depends(require_auth), db: AsyncSession = Depends(get_db)):
    gaps = (
        await db.execute(select(CollectionGap).order_by(CollectionGap.started_at.desc()).limit(10))
    ).scalars()
    msg_count = (await db.execute(select(func.count()).select_from(Message))).scalar_one()
    cfg = await app_settings.get_radio_config(db)
    return {
        "app": {"version": APP_VERSION, "radio_enabled_env": get_settings().radio_enabled},
        "database": {"ok": True, "messages": msg_count},
        "radio": supervisor.snapshot(),
        "radio_config": {"mode": cfg.mode, "host": cfg.host, "port": cfg.port, "paused": cfg.paused},
        "realtime_clients": hub.client_count,
        "gaps": [
            {
                "started_at": g.started_at,
                "ended_at": g.ended_at,
                "reason": g.reason,
                "open": g.ended_at is None,
            }
            for g in gaps
        ],
        "server_time": time.time(),
    }


@router.get("/device")
async def get_device(ctx: AuthContext = Depends(require_auth), db: AsyncSession = Depends(get_db)):
    radio = supervisor.radio
    if radio is None:
        radio = (
            await db.execute(select(Radio).order_by(Radio.last_connected_at.desc().nulls_last()).limit(1))
        ).scalar()
    if radio is None:
        return {"radio": None, "channels": [], "contacts": 0}
    channels = (
        await db.execute(
            select(Channel).where(Channel.radio_id == radio.id).order_by(Channel.slot, Channel.generation)
        )
    ).scalars()
    contacts = (await db.execute(select(func.count()).where(Contact.radio_id == radio.id))).scalar_one()
    return {
        "radio": {
            "id": str(radio.id),
            "name": radio.name,
            "public_key": radio.public_key,
            "is_simulated": radio.is_simulated,
            "device_info": radio.device_info,
            "rf": radio.self_info.get("radio", {}),
            "last_connected_at": radio.last_connected_at,
            "live": supervisor.connected,
        },
        # Channel secrets are never stored or returned; only slot, name and archive generation.
        "channels": [
            {"slot": c.slot, "name": c.name, "generation": c.generation, "active": c.active} for c in channels
        ],
        "contacts": contacts,
    }


@router.get("/settings/radio", response_model=app_settings.RadioConfig)
async def get_radio_settings(ctx: AuthContext = Depends(require_auth), db: AsyncSession = Depends(get_db)):
    return await app_settings.get_radio_config(db)


@router.put("/settings/radio", response_model=app_settings.RadioConfig)
async def put_radio_settings(
    body: RadioSettingsIn, ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)
):
    current = await app_settings.get_radio_config(db)
    cfg = app_settings.RadioConfig(
        mode=body.mode,
        host=body.host,
        port=body.port,
        sim_interval_seconds=body.sim_interval_seconds,
        paused=current.paused,
    )
    if cfg.mode == "tcp" and not cfg.host:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Enter the radio's IP address or hostname")
    if cfg.mode == "hat" and not radio_hat.native():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "The radio HAT is available on Raspberry Pi installs made with install.sh",
        )
    await app_settings.put_radio_config(db, cfg)
    db.add(AuditEvent(kind="radio.settings_changed", detail={"mode": cfg.mode}))
    await db.commit()
    supervisor.reload()
    return cfg


async def _set_paused(db: AsyncSession, paused: bool) -> app_settings.RadioConfig:
    cfg = await app_settings.get_radio_config(db)
    cfg.paused = paused
    await app_settings.put_radio_config(db, cfg)
    db.add(AuditEvent(kind="radio.paused" if paused else "radio.resumed", detail={}))
    await db.commit()
    supervisor.reload()
    return cfg


@router.post("/radio/pause", response_model=app_settings.RadioConfig)
async def pause_radio(ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)):
    return await _set_paused(db, True)


@router.post("/radio/resume", response_model=app_settings.RadioConfig)
async def resume_radio(ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)):
    return await _set_paused(db, False)


@router.post("/radio/test-connection")
async def test_connection(body: TestConnectionIn, ctx: AuthContext = Depends(require_session)):
    """Plain TCP reachability check. Does not perform the companion handshake (the supervisor owns that)."""
    started = time.monotonic()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(body.host.strip(), body.port), timeout=4)
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=2)
        except Exception:  # noqa: BLE001
            pass
    except TimeoutError:
        return {"reachable": False, "detail": "Timed out after 4 seconds"}
    except OSError as exc:
        return {"reachable": False, "detail": exc.strerror or type(exc).__name__}
    return {"reachable": True, "detail": f"TCP port open ({(time.monotonic() - started) * 1000:.0f} ms)"}


@router.post("/radio/simulate-incoming")
async def simulate_incoming(ctx: AuthContext = Depends(require_session)):
    if not supervisor.simulate_incoming():
        raise HTTPException(status.HTTP_409_CONFLICT, "Only available while the simulated radio is connected")
    return {"ok": True}


@router.delete("/simulated-data")
async def delete_simulated_data(
    ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)
):
    cfg = await app_settings.get_radio_config(db)
    if cfg.mode == "simulated":
        raise HTTPException(status.HTTP_409_CONFLICT, "Switch away from the simulated radio first")
    res = await db.execute(delete(Radio).where(Radio.is_simulated.is_(True)))
    db.add(AuditEvent(kind="simulated_data.deleted", detail={}))
    await db.commit()
    hub.publish("conversations-updated")
    hub.publish("contacts-updated")
    return {"deleted_radios": res.rowcount or 0}


@router.get("/settings/notifications", response_model=app_settings.NotificationConfig)
async def get_notifications(ctx: AuthContext = Depends(require_auth), db: AsyncSession = Depends(get_db)):
    return await app_settings.get_notification_config(db)


@router.put("/settings/notifications", response_model=app_settings.NotificationConfig)
async def put_notifications(
    body: app_settings.NotificationConfig,
    ctx: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    await app_settings.put_notification_config(db, body)
    await db.commit()
    hub.publish("settings-updated", key="notifications")
    return body


class AppInstallStatus(app_settings.AppInstallConfig):
    # Whether this request came over HTTPS (directly or through a proxy that says so). Installing
    # needs it, so the setting can only be turned on from an HTTPS page.
    https: bool


@router.get("/settings/app-install", response_model=AppInstallStatus)
async def get_app_install(
    request: Request, ctx: AuthContext = Depends(require_auth), db: AsyncSession = Depends(get_db)
):
    cfg = await app_settings.get_app_install_config(db)
    return AppInstallStatus(**cfg.model_dump(), https=is_https(request))


@router.put("/settings/app-install", response_model=AppInstallStatus)
async def put_app_install(
    body: app_settings.AppInstallConfig,
    request: Request,
    ctx: AuthContext = Depends(require_session),
    db: AsyncSession = Depends(get_db),
):
    https = is_https(request)
    if body.enabled and not https:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Installing as an app needs HTTPS. Turn on HTTPS, open MeshHome at its https:// address, then try again.",
        )
    await app_settings.put_app_install_config(db, body)
    db.add(AuditEvent(kind="settings.app_install", detail={"enabled": body.enabled}))
    await db.commit()
    hub.publish("settings-updated", key="app_install")
    return AppInstallStatus(**body.model_dump(), https=https)


@router.get("/settings/bot", response_model=app_settings.BotConfig)
async def get_bot(ctx: AuthContext = Depends(require_auth), db: AsyncSession = Depends(get_db)):
    return await app_settings.get_bot_config(db)


@router.put("/settings/bot", response_model=app_settings.BotConfig)
async def put_bot(
    body: app_settings.BotConfig,
    ctx: AuthContext = Depends(require_session),  # who may command the node: owner only
    db: AsyncSession = Depends(get_db),
):
    await app_settings.put_bot_config(db, body)
    db.add(AuditEvent(kind="settings.bot", detail=body.model_dump()))
    await db.commit()
    hub.publish("settings-updated", key="bot")
    return body


class TraceIn(BaseModel):
    # Repeaters in order from this radio, as public keys or key prefixes (hex). The trace goes out
    # along them and comes back the same way.
    hops: list[str] = Field(min_length=1, max_length=8)


class TraceHop(BaseModel):
    hash: str
    snr: float | None
    names: list[str]  # contacts whose key starts with the hash


class TraceOut(BaseModel):
    hops: list[TraceHop]  # each repeater on the way out and back, with the SNR it heard
    final_snr: float | None  # the last repeater, as heard back by this radio
    elapsed_ms: int


@router.post("/radio/trace", response_model=TraceOut)
async def trace_route(
    body: TraceIn, ctx: AuthContext = Depends(require_auth), db: AsyncSession = Depends(get_db)
):
    """Send a trace probe along a route of repeaters and back: the signal each one heard, and how
    long the round trip took. Nothing is stored; the probe is a single small packet each way."""
    hops = [h.strip().lower() for h in body.hops]
    if any(not h or len(h) % 2 or any(c not in "0123456789abcdef" for c in h) for h in hops):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Each hop must be a hex key or key prefix")
    # The longest hash every hop allows (1, 2 or 4 bytes): longer is less likely to match a second repeater.
    size = max(s for s in (1, 2, 4) if all(len(h) >= s * 2 for h in hops))
    if size == 1 and min(len(h) for h in hops) < 2:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Each hop must be at least 2 hex characters"
        )
    try:
        result = await supervisor.trace([bytes.fromhex(h[: size * 2]) for h in hops])
    except NotSupported as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except TimeoutError:
        raise HTTPException(
            status.HTTP_504_GATEWAY_TIMEOUT,
            "The trace didn't come back. A repeater on the route may be out of range of the next, or not relaying.",
        ) from None
    except RadioError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Could not send the trace: {exc}") from exc
    radio = supervisor.radio
    contacts = (
        (
            await db.execute(
                select(Contact.public_key, Contact.alias, Contact.name).where(Contact.radio_id == radio.id)
            )
        ).all()
        if radio
        else []
    )
    path = result.get("path") or []
    out = [
        TraceHop(
            hash=p["hash"],
            snr=p.get("snr"),
            names=[a or n for k, a, n in contacts if k.startswith(p["hash"])][:3],
        )
        for p in path
        if "hash" in p
    ]
    final = next((p.get("snr") for p in reversed(path) if "hash" not in p), None)
    return TraceOut(hops=out, final_snr=final, elapsed_ms=result["elapsed_ms"])
