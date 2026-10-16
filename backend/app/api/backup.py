"""Backup and restore (owner only; restore is also offered by the first-run setup wizard).

Native installs: the parts of the system the app cannot read (HTTPS certificate and key, DNS
provider credentials, the radio HAT's data) are exported and restored by the installer's root
helper, through the same request file as network settings (see services/system_config.py).

A TCP radio connected when a backup is made is captured too (services/radio_backup.py). Restoring
a backup never writes to a radio; /api/radio-restore puts a radio snapshot back, when the owner
chooses to.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import SetupToken, setup_failures
from app.api.deps import (
    AuthContext,
    client_ip,
    load_session,
    require_csrf,
    require_requested_with,
    require_session,
    session_cookie,
)
from app.config import get_settings
from app.db import get_db
from app.models import AuditEvent
from app.radio.base import RadioError
from app.radio.supervisor import supervisor
from app.security import constant_time_equals
from app.services import app_settings, backup, backup_crypto, radio_backup, remote_admin, system_config

log = logging.getLogger(__name__)
router = APIRouter(tags=["backup"])

MAX_UPLOAD = 2 * 1024**3
EXPORT_FILE = "system-export.tar.gz"
RESTORE_FILE = "system-restore.tar.gz"
HELPER_WAIT = 180.0
UPLOAD_ID = re.compile(r"^[0-9a-f]{32}$")
_busy = asyncio.Lock()


class PassphraseIn(BaseModel):
    passphrase: str = Field(min_length=backup_crypto.MIN_PASSPHRASE, max_length=1024)


def _native() -> bool:
    s = get_settings()
    return s.install_kind == "native" and bool(s.state_dir)


def _bad(exc: Exception) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))


async def restore_auth(request: Request, db: AsyncSession = Depends(get_db)) -> str:
    """The owner (signed in), or, before setup is complete, whoever holds the setup token."""
    try:
        return await _restore_auth(request, db)
    finally:
        # End this check's transaction now: its read locks would block the restore's TRUNCATE
        # for the rest of the request.
        await db.rollback()


async def _restore_auth(request: Request, db: AsyncSession) -> str:
    if await app_settings.setup_complete(db):
        # The signed-in owner's browser. API keys can't restore.
        ctx = await load_session(db, session_cookie(request.cookies))
        if ctx is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in")
        require_csrf(request, ctx.session)
        return "owner"
    require_requested_with(request)
    ip = client_ip(request)
    if setup_failures.blocked(ip):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts; wait a minute")
    expected = SetupToken.value
    given = request.headers.get("x-setup-token", "").strip()
    if expected is None or not given or not constant_time_equals(given, expected):
        setup_failures.hit(ip)
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Setup token is incorrect — copy it from the server log"
        )
    return "setup"


# ---- native: the root helper's part ----------------------------------------------------------


async def _helper(action: str, extra: dict | None = None) -> dict:
    """Ask the root helper to do something and wait until it reports done or failed."""
    if system_config.in_progress():
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Another system change is in progress; try again shortly"
        )
    started = time.time()
    system_config.write_request({"action": action, **(extra or {})})
    while time.time() - started < HELPER_WAIT:
        await asyncio.sleep(1)
        st = system_config.status() or {}
        if float(st.get("updated_at") or 0) >= started and st.get("state") in ("done", "failed"):
            return st
    return {"state": "failed", "message": "The installer's helper did not answer in time."}


async def _export_system() -> tuple[Path | None, str | None]:
    """System files packed by the root helper, or (None, reason) if they could not be."""
    path = Path(get_settings().state_dir) / EXPORT_FILE
    path.unlink(missing_ok=True)
    st = await _helper("system-export")
    if st.get("state") != "done" or not path.is_file():
        return None, st.get("message") or "The installer's helper could not export the system settings."
    return path, None


async def _capture_radio(db: AsyncSession) -> tuple[radio_backup.Snapshot | None, str | None]:
    """A snapshot of the TCP radio for the backup, or (None, why not). The radio HAT's data is in
    the system archive instead, and a simulated radio is only captured for testing."""
    cfg = await app_settings.get_radio_config(db)
    await db.rollback()  # don't hold a transaction open while the radio is read
    if cfg.mode not in ("tcp", "simulated"):
        return None, None
    if not supervisor.connected:
        return (
            None,
            "The radio wasn't connected, so its identity, settings and channels aren't in this backup.",
        )
    try:
        snap = await radio_backup.capture()
    except (RadioError, TimeoutError) as exc:
        log.info("backup: radio not captured: %s", exc)
        return (
            None,
            "The radio couldn't be read, so its identity, settings and channels aren't in this backup.",
        )
    return snap, radio_backup.KEY_NOTES.get(snap.key)


# ---- backups -----------------------------------------------------------------------------------


@router.get("/api/backups")
async def list_backups(ctx: AuthContext = Depends(require_session)):
    backup.prune_temporary()
    return {"persistent": backup.persistent(), "native": _native(), "backups": backup.list_backups()}


@router.post("/api/backups")
async def create_backup(
    body: PassphraseIn, ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)
):
    if _busy.locked():
        raise HTTPException(status.HTTP_409_CONFLICT, "A backup or restore is already running")
    async with _busy:
        system_path, system_note = (None, None)
        if _native():
            system_path, system_note = await _export_system()
        radio, radio_note = await _capture_radio(db)
        try:
            path = await backup.create(body.passphrase, system_archive=system_path, radio=radio)
        except backup_crypto.BackupError as exc:
            raise _bad(exc) from exc
        finally:
            if system_path is not None:
                system_path.unlink(missing_ok=True)
    db.add(
        AuditEvent(
            kind="backup.created",
            detail={"system": system_path is not None, "radio": None if radio is None else radio.key},
        )
    )
    await db.commit()
    return {
        "name": path.name,
        "size": path.stat().st_size,
        "system_included": system_path is not None,
        "warning": system_note,
        # The radio snapshot it holds (never the key itself), and why parts are missing.
        "radio": None if radio is None else radio_backup.summary(radio),
        "radio_note": radio_note,
    }


@router.get("/api/backups/{name}")
async def download_backup(name: str, ctx: AuthContext = Depends(require_session)):
    try:
        path = backup.backup_path(name)
    except FileNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Backup not found") from None
    return FileResponse(
        path, media_type="application/octet-stream", filename=name, headers={"Cache-Control": "no-store"}
    )


@router.delete("/api/backups/{name}", status_code=204)
async def delete_backup(name: str, ctx: AuthContext = Depends(require_session)):
    try:
        backup.backup_path(name).unlink()
    except FileNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Backup not found") from None


# ---- restore -----------------------------------------------------------------------------------


def _upload_path(upload_id: str) -> Path:
    if not UPLOAD_ID.match(upload_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Upload not found; upload the backup again")
    p = backup.staging_dir() / f"{upload_id}.mhb"
    if not p.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Upload not found; upload the backup again")
    return p


@router.post("/api/restore/upload")
async def upload(request: Request, who: str = Depends(restore_auth)):
    """The backup file as the raw request body (application/octet-stream)."""
    backup.prune_temporary()
    upload_id = uuid.uuid4().hex
    target = backup.staging_dir() / f"{upload_id}.mhb"
    size = 0
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "The file is too large")
                f.write(chunk)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    if size == 0:
        target.unlink(missing_ok=True)
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "The file is empty")
    return {"upload_id": upload_id, "size": size}


@router.post("/api/restore/{upload_id}/inspect")
async def inspect_upload(upload_id: str, body: PassphraseIn, who: str = Depends(restore_auth)):
    path = _upload_path(upload_id)
    try:
        opened = await asyncio.to_thread(backup.open_backup, path, body.passphrase)
    except backup_crypto.BackupError as exc:
        raise _bad(exc) from exc
    try:
        return backup.inspect(opened)
    finally:
        opened.close()


@router.post("/api/restore/{upload_id}/apply")
async def apply_upload(upload_id: str, body: PassphraseIn, who: str = Depends(restore_auth)):
    path = _upload_path(upload_id)
    if _busy.locked():
        raise HTTPException(status.HTTP_409_CONFLICT, "A backup or restore is already running")
    async with _busy:
        try:
            opened = await asyncio.to_thread(backup.open_backup, path, body.passphrase)
        except backup_crypto.BackupError as exc:
            raise _bad(exc) from exc
        try:
            summary = backup.inspect(opened)
            if not summary["can_restore"]:
                raise HTTPException(status.HTTP_409_CONFLICT, " ".join(summary["errors"]))
            # Native: keep a copy of what is being replaced (encrypted with the same passphrase).
            safety = None
            if who == "owner" and backup.persistent():
                try:
                    safety = (await backup.create(body.passphrase, label="before-restore")).name
                except Exception as exc:  # noqa: BLE001
                    log.warning("could not take a safety backup before restoring: %s", exc)
            await supervisor.stop()
            try:
                await backup.restore_database(opened)
            finally:
                supervisor.__init__()
                remote_admin._sessions.clear()
                supervisor.start()
            SetupToken.clear()
            system_state = "none"
            if summary["system_restore"]:
                archive = opened.members["system.tar.gz"]
                target = Path(get_settings().state_dir) / RESTORE_FILE
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(archive.read_bytes())
                digest = hashlib.sha256(target.read_bytes()).hexdigest()
                system_config.write_request({"action": "system-restore", "sha256": digest})
                system_state = "applying"
        finally:
            opened.close()
        path.unlink(missing_ok=True)
    log.info("restored a backup made %s (from %s)", summary.get("created_at"), summary.get("install_kind"))
    return {"restored": True, "system": system_state, "safety_backup": safety, "summary": summary}


# ---- putting a radio snapshot back -------------------------------------------------------------
# Uses an uploaded backup (/api/restore/upload), but changes only the connected radio.


def _snapshot(path: Path, passphrase: str) -> radio_backup.Snapshot:
    opened = backup.open_backup(path, passphrase)
    try:
        snap = backup.radio_snapshot(opened)
    finally:
        opened.close()
    if snap is None:
        raise backup_crypto.BackupError(
            "This backup has no radio snapshot: it was made without a TCP radio connected, or by an older MeshHome."
        )
    return snap


async def _connected_radio() -> dict | None:
    if not supervisor.connected:
        return None
    try:
        snap = await supervisor.call("get_device_snapshot")
    except (RadioError, TimeoutError):
        return None
    return {"name": snap.name, "public_key": snap.public_key.lower(), "simulated": snap.is_simulated}


@router.post("/api/radio-restore/{upload_id}/inspect")
async def inspect_radio(
    upload_id: str,
    body: PassphraseIn,
    ctx: AuthContext = Depends(require_session),
    db: AsyncSession = Depends(get_db),
):
    """The radio snapshot in an uploaded backup, and the radio it would be written to."""
    path = _upload_path(upload_id)
    try:
        snap = await asyncio.to_thread(_snapshot, path, body.passphrase)
    except backup_crypto.BackupError as exc:
        raise _bad(exc) from exc
    mode = (await app_settings.get_radio_config(db)).mode
    await db.rollback()
    radio = None if mode == "hat" else await _connected_radio()
    problem = None
    if mode == "hat":
        problem = "The radio HAT's identity is restored with the whole backup, not here."
    elif radio is None:
        problem = "Connect the radio first (Settings → Radio connection)."
    return {
        "snapshot": radio_backup.summary(snap),
        "radio": radio,
        "same_identity": bool(radio and radio["public_key"] == snap.public_key),
        "can_restore": problem is None,
        "problem": problem,
    }


class RadioRestoreIn(PassphraseIn):
    parts: list[str] = Field(min_length=1, max_length=len(radio_backup.PARTS))


@router.post("/api/radio-restore/{upload_id}/apply")
async def apply_radio(
    upload_id: str,
    body: RadioRestoreIn,
    ctx: AuthContext = Depends(require_session),
    db: AsyncSession = Depends(get_db),
):
    """Write the chosen parts of the backup's radio snapshot to the connected radio."""
    if unknown := set(body.parts) - set(radio_backup.PARTS):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"Unknown part: {sorted(unknown)[0]}")
    path = _upload_path(upload_id)
    if (await app_settings.get_radio_config(db)).mode == "hat":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "The radio HAT's identity is restored with the whole backup."
        )
    await db.rollback()
    if _busy.locked():
        raise HTTPException(status.HTTP_409_CONFLICT, "A backup or restore is already running")
    async with _busy:
        try:
            snap = await asyncio.to_thread(_snapshot, path, body.passphrase)
        except backup_crypto.BackupError as exc:
            raise _bad(exc) from exc
        if not supervisor.connected:
            raise HTTPException(status.HTTP_409_CONFLICT, "The radio isn't connected")
        try:
            steps = await radio_backup.restore(snap, set(body.parts))
        except (RadioError, TimeoutError) as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"The radio stopped answering: {exc}") from exc
    db.add(
        AuditEvent(
            kind="radio.restored",
            detail={
                "from_backup_of": snap.public_key[:12],
                "parts": sorted(body.parts),
                "failed": [st["part"] for st in steps if not st["ok"]],
            },
        )
    )
    await db.commit()
    path.unlink(missing_ok=True)
    log.info("restored a radio snapshot (%s) to the connected radio", ", ".join(sorted(body.parts)))
    return {"steps": steps}
