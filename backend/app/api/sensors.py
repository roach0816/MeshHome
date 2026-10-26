"""Sensors attached to this Pi (services/sensors.py): current readings, history and settings."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, require_auth, require_session
from app.db import get_db
from app.models import AuditEvent, SensorReading
from app.realtime import hub
from app.services import app_settings
from app.services.sensors import available, manager

router = APIRouter(prefix="/api", tags=["sensors"])

MAX_POINTS = 300


class SensorsOut(BaseModel):
    # False in a container (Docker, Kubernetes): sensors are only read on a native install, so the
    # statuses stay "off", there's no history and the settings can't be changed.
    available: bool
    # status: off | not_found | ok | error (environment); off | not_found | silent | no_fix | fix |
    # error (gnss). reading: the latest values, with "at" (Unix seconds).
    environment: dict[str, Any]
    gnss: dict[str, Any]
    config: app_settings.SensorsConfig


class HistoryPoint(BaseModel):
    at: datetime
    temperature_c: float | None = None
    humidity: float | None = None
    pressure_hpa: float | None = None
    gas_ohms: float | None = None
    satellites: float | None = None


class HistoryOut(BaseModel):
    hours: int
    points: list[HistoryPoint]


@router.get("/sensors", response_model=SensorsOut)
async def get_sensors(ctx: AuthContext = Depends(require_auth), db: AsyncSession = Depends(get_db)):
    snap = manager.snapshot()
    return SensorsOut(**snap, config=await app_settings.get_sensors_config(db))


@router.get("/sensors/history", response_model=HistoryOut)
async def sensor_history(
    hours: int = Query(default=24, ge=1, le=720),
    ctx: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """Stored readings (one every 5 minutes), averaged into at most 300 points."""
    since = datetime.now(UTC) - timedelta(hours=hours)
    rows = (
        (await db.execute(select(SensorReading).where(SensorReading.at >= since).order_by(SensorReading.at)))
        .scalars()
        .all()
    )
    fields = ("temperature_c", "humidity", "pressure_hpa", "gas_ohms", "satellites")
    step = max(1, -(-len(rows) // MAX_POINTS))
    points = []
    for i in range(0, len(rows), step):
        chunk = rows[i : i + step]
        p: dict[str, Any] = {"at": chunk[len(chunk) // 2].at}
        for f in fields:
            vals = [getattr(r, f) for r in chunk if getattr(r, f) is not None]
            p[f] = round(sum(vals) / len(vals), 2) if vals else None
        points.append(HistoryPoint(**p))
    return HistoryOut(hours=hours, points=points)


@router.put("/settings/sensors", response_model=app_settings.SensorsConfig)
async def put_sensors(
    body: app_settings.SensorsConfig,
    ctx: AuthContext = Depends(require_session),
    db: AsyncSession = Depends(get_db),
):
    if not available():
        raise HTTPException(409, "Sensors are only read on a Raspberry Pi / Debian install")
    await app_settings.put_sensors_config(db, body)
    db.add(AuditEvent(kind="settings.sensors", detail=body.model_dump()))
    await db.commit()
    manager.wake()
    hub.publish("settings-updated")
    return body
