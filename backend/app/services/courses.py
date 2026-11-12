"""Courses for the tracking map, from GPX files (tracks, routes and waypoints)."""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from app import db
from app.models import Course

MAX_GPX_BYTES = 2_000_000  # under the HTTPS front end's 2 MB request limit
MAX_POINTS = 20_000  # per course, after thinning
MIN_STEP_M = 3.0  # points closer than this to the previous one add nothing at map scale
MAX_WAYPOINTS = 200
# Named colours ("Green loop") get that colour; others get the first unused of the palette.
NAMED_COLORS = {
    "green": "#16a34a",
    "yellow": "#ca8a04",
    "red": "#dc2626",
    "blue": "#2563eb",
    "orange": "#ea580c",
    "purple": "#9333ea",
    "black": "#334155",
}
PALETTE = ("#16a34a", "#ca8a04", "#dc2626", "#2563eb", "#9333ea", "#ea580c", "#0891b2", "#334155")


class GpxError(ValueError):
    """The file isn't a GPX file MeshHome can read; the message says why."""


@dataclass
class ParsedCourse:
    name: str | None
    segments: list[list[list[float]]] = field(default_factory=list)
    waypoints: list[dict[str, Any]] = field(default_factory=list)

    @property
    def distance_m(self) -> float:
        return sum(
            distance_m(a[0], a[1], b[0], b[1])
            for seg in self.segments
            for a, b in zip(seg, seg[1:], strict=False)
        )


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(el: ET.Element, name: str) -> ET.Element | None:
    return next((c for c in el if _local(c.tag) == name), None)


def _text(el: ET.Element, name: str) -> str | None:
    c = _child(el, name)
    t = (c.text or "").strip() if c is not None else ""
    return t[:80] or None


def _point(el: ET.Element) -> list[float] | None:
    try:
        lat, lon = float(el.get("lat", "")), float(el.get("lon", ""))
    except ValueError:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or math.isnan(lat) or math.isnan(lon):
        return None
    return [round(lat, 6), round(lon, 6)]


def _thin(points: list[list[float]]) -> list[list[float]]:
    if len(points) < 3:
        return points
    out = [points[0]]
    for p in points[1:-1]:
        if distance_m(out[-1][0], out[-1][1], p[0], p[1]) >= MIN_STEP_M:
            out.append(p)
    out.append(points[-1])
    return out


def parse_gpx(data: bytes) -> ParsedCourse:
    if len(data) > MAX_GPX_BYTES:
        raise GpxError("The file is too large (the limit is 2 MB).")
    # GPX never needs a DTD; refusing one rules out entity expansion.
    if re.search(rb"<!(DOCTYPE|ENTITY)", data, re.IGNORECASE):
        raise GpxError("This GPX file declares a DOCTYPE, which MeshHome doesn't accept.")
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise GpxError("This isn't a GPX file MeshHome can read (it isn't valid XML).") from None
    if _local(root.tag) != "gpx":
        raise GpxError("This isn't a GPX file (its top element isn't <gpx>).")
    meta = _child(root, "metadata")
    course = ParsedCourse(name=_text(meta, "name") if meta is not None else None)
    for el in root:
        kind = _local(el.tag)
        if kind == "trk":
            course.name = course.name or _text(el, "name")
            for seg in el:
                if _local(seg.tag) == "trkseg":
                    pts = [p for p in (_point(x) for x in seg if _local(x.tag) == "trkpt") if p]
                    if len(pts) >= 2:
                        course.segments.append(pts)
        elif kind == "rte":
            course.name = course.name or _text(el, "name")
            pts = [p for p in (_point(x) for x in el if _local(x.tag) == "rtept") if p]
            if len(pts) >= 2:
                course.segments.append(pts)
        elif kind == "wpt" and len(course.waypoints) < MAX_WAYPOINTS:
            p = _point(el)
            if p:
                course.waypoints.append({"name": _text(el, "name") or "", "lat": p[0], "lon": p[1]})
    if not course.segments:
        raise GpxError("This GPX file has no track or route to draw.")
    course.segments = [_thin(seg) for seg in course.segments]
    total = sum(len(seg) for seg in course.segments)
    if total > MAX_POINTS:
        step = math.ceil(total / MAX_POINTS)
        course.segments = [
            seg[::step] + ([seg[-1]] if (len(seg) - 1) % step else []) for seg in course.segments
        ]
    return course


def color_for(name: str, used: set[str]) -> str:
    words = set(re.findall(r"[a-z]+", name.lower()))
    for word, color in NAMED_COLORS.items():
        if word in words:
            return color
    return next((c for c in PALETTE if c not in used), PALETTE[len(used) % len(PALETTE)])


async def sync_sim_route() -> None:
    """The simulated tracker runs the first visible course (the longest part of it), so the whole
    flow can be tried without hardware."""
    from app.radio import simulated

    async with db.session_factory()() as s:
        c = (
            await s.execute(select(Course).where(Course.visible).order_by(Course.created_at).limit(1))
        ).scalar_one_or_none()
        route = max(c.segments, key=len) if c is not None and c.segments else None
    simulated.set_sim_tracker_route(route)
