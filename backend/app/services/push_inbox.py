"""The latest reading each installed bot app's push source received.

A push source (bot_recipes, `network: "push"`) gets its data from a device that *sends* readings to
MeshHome, instead of MeshHome requesting them. Whatever brings a reading in (today the HTTP route
in app/api/push_receiver.py; later perhaps UDP or MQTT) records it here, and commands read the
latest one. Only the latest reading per app is kept, in memory: devices send again within a minute,
so losing it on a restart is harmless.
"""

from __future__ import annotations

import time
from typing import Any

MIN_INTERVAL = 1.0  # seconds: at most one stored reading per app per second

# app id -> (monotonic time, wall-clock time, data)
_latest: dict[str, tuple[float, float, Any]] = {}


def accept(app_id: str, data: Any) -> bool:
    """Store a reading. False (and nothing stored) if the last one came less than a second ago."""
    now = time.monotonic()
    last = _latest.get(app_id)
    if last and now - last[0] < MIN_INTERVAL:
        return False
    _latest[app_id] = (now, time.time(), data)
    return True


def latest(app_id: str, max_age: float) -> Any | None:
    """The reading, if one arrived in the last `max_age` seconds."""
    last = _latest.get(app_id)
    if last is None or time.monotonic() - last[0] > max_age:
        return None
    return last[2]


def received_at(app_id: str) -> float | None:
    """When the latest reading arrived (seconds since the epoch), or None if none has."""
    last = _latest.get(app_id)
    return last[1] if last else None


def forget(app_id: str) -> None:
    _latest.pop(app_id, None)
