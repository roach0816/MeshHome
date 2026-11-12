"""Tracking (services/tracking.py): tracked devices, their positions, and courses (GPX)."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta
from xml.sax.saxutils import escape, quoteattr

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, require_auth, require_session
from app.db import get_db
from app.models import AuditEvent, Contact, Course, Radio, TrackedDevice, TrackPosition, utcnow
from app.radio.supervisor import supervisor
from app.realtime import hub
from app.services import app_settings, courses, offline_map, progress, tracking

router = APIRouter(prefix="/api", tags=["tracking"])

HEX_COLOR = r"^#[0-9a-fA-F]{6}$"
Interval = int  # one of tracking.INTERVALS


class TrackPoint(BaseModel):
    at: datetime
    lat: float
    lon: float
    alt_m: float | None = None
    source: str  # "telemetry" (it answered a request) | "advert"


class CourseProgress(BaseModel):
    course_id: uuid.UUID
    course_name: str
    color: str
    # True: the tracker has no course chosen, and this is the one its position is on.
    auto: bool
    total_m: float
    # Distance covered along the course, and as a percentage; None while off course.
    along_m: float | None
    pct: float | None
    # Off course: how far from the course line (None: farther than the course search reaches).
    off_course_m: float | None
    finished: bool
    at: datetime  # of the position this is for


class Pace(BaseModel):
    speed_mps: float
    # "course": distance along the course; "straight": between fixes (reads slower on trail).
    basis: str
    moving: bool
    # To the end of the course, at this pace from the last position (course basis only).
    remaining_m: float | None
    eta_at: datetime | None


class Hop(BaseModel):
    hash: str
    name: str | None  # the repeater it most likely is (a contact with that key prefix)


class ReplyLink(BaseModel):
    at: datetime
    route: str  # "direct" (its stored route) | "flood"
    hops: list[Hop]  # repeaters, the tracker's end first; empty: heard directly
    # Signal of the last hop, as this radio heard it.
    snr: float | None
    rssi: float | None


class Sensors(BaseModel):
    at: datetime
    temp_c: float | None
    pressure_hpa: float | None
    baro_alt_m: float | None


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
    # About how full its battery is, from the voltage; low: at or under 20 %.
    battery_pct: int | None = None
    battery_low: bool = False
    # The course chosen for it (None: the one its position is on).
    course_id: uuid.UUID | None = None
    progress: CourseProgress | None = None
    pace: Pace | None = None
    # How its latest answer reached this radio (when the radio logged the packet).
    link: ReplyLink | None = None
    # Its latest environment readings (when it shares them).
    sensors: Sensors | None = None
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
    # The app-wide units setting (GET/PUT /api/settings/units); values here are always metric.
    units: str = "metric"
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


PROGRESS_WINDOW = timedelta(minutes=30)


class _Context:
    """What every tracker's output needs: courses, and names for repeater hash prefixes."""

    def __init__(self, course_rows: list[Course], contacts: list[tuple[str, str | None, int]]):
        self.courses = {c.id: c for c in course_rows}
        self.contacts = contacts

    def hop_name(self, h: str) -> str | None:
        found = [(name, kind) for key, name, kind in self.contacts if key.startswith(h.lower())]
        repeaters = [n for n, k in found if k == 2]
        if len(repeaters) == 1:
            return repeaters[0]
        return found[0][0] if len(found) == 1 else None


async def _context(db: AsyncSession) -> _Context:
    course_rows = list((await db.execute(select(Course).order_by(Course.created_at))).scalars().all())
    contacts: list[tuple[str, str | None, int]] = []
    radio = supervisor.radio
    if radio is not None:
        contacts = [
            (k.lower(), alias or name, kind)
            for k, name, alias, kind in (
                await db.execute(
                    select(Contact.public_key, Contact.name, Contact.alias, Contact.kind).where(
                        Contact.radio_id == radio.id
                    )
                )
            ).all()
        ]
    return _Context(course_rows, contacts)


def _progress(
    ctx: _Context, d: TrackedDevice, recent: list[TrackPosition]
) -> tuple[CourseProgress | None, Pace | None]:
    points = [(p.at, p.lat, p.lon) for p in recent]
    if not points:
        return None, None
    course = ctx.courses.get(d.course_id) if d.course_id else None
    auto = course is None
    if auto:
        visible = [(c, progress.geometry(c)) for c in ctx.courses.values() if c.visible]
        picked = progress.pick_course(visible, points)
        course = picked[0] if picked else None
    if course is None:
        speed = progress.straight_pace(points)
        p = (
            None
            if speed is None
            else Pace(
                speed_mps=round(speed, 2),
                basis="straight",
                moving=speed >= progress.MOVING_MPS,
                remaining_m=None,
                eta_at=None,
            )
        )
        return None, p
    g = progress.geometry(course)
    steps = progress.series(g, points)
    last = steps[-1]
    total = g.total_m
    along = last.along
    finished = along is not None and total - along <= progress.FINISH_M and along > total / 2
    out = CourseProgress(
        course_id=course.id,
        course_name=course.name,
        color=course.color,
        auto=auto,
        total_m=round(total, 1),
        along_m=None if along is None else round(along, 1),
        pct=None if along is None or total <= 0 else round(min(100.0, 100 * along / total), 1),
        off_course_m=None if along is not None or last.off == float("inf") else round(last.off),
        finished=finished,
        at=last.at,
    )
    speed = progress.pace(steps)
    basis = "course"
    if speed is None:
        return out, None
    moving = speed >= progress.MOVING_MPS
    remaining = None if along is None else max(0.0, total - along)
    eta = None
    if moving and remaining is not None and not finished:
        eta = last.at + timedelta(seconds=remaining / speed)
    return out, Pace(
        speed_mps=round(speed, 2),
        basis=basis,
        moving=moving,
        remaining_m=None if remaining is None else round(remaining, 1),
        eta_at=eta,
    )


async def _device_out(
    db: AsyncSession,
    d: TrackedDevice,
    c: Contact,
    sim: bool,
    trail_minutes: int,
    ctx: _Context | None = None,
) -> TrackedDeviceOut:
    ctx = ctx or await _context(db)
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
    recent: list[TrackPosition] = []
    link_row = sensor_row = None
    if last is not None:
        since = last.at - PROGRESS_WINDOW
        recent = list(
            (
                await db.execute(
                    select(TrackPosition)
                    .where(TrackPosition.device_id == d.id, TrackPosition.at >= since)
                    .order_by(TrackPosition.at)
                    .limit(500)
                )
            )
            .scalars()
            .all()
        )
        link_row = (
            await db.execute(
                select(TrackPosition)
                .where(TrackPosition.device_id == d.id, TrackPosition.route.is_not(None))
                .order_by(TrackPosition.at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        sensor_row = (
            await db.execute(
                select(TrackPosition)
                .where(
                    TrackPosition.device_id == d.id,
                    or_(
                        TrackPosition.temp_c.is_not(None),
                        TrackPosition.pressure_hpa.is_not(None),
                        TrackPosition.baro_alt_m.is_not(None),
                    ),
                )
                .order_by(TrackPosition.at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    prog, pace = _progress(ctx, d, recent)
    pct = tracking.battery_percent(d.battery_v, d.battery_max_v)
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
        battery_pct=pct,
        battery_low=pct is not None and pct <= tracking.LOW_BATTERY_PCT,
        course_id=d.course_id,
        progress=prog,
        pace=pace,
        link=None
        if link_row is None
        else ReplyLink(
            at=link_row.at,
            route=link_row.route or "direct",
            hops=[Hop(hash=h, name=ctx.hop_name(h)) for h in (link_row.hops or []) if isinstance(h, str)],
            snr=link_row.snr,
            rssi=link_row.rssi,
        ),
        sensors=None
        if sensor_row is None
        else Sensors(
            at=sensor_row.at,
            temp_c=sensor_row.temp_c,
            pressure_hpa=sensor_row.pressure_hpa,
            baro_alt_m=sensor_row.baro_alt_m,
        ),
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
    ctx_ = await _context(db)
    devices = [await _device_out(db, d, c, sim, cfg.trail_minutes, ctx_) for d, c, sim in rows]
    base = None
    radio = supervisor.radio
    if radio is not None:
        r = await db.get(Radio, radio.id)
        loc = ((r.self_info if r else None) or {}).get("location") or {}
        if loc.get("lat") is not None and loc.get("lon") is not None:
            base = BasePosition(name=r.name, lat=loc["lat"], lon=loc["lon"])
    tiles = await app_settings.get_map_config(db)
    units = (await app_settings.get_units(db)).units
    await db.rollback()
    return TrackingOut(
        config=cfg,
        units=units,
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


@router.get("/settings/units", response_model=app_settings.UnitsConfig)
async def get_units_setting(ctx: AuthContext = Depends(require_auth), db: AsyncSession = Depends(get_db)):
    """How measurements are shown everywhere (radio sensors, the /env bot reply, tracking)."""
    return await app_settings.get_units(db)


@router.put("/settings/units", response_model=app_settings.UnitsConfig)
async def put_units_setting(
    body: app_settings.UnitsConfig,
    ctx: AuthContext = Depends(require_session),
    db: AsyncSession = Depends(get_db),
):
    await app_settings.put_units(db, body)
    db.add(AuditEvent(kind="settings.units", detail=body.model_dump()))
    await db.commit()
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
    # The course it's on, for progress and ETA; null: the one its position is on.
    course_id: uuid.UUID | None = None


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
    if "course_id" in fields:
        if body.course_id is not None and await db.get(Course, body.course_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Course not found")
        d.course_id = body.course_id
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
