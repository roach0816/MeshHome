"""Tracking (services/tracking.py): tracked devices, their positions, and courses (GPX)."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta
from xml.sax.saxutils import escape, quoteattr

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, require_auth, require_session
from app.db import get_db
from app.models import AuditEvent, Contact, Course, Radio, TrackedDevice, TrackPosition, utcnow
from app.radio.supervisor import supervisor
from app.realtime import hub
from app.services import app_settings, courses, offline_map, tracking

router = APIRouter(prefix="/api", tags=["tracking"])

HEX_COLOR = r"^#[0-9a-fA-F]{6}$"
Interval = int  # one of tracking.INTERVALS


class TrackPoint(BaseModel):
    at: datetime
    lat: float
    lon: float
    alt_m: float | None = None
    source: str  # "telemetry" (it answered a request) | "advert"


class TrackedDeviceOut(BaseModel):
    id: uuid.UUID
    contact_id: uuid.UUID
    public_key: str
    # The contact's name (its alias if it has one); label, when set, is shown instead.
    name: str
    label: str | None
    color: str
    interval_s: int
    polling: bool
    show: bool
    # ok | no_position | no_answer | error | new
    status: str
    status_detail: str | None
    failures: int
    last_poll_at: datetime | None
    last_reply_at: datetime | None
    battery_v: float | None
    # On the radio that's connected now (its other radios' contacts aren't asked).
    current_radio: bool
    is_simulated: bool
    # Seconds until it's asked next (None: not scheduled); true while a request is out.
    next_poll_in: float | None
    asking: bool
    last_position: TrackPoint | None
    # Positions within the trail length (Settings: trail_minutes), oldest first.
    trail: list[TrackPoint]


class BasePosition(BaseModel):
    name: str
    lat: float
    lon: float


class TrackingOut(BaseModel):
    config: app_settings.TrackingConfig
    radio_connected: bool
    intervals: list[int]
    base: BasePosition | None
    devices: list[TrackedDeviceOut]
    tiles: app_settings.MapConfig
    offline: dict


def _point(p: TrackPosition) -> TrackPoint:
    return TrackPoint(at=p.at, lat=p.lat, lon=p.lon, alt_m=p.alt_m, source=p.source)


def _require_enabled(cfg: app_settings.TrackingConfig) -> None:
    if not cfg.enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "Tracking is off. Turn it on in Settings → Tracking.")


async def _device_out(
    db: AsyncSession, d: TrackedDevice, c: Contact, sim: bool, trail_minutes: int
) -> TrackedDeviceOut:
    last = (
        await db.execute(
            select(TrackPosition)
            .where(TrackPosition.device_id == d.id)
            .order_by(TrackPosition.at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    trail: list[TrackPosition] = []
    if last is not None and trail_minutes > 0:
        since = utcnow() - timedelta(minutes=trail_minutes)
        trail = list(
            (
                await db.execute(
                    select(TrackPosition)
                    .where(TrackPosition.device_id == d.id, TrackPosition.at >= since)
                    .order_by(TrackPosition.at)
                    .limit(2000)
                )
            )
            .scalars()
            .all()
        )
    radio = supervisor.radio
    return TrackedDeviceOut(
        id=d.id,
        contact_id=c.id,
        public_key=c.public_key,
        name=c.alias or c.name or c.public_key[:8],
        label=d.label,
        color=d.color,
        interval_s=d.interval_s,
        polling=d.polling,
        show=d.show,
        status=d.status,
        status_detail=d.status_detail,
        failures=d.failures,
        last_poll_at=d.last_poll_at,
        last_reply_at=d.last_reply_at,
        battery_v=d.battery_v,
        current_radio=radio is not None and c.radio_id == radio.id,
        is_simulated=sim,
        next_poll_in=tracking.manager.next_poll_in(d.id) if d.polling else None,
        asking=tracking.manager.polling_now == d.id,
        last_position=_point(last) if last else None,
        trail=[_point(p) for p in trail],
    )


async def _load(db: AsyncSession, device_id: uuid.UUID) -> tuple[TrackedDevice, Contact, bool]:
    row = (
        await db.execute(
            select(TrackedDevice, Contact, Radio.is_simulated)
            .join(Contact, Contact.id == TrackedDevice.contact_id)
            .join(Radio, Radio.id == Contact.radio_id)
            .where(TrackedDevice.id == device_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tracked device not found")
    return row[0], row[1], row[2]


@router.get("/tracking", response_model=TrackingOut)
async def get_tracking(ctx: AuthContext = Depends(require_auth), db: AsyncSession = Depends(get_db)):
    """Tracked devices with their last position and recent trail, for the tracking map."""
    cfg = await app_settings.get_tracking_config(db)
    rows = (
        await db.execute(
            select(TrackedDevice, Contact, Radio.is_simulated)
            .join(Contact, Contact.id == TrackedDevice.contact_id)
            .join(Radio, Radio.id == Contact.radio_id)
            .order_by(TrackedDevice.created_at)
        )
    ).all()
    devices = [await _device_out(db, d, c, sim, cfg.trail_minutes) for d, c, sim in rows]
    base = None
    radio = supervisor.radio
    if radio is not None:
        r = await db.get(Radio, radio.id)
        loc = ((r.self_info if r else None) or {}).get("location") or {}
        if loc.get("lat") is not None and loc.get("lon") is not None:
            base = BasePosition(name=r.name, lat=loc["lat"], lon=loc["lon"])
    tiles = await app_settings.get_map_config(db)
    await db.rollback()
    return TrackingOut(
        config=cfg,
        radio_connected=supervisor.connected,
        intervals=list(tracking.INTERVALS),
        base=base,
        devices=devices,
        tiles=tiles,
        offline=await offline_map.manager.summary(),
    )


@router.get("/settings/tracking", response_model=app_settings.TrackingConfig)
async def get_tracking_settings(ctx: AuthContext = Depends(require_auth), db: AsyncSession = Depends(get_db)):
    return await app_settings.get_tracking_config(db)


@router.put("/settings/tracking", response_model=app_settings.TrackingConfig)
async def put_tracking_settings(
    body: app_settings.TrackingConfig,
    ctx: AuthContext = Depends(require_session),
    db: AsyncSession = Depends(get_db),
):
    await app_settings.put_tracking_config(db, body)
    db.add(AuditEvent(kind="settings.tracking", detail=body.model_dump()))
    await db.commit()
    tracking.manager.wake()
    hub.publish("settings-updated")
    hub.publish("tracking-updated")
    return body


# ---- devices -----------------------------------------------------------------------------


class DeviceIn(BaseModel):
    contact_id: uuid.UUID
    label: str | None = Field(default=None, max_length=64)
    interval_s: Interval = 60


class DevicePatch(BaseModel):
    label: str | None = Field(default=None, max_length=64)
    color: str | None = Field(default=None, pattern=HEX_COLOR)
    interval_s: Interval | None = None
    polling: bool | None = None
    show: bool | None = None


def _check_interval(v: int | None) -> None:
    if v is not None and v not in tracking.INTERVALS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Choose an interval of {', '.join(str(i) for i in tracking.INTERVALS)} seconds",
        )


@router.post("/tracking/devices", response_model=TrackedDeviceOut, status_code=201)
async def add_device(
    body: DeviceIn, ctx: AuthContext = Depends(require_auth), db: AsyncSession = Depends(get_db)
):
    """Track a contact: it's asked for its position every interval_s seconds (60 to 900)."""
    cfg = await app_settings.get_tracking_config(db)
    _require_enabled(cfg)
    _check_interval(body.interval_s)
    contact = await db.get(Contact, body.contact_id)
    if contact is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact not found")
    if contact.kind != 1:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Only companions (people's radios and trackers) can be tracked",
        )
    if (await db.execute(select(TrackedDevice.id).where(TrackedDevice.contact_id == contact.id))).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "That contact is already tracked")
    used = set((await db.execute(select(TrackedDevice.color))).scalars().all())
    color = next(
        (c for c in tracking.COLORS if c not in used), tracking.COLORS[len(used) % len(tracking.COLORS)]
    )
    d = TrackedDevice(
        contact_id=contact.id,
        label=(body.label or "").strip() or None,
        color=color,
        interval_s=body.interval_s,
        polling=True,
        show=True,
        status="new",
        failures=0,
    )
    db.add(d)
    db.add(AuditEvent(kind="tracking.device_added", detail={"contact_id": str(contact.id)}))
    await db.commit()
    tracking.manager.poll_soon(d.id)
    hub.publish("tracking-updated")
    d, c, sim = await _load(db, d.id)
    return await _device_out(db, d, c, sim, cfg.trail_minutes)


@router.patch("/tracking/devices/{device_id}", response_model=TrackedDeviceOut)
async def update_device(
    device_id: uuid.UUID,
    body: DevicePatch,
    ctx: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    _check_interval(body.interval_s)
    d, c, sim = await _load(db, device_id)
    fields = body.model_fields_set
    if "label" in fields:
        d.label = (body.label or "").strip() or None
    if body.color is not None:
        d.color = body.color.lower()
    if body.interval_s is not None and body.interval_s != d.interval_s:
        d.interval_s = body.interval_s
        tracking.manager.poll_soon(d.id)  # then on the new interval
    if body.polling is not None:
        d.polling = body.polling
        if body.polling:
            d.failures = 0
            tracking.manager.poll_soon(d.id)
        else:
            tracking.manager.forget(d.id)
    if body.show is not None:
        d.show = body.show
    await db.commit()
    hub.publish("tracking-updated")
    cfg = await app_settings.get_tracking_config(db)
    d, c, sim = await _load(db, device_id)
    return await _device_out(db, d, c, sim, cfg.trail_minutes)


@router.delete("/tracking/devices/{device_id}", status_code=204)
async def remove_device(
    device_id: uuid.UUID, ctx: AuthContext = Depends(require_auth), db: AsyncSession = Depends(get_db)
):
    """Stop tracking it and delete its positions. The contact stays."""
    d, _, _ = await _load(db, device_id)
    await db.delete(d)
    db.add(AuditEvent(kind="tracking.device_removed", detail={"contact_id": str(d.contact_id)}))
    await db.commit()
    tracking.manager.forget(device_id)
    hub.publish("tracking-updated")
    return Response(status_code=204)


@router.post("/tracking/devices/{device_id}/poll", status_code=202)
async def poll_device(
    device_id: uuid.UUID, ctx: AuthContext = Depends(require_auth), db: AsyncSession = Depends(get_db)
):
    """Ask it for its position now (the answer arrives as a tracking-updated event)."""
    _require_enabled(await app_settings.get_tracking_config(db))
    d, _, _ = await _load(db, device_id)
    if not supervisor.connected:
        raise HTTPException(status.HTTP_409_CONFLICT, "The radio isn't connected")
    tracking.manager.poll_soon(d.id)
    return {"queued": True}


@router.delete("/tracking/devices/{device_id}/positions", status_code=204)
async def clear_positions(
    device_id: uuid.UUID, ctx: AuthContext = Depends(require_auth), db: AsyncSession = Depends(get_db)
):
    """Delete its position history (e.g. before an event), keeping it tracked."""
    d, _, _ = await _load(db, device_id)
    await db.execute(delete(TrackPosition).where(TrackPosition.device_id == d.id))
    await db.commit()
    hub.publish("tracking-updated")
    return Response(status_code=204)


def _filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")[:60] or "track"


@router.get("/tracking/devices/{device_id}/track.gpx")
async def export_track(
    device_id: uuid.UUID,
    hours: int = Query(default=48, ge=1, le=24 * 30),
    ctx: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """Its positions over the last `hours` as a GPX track, e.g. to open in a mapping app."""
    d, c, _ = await _load(db, device_id)
    rows = (
        (
            await db.execute(
                select(TrackPosition)
                .where(TrackPosition.device_id == d.id, TrackPosition.at >= utcnow() - timedelta(hours=hours))
                .order_by(TrackPosition.at)
            )
        )
        .scalars()
        .all()
    )
    name = d.label or c.alias or c.name or "Tracker"
    pts = "".join(
        f'<trkpt lat="{p.lat:.6f}" lon="{p.lon:.6f}">'
        + (f"<ele>{p.alt_m:.1f}</ele>" if p.alt_m is not None else "")
        + f"<time>{p.at.strftime('%Y-%m-%dT%H:%M:%SZ')}</time></trkpt>"
        for p in rows
    )
    gpx = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<gpx version="1.1" creator="MeshHome" xmlns="http://www.topografix.com/GPX/1/1">'
        f"<trk><name>{escape(name)}</name><trkseg>{pts}</trkseg></trk></gpx>\n"
    )
    return Response(
        gpx,
        media_type="application/gpx+xml",
        headers={"Content-Disposition": f"attachment; filename={quoteattr(_filename(name) + '.gpx')}"},
    )


# ---- courses -----------------------------------------------------------------------------


class CourseOut(BaseModel):
    id: uuid.UUID
    name: str
    color: str
    visible: bool
    distance_m: float
    # [[[lat, lon], ...], ...]: one line per track segment or route.
    segments: list[list[list[float]]]
    waypoints: list[dict]
    created_at: datetime


class CoursePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    color: str | None = Field(default=None, pattern=HEX_COLOR)
    visible: bool | None = None


def _course_out(c: Course) -> CourseOut:
    return CourseOut(
        id=c.id,
        name=c.name,
        color=c.color,
        visible=c.visible,
        distance_m=round(c.distance_m, 1),
        segments=c.segments,
        waypoints=c.waypoints,
        created_at=c.created_at,
    )


@router.get("/tracking/courses", response_model=list[CourseOut])
async def list_courses(ctx: AuthContext = Depends(require_auth), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Course).order_by(Course.created_at))).scalars().all()
    return [_course_out(c) for c in rows]


@router.post("/tracking/courses", response_model=CourseOut, status_code=201)
async def upload_course(
    request: Request,
    name: str | None = Query(default=None, max_length=80),
    filename: str | None = Query(default=None, max_length=200),
    ctx: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """A GPX file (tracks, routes and waypoints) as the raw request body, at most 2 MB. Its name is
    `name`, else the one in the file, else `filename` (without .gpx). A colour in the name or
    filename ("Green loop") picks that colour."""
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > courses.MAX_GPX_BYTES:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE, "The file is too large (the limit is 2 MB)"
            )
    try:
        parsed = courses.parse_gpx(bytes(data))
    except courses.GpxError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    stem = re.sub(r"\.gpx$", "", (filename or "").strip(), flags=re.IGNORECASE).strip()
    title = (name or "").strip() or parsed.name or stem or "Course"
    used = set((await db.execute(select(Course.color))).scalars().all())
    c = Course(
        name=title[:80],
        color=courses.color_for(f"{title} {stem}", used),
        visible=True,
        segments=parsed.segments,
        waypoints=parsed.waypoints,
        distance_m=parsed.distance_m,
    )
    db.add(c)
    db.add(AuditEvent(kind="tracking.course_added", detail={"name": c.name}))
    await db.commit()
    await courses.sync_sim_route()
    hub.publish("courses-updated")
    return _course_out(c)


@router.patch("/tracking/courses/{course_id}", response_model=CourseOut)
async def update_course(
    course_id: uuid.UUID,
    body: CoursePatch,
    ctx: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    c = await db.get(Course, course_id)
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Course not found")
    if body.name is not None:
        c.name = body.name.strip() or c.name
    if body.color is not None:
        c.color = body.color.lower()
    if body.visible is not None:
        c.visible = body.visible
    await db.commit()
    await courses.sync_sim_route()
    hub.publish("courses-updated")
    return _course_out(c)


@router.delete("/tracking/courses/{course_id}", status_code=204)
async def delete_course(
    course_id: uuid.UUID, ctx: AuthContext = Depends(require_auth), db: AsyncSession = Depends(get_db)
):
    c = await db.get(Course, course_id)
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Course not found")
    await db.delete(c)
    db.add(AuditEvent(kind="tracking.course_removed", detail={"name": c.name}))
    await db.commit()
    await courses.sync_sim_route()
    hub.publish("courses-updated")
    return Response(status_code=204)
