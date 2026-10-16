"""Radio firmware: compare the connected radio with MeshCore's latest companion release.

Read-only: nothing here changes the radio.
"""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, require_auth, require_session
from app.db import get_db
from app.models import Radio
from app.radio.supervisor import supervisor
from app.services import app_settings, firmware, radio_hat

router = APIRouter(prefix="/api/radio/firmware", tags=["radio firmware"])


async def _radio(db: AsyncSession) -> Radio | None:
    if supervisor.radio is not None:
        return supervisor.radio
    return (
        await db.execute(select(Radio).order_by(Radio.last_connected_at.desc().nulls_last()).limit(1))
    ).scalar()


async def _status(db: AsyncSession, force: bool = False) -> dict:
    radio = await _radio(db)
    mode = (await app_settings.get_radio_config(db)).mode
    if radio is None:
        return {"available": False, "reason": "No radio has connected yet."}
    if radio.is_simulated:
        return {"available": False, "reason": "The simulated radio has no firmware to update."}
    if mode == "hat":
        pinned = radio_hat.pinned().get("ZEPHCORE_VERSION")
        return {
            "available": False,
            "reason": "The radio HAT runs ZephCore, the MeshCore firmware for Linux. MeshHome installs it "
            f"and updates it with its own updates{f' (pinned: {pinned})' if pinned else ''}.",
        }
    info = radio.device_info or {}
    latest = await firmware.catalog.latest(force=force)
    out = firmware.status(
        model=info.get("model"), version=info.get("ver") or info.get("firmware"), latest=latest
    )
    return {
        "available": True,
        "checked_at": firmware.catalog.checked_at,
        "error": firmware.catalog.error,
        **out,
    }


@router.get("")
async def get_status(ctx: AuthContext = Depends(require_auth), db: AsyncSession = Depends(get_db)):
    return await _status(db)


@router.post("/check")
async def check_now(ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)):
    return await _status(db, force=True)
