"""Where a tracker is along its course, how fast it's going, and when it will finish.

Each position is snapped to the nearest point on the course line (GPX), giving the distance
covered so far. Loops start and finish at the same place, and out-and-back sections or shared
trail pass the same spot twice, so one position can match several places along the course. The
one picked is closest to where the runner should be by now, going on from the previous position
at a running pace. A position farther than ON_COURSE_M from the course counts as off course.

Pace is the distance covered along the course over the last ten minutes or so (or, with no course,
the straight-line distance between fixes, which reads slower on twisty trail). The ETA assumes
that pace for the rest of the course.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

ON_COURSE_M = 120.0  # GPX lines and real trail differ; dual-band GPS itself is within a few metres
SAME_PLACE_M = 40.0  # matches this much farther than the nearest one still count
EXPECT_MPS = 2.5  # a running pace, for continuing from the previous position
RESET_AFTER_S = 20 * 60  # a gap this long: don't assume they kept going (e.g. a new runner)
PACE_WINDOW_S = 10 * 60
PACE_MIN_S = 90.0
MOVING_MPS = 0.4  # slower than this: stopped
FINISH_M = 75.0  # this close to the end: finished
CHUNK = 64  # segments per bounding box, to skip most of a long course quickly
M_PER_DEG = 111_320.0


@dataclass
class Geometry:
    """A course as one line: points, the distance covered at each, and which consecutive pairs
    are real segments (not the gap between two GPX track segments)."""

    pts: list[tuple[float, float]]
    cum: list[float]
    joined: list[bool]
    boxes: list[tuple[int, float, float, float, float]]  # (first segment, min lat, max lat, min lon, max lon)

    @property
    def total_m(self) -> float:
        return self.cum[-1] if self.cum else 0.0


def _dist(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    k = math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot((lat2 - lat1) * M_PER_DEG, (lon2 - lon1) * M_PER_DEG * k)


def build(segments: list[Any]) -> Geometry:
    pts: list[tuple[float, float]] = []
    cum: list[float] = []
    joined: list[bool] = []
    for seg in segments or []:
        first = True
        for p in seg:
            lat, lon = float(p[0]), float(p[1])
            if pts:
                joined.append(not first)
                cum.append(cum[-1] + (_dist(*pts[-1], lat, lon) if not first else 0.0))
            else:
                cum.append(0.0)
            pts.append((lat, lon))
            first = False
    boxes = []
    pad = ON_COURSE_M / M_PER_DEG
    for i in range(0, max(len(pts) - 1, 0), CHUNK):
        chunk = pts[i : i + CHUNK + 1]
        lats, lons = [p[0] for p in chunk], [p[1] for p in chunk]
        k = max(math.cos(math.radians(lats[0])), 0.05)
        boxes.append((i, min(lats) - pad, max(lats) + pad, min(lons) - pad / k, max(lons) + pad / k))
    return Geometry(pts, cum, joined, boxes)


_cache: dict[tuple[Any, int, float], Geometry] = {}


def geometry(course: Any) -> Geometry:
    """build(), remembered per course (a course's line never changes after upload)."""
    key = (course.id, len(course.segments or []), round(course.distance_m or 0.0, 1))
    g = _cache.get(key)
    if g is None:
        if len(_cache) > 32:
            _cache.clear()
        g = _cache[key] = build(course.segments)
    return g


def matches(g: Geometry, lat: float, lon: float) -> tuple[float, list[tuple[float, float]]]:
    """(distance to the nearest point of the course, [(distance off, distance along)] for each
    separate pass within ON_COURSE_M, nearest first)."""
    k = math.cos(math.radians(lat))
    best = math.inf
    near: dict[int, tuple[float, float]] = {}  # segment -> (off, along), for those within reach
    for first, la0, la1, lo0, lo1 in g.boxes:
        if not (la0 <= lat <= la1 and lo0 <= lon <= lo1):
            continue
        for i in range(first, min(first + CHUNK, len(g.pts) - 1)):
            if not g.joined[i]:
                continue
            (alat, alon), (blat, blon) = g.pts[i], g.pts[i + 1]
            ax, ay = (alon - lon) * M_PER_DEG * k, (alat - lat) * M_PER_DEG
            bx, by = (blon - lon) * M_PER_DEG * k, (blat - lat) * M_PER_DEG
            dx, dy = bx - ax, by - ay
            seg2 = dx * dx + dy * dy
            t = 0.0 if seg2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / seg2))
            off = math.hypot(ax + t * dx, ay + t * dy)
            best = min(best, off)
            if off > ON_COURSE_M:
                continue
            near[i] = (off, g.cum[i] + t * (g.cum[i + 1] - g.cum[i]))
    # Each pass by the position is where the distance to it bottoms out along the course (at a
    # turnaround, the way out and the way back are neighbouring segments: both count).
    passes: list[tuple[float, float]] = []
    for i, (off, along) in sorted(near.items()):
        before, after = near.get(i - 1, (math.inf, 0))[0], near.get(i + 1, (math.inf, 0))[0]
        if off <= before and off <= after and not any(abs(along - a) < SAME_PLACE_M for _, a in passes):
            passes.append((off, along))
    passes.sort()
    return best, [p for p in passes if p[0] <= passes[0][0] + SAME_PLACE_M]


@dataclass
class Step:
    at: datetime
    along: float | None  # None: off course
    off: float


def series(g: Geometry, points: list[tuple[datetime, float, float]]) -> list[Step]:
    """Distance along the course for each position (oldest first), following on from the last."""
    out: list[Step] = []
    prev: Step | None = None
    misses = 0
    for at, lat, lon in points:
        if prev is not None and (at - prev.at).total_seconds() > RESET_AFTER_S:
            prev = None
        off, cands = matches(g, lat, lon)
        if not cands:
            out.append(Step(at, None, off))
            misses += 1
            if misses >= 2:
                prev = None
            continue
        misses = 0
        if prev is None or prev.along is None:
            # No history: the earliest match (a loop's start rather than its finish).
            along = min(c[1] for c in cands)
        else:
            expected = min(prev.along + EXPECT_MPS * (at - prev.at).total_seconds(), g.total_m)
            along = min(cands, key=lambda c: abs(c[1] - expected) + 3 * c[0])[1]
        prev = Step(at, along, off)
        out.append(prev)
    return out


def pace(steps: list[Step]) -> float | None:
    """Metres per second along the course over the last PACE_WINDOW_S, or None (too little)."""
    on = [s for s in steps if s.along is not None]
    if len(on) < 2:
        return None
    last = on[-1]
    window = [s for s in on if 0 < (last.at - s.at).total_seconds() <= PACE_WINDOW_S]
    if not window:
        return None
    first = window[0]
    dt = (last.at - first.at).total_seconds()
    if dt < PACE_MIN_S:
        return None
    return max(0.0, (last.along - first.along) / dt)


def straight_pace(points: list[tuple[datetime, float, float]]) -> float | None:
    """Metres per second between fixes over the last PACE_WINDOW_S, ignoring the course."""
    if len(points) < 2:
        return None
    last = points[-1][0]
    window = [p for p in points if (last - p[0]).total_seconds() <= PACE_WINDOW_S]
    dt = (window[-1][0] - window[0][0]).total_seconds()
    if len(window) < 2 or dt < PACE_MIN_S:
        return None
    d = sum(_dist(a[1], a[2], b[1], b[2]) for a, b in zip(window, window[1:], strict=False))
    return d / dt


def pick_course(candidates: list[tuple[Any, Geometry]], points: list[tuple[datetime, float, float]]):
    """For a tracker without a chosen course: the course its latest position is on, preferring
    the one closest over its last three positions (loops often share their first stretch)."""
    if not points:
        return None
    lat, lon = points[-1][1], points[-1][2]
    on = [(c, g) for c, g in candidates if matches(g, lat, lon)[1]]
    if not on:
        return None
    recent = points[-3:]
    return min(on, key=lambda cg: sum(matches(cg[1], p[1], p[2])[0] for p in recent))
