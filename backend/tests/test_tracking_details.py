"""Tracking details: course progress, pace and ETA, battery percent, sensors, and reply routes."""

import math
import uuid
from datetime import timedelta

import pytest

from app import db
from app.models import TrackedDevice, TrackPosition, utcnow
from app.radio import packets
from app.services import progress, tracking
from app.services.msg_paths import PathTracker
from tests.conftest import csrf, do_setup
from tests.test_api import _connected_conversations
from tests.test_tracking import _device, _enable, _tracker_contact, sim_tracker  # noqa: F401

LAT0, LON0, R = 40.0, -105.0, 500.0  # a round loop, about 3.1 km


def _on_loop(f: float, r: float = R) -> tuple[float, float]:
    """The point a fraction f of the way round the loop (clockwise from the east), r metres out."""
    a = 2 * math.pi * f
    return (
        LAT0 - r * math.sin(a) / progress.M_PER_DEG,
        LON0 + r * math.cos(a) / (progress.M_PER_DEG * math.cos(math.radians(LAT0))),
    )


LOOP = [[list(_on_loop(i / 72)) for i in range(73)]]


def _out_and_back() -> list[list[list[float]]]:
    """1 km east and back the same way."""
    k = progress.M_PER_DEG * math.cos(math.radians(LAT0))
    out = [[LAT0, LON0 + x / k] for x in range(0, 1001, 50)]
    return [out + out[-2::-1]]


def _steps(g, fs: list[float], every_s: float = 60.0):
    t0 = utcnow()
    return progress.series(g, [(t0 + timedelta(seconds=i * every_s), *_on_loop(f)) for i, f in enumerate(fs)])


def test_progress_on_a_loop():
    g = progress.build(LOOP)
    assert g.total_m == pytest.approx(2 * math.pi * R, rel=0.01)
    # At the start/finish with no history: the start, not the finish.
    assert _steps(g, [0.0])[0].along == pytest.approx(0, abs=1)
    # Coming round to the finish: the finish, not the start.
    s = _steps(g, [0.9, 0.95, 0.999])
    assert s[-1].along == pytest.approx(g.total_m, rel=0.01)
    # A quarter of the way round, a little off the line (GPX and trail differ).
    s = _steps(g, [0.2, 0.25])
    assert s[-1].along == pytest.approx(g.total_m / 4, rel=0.02) and s[-1].off < 1
    off, cands = progress.matches(g, *_on_loop(0.125, R + 60))
    assert cands and 55 < off < 65


def test_off_course():
    g = progress.build(LOOP)
    off, cands = progress.matches(g, *_on_loop(0.125, R + 200))
    assert not cands and 190 < off < 210  # near enough to measure
    off, cands = progress.matches(g, *_on_loop(0.0, R + 400))
    assert not cands and off == math.inf  # beyond what's searched
    assert _steps(g, [0.1])[0].along is not None


def test_out_and_back_follows_on():
    g = progress.build(_out_and_back())
    assert g.total_m == pytest.approx(2000, rel=0.01)
    k = progress.M_PER_DEG * math.cos(math.radians(LAT0))
    t0 = utcnow()
    pts = []
    for i in range(13):  # 150 m a minute, out to 1 km and back to 200 m
        a = 150.0 * i
        x = a if a <= 1000 else 2000 - a
        pts.append((t0 + timedelta(minutes=i), LAT0, LON0 + x / k))
    s = progress.series(g, pts)
    assert [round(x.along / 10) * 10 for x in s] == [150 * i for i in range(13)]
    assert progress.pace(s) == pytest.approx(2.5, rel=0.02)


def test_pace_needs_time_and_reads_stopped():
    g = progress.build(LOOP)
    assert progress.pace(_steps(g, [0.1, 0.11], every_s=30)) is None  # under 90 s
    s = _steps(g, [0.3, 0.3, 0.3])
    assert progress.pace(s) == pytest.approx(0, abs=0.05)
    t0 = utcnow()
    k = progress.M_PER_DEG * math.cos(math.radians(LAT0))
    pts = [(t0 + timedelta(minutes=i), LAT0, LON0 + 120 * i / k) for i in range(4)]
    assert progress.straight_pace(pts) == pytest.approx(2.0, rel=0.02)


def test_battery_percent():
    assert tracking.battery_percent(4.2) == 100  # standard cell, full
    assert tracking.battery_percent(4.2, max_seen=4.36) == 88  # high-voltage cell
    assert tracking.battery_percent(4.37) == 100
    assert tracking.battery_percent(3.81) == 50
    assert tracking.battery_percent(3.2) == 0
    assert tracking.battery_percent(None) is None and tracking.battery_percent(12.6) is None


def test_parse_sensors():
    reply = {
        "lpp": [
            {"channel": 2, "type": "temperature", "value": 31.0},
            {"channel": 1, "type": "temperature", "value": 24.53},
            {"channel": 2, "type": "temperature", "value": 30.0},
            {"channel": 1, "type": "barometer", "value": 955.4},
            {"channel": 1, "type": "altitude", "value": 512.3},
            {"channel": 1, "type": "voltage", "value": 4.1},
            {"channel": 1, "type": "humidity", "value": 40},
        ]
    }
    assert tracking.parse_sensors(reply) == {"temp_c": 24.53, "pressure_hpa": 955.4, "baro_alt_m": 512.3}
    assert tracking.parse_sensors({"lpp": [{"type": "temperature", "value": True}]}) == {}
    assert tracking.parse_sensors(None) == {}


def test_reply_route():
    heard = {"hops": [], "route": "direct", "snr": 5.5, "rssi": -90}
    # Direct: the stored route (ours to it) reversed, so the tracker's end comes first.
    assert tracking.reply_route(heard, {"path_len": 2, "path_hex": "aabb"})["hops"] == ["bb", "aa"]
    assert tracking.reply_route(heard, {"path_len": 0, "path_hex": ""})["hops"] == []
    flood = {"hops": ["11", "22"], "route": "flood", "snr": -3.0, "rssi": -110}
    assert tracking.reply_route(flood, {"path_len": 1, "path_hex": "99"}) == {
        "route": "flood",
        "hops": ["11", "22"],
        "snr": -3.0,
        "rssi": -110,
    }
    assert tracking.reply_route(None, {}) is None


def test_reply_packets_are_matched_by_sender():
    now = [100.0]
    paths = PathTracker(clock=lambda: now[0])
    me, them = 0x5A, 0xC6
    reply = packets.parse(packets.build(packets.TYPE_PATH, bytes([me, them]) + b"x" * 18, [b"\x11", b"\x22"]))
    other = packets.parse(packets.build(packets.TYPE_RESPONSE, bytes([me, 0x01]) + b"y" * 18, []))
    assert paths.heard(other, 1.0, -100) is None and paths.heard(reply, 6.25, -95) is None
    got = paths.claim_reply(sender_hash=them, self_hash=me, since=99.0)
    assert got["hops"] == ["11", "22"] and got["route"] == "flood" and got["snr"] == 6.25
    assert paths.claim_reply(sender_hash=them, self_hash=me, since=100.5) is None  # heard before asking
    assert paths.claim_reply(sender_hash=them, self_hash=0x01, since=0) is None  # not to us


def _gpx(points: list[list[float]]) -> bytes:
    pts = "".join(f'<trkpt lat="{p[0]:.7f}" lon="{p[1]:.7f}"/>' for p in points)
    return (
        '<?xml version="1.0"?><gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">'
        f"<trk><name>Red Loop</name><trkseg>{pts}</trkseg></trk></gpx>"
    ).encode()


async def test_progress_pace_and_eta_in_the_api(client, sim_tracker):  # noqa: F811
    await do_setup(client)
    await _connected_conversations(client)
    await _enable(client)
    sim_tracker["tracker_dropout"] = 1.0  # positions come from this test only
    r = await client.post(
        "/api/tracking/courses",
        headers={**csrf(client), "Content-Type": "application/gpx+xml"},
        content=_gpx(LOOP[0]),
    )
    course = r.json()
    contact = await _tracker_contact(client)
    dev = (
        await client.post("/api/tracking/devices", headers=csrf(client), json={"contact_id": contact["id"]})
    ).json()
    assert dev["course_id"] is None and dev["progress"] is None
    # Ten minutes along the loop at 2.5 m/s, ending now, 60 % of the way round.
    total = 2 * math.pi * R
    now = utcnow()
    async with db.session_factory()() as s:
        d = await s.get(TrackedDevice, uuid.UUID(dev["id"]))
        d.polling, d.advert_seen_at = False, now + timedelta(days=1)
        d.battery_v, d.battery_max_v = 3.74, 4.36
        await s.execute(TrackPosition.__table__.delete().where(TrackPosition.device_id == d.id))
        for i in range(11):
            along = total * 0.6 - 150 * (10 - i)
            lat, lon = _on_loop(along / total)
            s.add(
                TrackPosition(
                    device_id=d.id, at=now - timedelta(minutes=10 - i), lat=lat, lon=lon, source="telemetry"
                )
            )
        await s.commit()
    tracking.manager.forget(uuid.UUID(dev["id"]))

    d = await _device(client, dev["id"])
    p = d["progress"]
    assert p["auto"] is True and p["course_id"] == course["id"] and p["course_name"] == "Red Loop"
    assert p["pct"] == pytest.approx(60, abs=1) and p["finished"] is False
    assert d["pace"]["basis"] == "course" and d["pace"]["moving"] is True
    assert d["pace"]["speed_mps"] == pytest.approx(2.5, rel=0.03)
    assert d["pace"]["remaining_m"] == pytest.approx(total * 0.4, rel=0.02)
    assert d["pace"]["eta_at"] is not None
    assert d["battery_pct"] == 18 and d["battery_low"] is True

    # Chosen by hand; a course that doesn't exist is refused; deleting the course unsets it.
    r = await client.patch(
        f"/api/tracking/devices/{dev['id']}", headers=csrf(client), json={"course_id": course["id"]}
    )
    assert r.status_code == 200 and r.json()["progress"]["auto"] is False
    r = await client.patch(
        f"/api/tracking/devices/{dev['id']}", headers=csrf(client), json={"course_id": str(uuid.uuid4())}
    )
    assert r.status_code == 404
    await client.patch(f"/api/tracking/courses/{course['id']}", headers=csrf(client), json={"visible": False})
    assert (await _device(client, dev["id"]))["progress"]["course_id"] == course["id"]  # chosen: still used
    assert (
        await client.delete(f"/api/tracking/courses/{course['id']}", headers=csrf(client))
    ).status_code == 204
    d = await _device(client, dev["id"])
    assert d["course_id"] is None and d["progress"] is None
    assert d["pace"]["basis"] == "straight"  # no course: between fixes


async def test_units_setting(client):
    await do_setup(client)
    assert (await client.get("/api/settings/tracking")).json()["units"] == "metric"
    r = await client.put(
        "/api/settings/tracking", headers=csrf(client), json={"enabled": True, "units": "imperial"}
    )
    assert r.status_code == 200 and r.json()["units"] == "imperial"
    assert (await client.get("/api/tracking")).json()["config"]["units"] == "imperial"
    r = await client.put(
        "/api/settings/tracking", headers=csrf(client), json={"enabled": True, "units": "furlongs"}
    )
    assert r.status_code == 422
