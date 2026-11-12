"""Tracking: polled positions from the simulated tracker, adverts, courses and the offline map."""

import asyncio
import uuid

import httpx
import pytest
from sqlalchemy import select

from app import db
from app.models import Contact, TrackedDevice, TrackPosition, utcnow
from app.radio import simulated
from app.services import backup, courses, offline_map, tracking
from app.services.offline_map import Bounds
from tests.conftest import csrf, do_setup, wait_for
from tests.test_api import _connected_conversations

GPX = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">
  <metadata><name>Green Loop</name></metadata>
  <wpt lat="40.0300" lon="-105.2500"><name>Exchange</name></wpt>
  <trk><name>ignored, metadata wins</name><trkseg>
    <trkpt lat="40.0300" lon="-105.2500"><ele>1600</ele></trkpt>
    <trkpt lat="40.0300" lon="-105.2500"></trkpt>
    <trkpt lat="40.0310" lon="-105.2480"></trkpt>
    <trkpt lat="40.0330" lon="-105.2470"></trkpt>
    <trkpt lat="40.0300" lon="-105.2500"></trkpt>
  </trkseg></trk>
</gpx>"""


@pytest.fixture(autouse=True)
def sim_tracker():
    st = simulated.sim_state()
    saved = {k: st.get(k) for k in ("tracker_dropout", "tracker_route_stale", "tracker_requests")}
    st["tracker_dropout"] = 0.0
    st["tracker_route_stale"] = False
    st["contact_paths"].pop(simulated.SIM_TRACKER_KEY, None)
    simulated.set_sim_tracker_route(None)
    yield st
    for k, v in saved.items():
        if v is None:
            st.pop(k, None)
        else:
            st[k] = v
    st["contact_paths"].pop(simulated.SIM_TRACKER_KEY, None)
    simulated.set_sim_tracker_route(None)


# ---- pure functions --------------------------------------------------------------------------


def test_parse_telemetry():
    fix, battery = tracking.parse_telemetry(
        {
            "lpp": [
                {"channel": 2, "type": "gps", "value": {"latitude": 1.0, "longitude": 2.0, "altitude": 3}},
                {"channel": 1, "type": "voltage", "value": 3.912},
                {
                    "channel": 1,
                    "type": "gps",
                    "value": {"latitude": 37.93, "longitude": -81.13, "altitude": 610.5},
                },
            ]
        }
    )
    assert fix == {"lat": 37.93, "lon": -81.13, "alt_m": 610.5}  # the node's own channel wins
    assert battery == 3.91
    # 0,0 is MeshCore's "no fix"; a reply without GPS has no position.
    assert tracking.parse_telemetry(
        {"lpp": [{"channel": 1, "type": "gps", "value": {"latitude": 0, "longitude": 0}}]}
    ) == (None, None)
    assert tracking.parse_telemetry({"lpp": [{"channel": 1, "type": "voltage", "value": 4.1}]}) == (None, 4.1)
    assert tracking.parse_telemetry(None) == (None, None)


def test_back_off():
    assert tracking.next_interval(60, 0, True) == 60
    assert tracking.next_interval(60, 2, True) == 60
    assert tracking.next_interval(60, 3, True) == 120
    assert tracking.next_interval(60, 4, True) == 240
    assert tracking.next_interval(60, 9, True) == 300  # at most 5 minutes
    assert tracking.next_interval(600, 5, True) == 600  # never more often than its own interval
    assert tracking.next_interval(60, 9, False) == 60


def test_parse_gpx():
    c = courses.parse_gpx(GPX)
    assert c.name == "Green Loop"
    assert len(c.segments) == 1 and len(c.segments[0]) == 4  # the duplicate point is thinned out
    assert c.waypoints == [{"name": "Exchange", "lat": 40.03, "lon": -105.25}]
    assert 500 < c.distance_m < 1000
    route = courses.parse_gpx(
        b'<gpx xmlns="http://www.topografix.com/GPX/1/0"><rte><name>R</name>'
        b'<rtept lat="1" lon="1"/><rtept lat="1.001" lon="1"/></rte></gpx>'
    )
    assert route.name == "R" and route.segments == [[[1.0, 1.0], [1.001, 1.0]]]
    for bad, why in (
        (b"<kml/>", "isn't a GPX"),
        (b"not xml", "valid XML"),
        (b'<!DOCTYPE gpx [<!ENTITY a "x">]><gpx/>', "DOCTYPE"),
        (b"<gpx><wpt lat='1' lon='1'/></gpx>", "no track"),
    ):
        with pytest.raises(courses.GpxError, match=why):
            courses.parse_gpx(bad)


def test_course_colors():
    assert courses.color_for("Ragnar Yellow Loop", set()) == courses.NAMED_COLORS["yellow"]
    assert courses.color_for("Loop 1", {courses.PALETTE[0]}) == courses.PALETTE[1]


def test_tile_math():
    assert offline_map.tile_xy(0, 0, 1) == (1, 1)
    assert offline_map.tile_xy(85.1, -180, 3) == (0, 0)
    b = Bounds(south=37.90, west=-81.16, north=37.96, east=-81.10)
    assert offline_map.count_tiles(b, 16, 16) == len(
        [
            (x, y)
            for _, x0, x1, y0, y1 in offline_map.tile_ranges(b, 16, 16)
            for x in range(x0, x1 + 1)
            for y in range(y0, y1 + 1)
        ]
    )
    assert offline_map.count_tiles(b, 0, 0) == 1
    with pytest.raises(offline_map.OfflineMapError, match="OpenStreetMap"):
        offline_map.check_custom_url("https://a.tile.openstreetmap.org/{z}/{x}/{y}.png")
    with pytest.raises(offline_map.OfflineMapError, match="https"):
        offline_map.check_custom_url("http://tiles.example/{z}/{x}/{y}.png")
    assert (
        offline_map.check_custom_url(" https://tiles.example/{z}/{x}/{y}.png ")
        == "https://tiles.example/{z}/{x}/{y}.png"
    )


def test_backups_leave_out_tiles():
    assert "map_tiles" not in {t.name for t in backup.tables()}
    assert {"tracked_devices", "track_positions", "courses"} <= {t.name for t in backup.tables()}


def test_sim_tracker_follows_the_course():
    simulated.set_sim_tracker_route([[10.0, 10.0], [10.0, 10.01]])
    lat, lon = simulated.sim_tracker_position(simulated._tracker_t0 + 100)  # 280 m along
    assert lat == pytest.approx(10.0) and 10.002 < lon < 10.003


# ---- the API, on the simulated radio ------------------------------------------------------------


async def _tracker_contact(client):
    page = (await client.get("/api/contacts", params={"q": "Tracker (sim)"})).json()
    return page["items"][0]


async def _enable(client, **cfg):
    r = await client.put(
        "/api/settings/tracking", headers=csrf(client), json={"enabled": True, "back_off": True, **cfg}
    )
    assert r.status_code == 200, r.text


async def _device(client, device_id):
    data = (await client.get("/api/tracking")).json()
    return next(d for d in data["devices"] if d["id"] == device_id)


async def test_tracking_flow(client, sim_tracker):
    await do_setup(client)
    await _connected_conversations(client)
    contact = await _tracker_contact(client)

    # Off by default: nothing can be tracked until it's turned on.
    data = (await client.get("/api/tracking")).json()
    assert data["config"]["enabled"] is False and data["devices"] == []
    r = await client.post("/api/tracking/devices", headers=csrf(client), json={"contact_id": contact["id"]})
    assert r.status_code == 409

    await _enable(client, trail_minutes=60)
    r = await client.post(
        "/api/tracking/devices", headers=csrf(client), json={"contact_id": contact["id"], "interval_s": 45}
    )
    assert r.status_code == 422  # not one of the intervals
    r = await client.post(
        "/api/tracking/devices",
        headers=csrf(client),
        json={"contact_id": contact["id"], "label": " Leg 1 – Sam "},
    )
    assert r.status_code == 201, r.text
    dev = r.json()
    assert dev["label"] == "Leg 1 – Sam" and dev["interval_s"] == 60 and dev["is_simulated"] is True
    assert (
        await client.post("/api/tracking/devices", headers=csrf(client), json={"contact_id": contact["id"]})
    ).status_code == 409
    # Repeaters aren't companions.
    rep = (await client.get("/api/contacts", params={"q": "Roof Repeater (sim)"})).json()["items"][0]
    assert (
        await client.post("/api/tracking/devices", headers=csrf(client), json={"contact_id": rep["id"]})
    ).status_code == 422

    # It's asked at once, and answers with where the simulated runner is.
    async def located():
        d = await _device(client, dev["id"])
        return d if d["status"] == "ok" and d["last_position"] else None

    d = await wait_for(located, timeout=30)
    assert 3.6 < d["battery_v"] <= 4.33 and d["last_position"]["source"] == "telemetry"
    assert 80 <= d["battery_pct"] <= 100 and d["battery_low"] is False  # a high-voltage cell
    assert 490 < d["last_position"]["alt_m"] < 535 and len(d["trail"]) >= 1
    # Its sensors, and how the reply came: by flood (no route yet), through the sim's repeaters.
    assert 26 < d["sensors"]["temp_c"] < 29 and 880 < d["sensors"]["pressure_hpa"] < 960
    assert d["link"]["route"] == "flood" and d["link"]["hops"] and d["link"]["snr"] is not None
    assert all(h["name"] for h in d["link"]["hops"])
    assert d["current_radio"] is True and 0 < d["next_poll_in"] <= 60
    lat, lon = d["last_position"]["lat"], d["last_position"]["lon"]
    assert 40.02 < lat < 40.035 and -105.26 < lon < -105.24  # on the built-in loop

    # Asking again on demand adds a position.
    assert (
        await client.post(f"/api/tracking/devices/{dev['id']}/poll", headers=csrf(client))
    ).status_code == 202

    async def two():
        return len((await _device(client, dev["id"]))["trail"]) >= 2

    await wait_for(two, timeout=15)

    # Hidden from the map, renamed, recoloured; then paused.
    r = await client.patch(
        f"/api/tracking/devices/{dev['id']}",
        headers=csrf(client),
        json={"show": False, "label": "", "color": "#00FF00", "interval_s": 300},
    )
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["show"] is False and d["label"] is None and d["color"] == "#00ff00" and d["interval_s"] == 300
    d = (
        await client.patch(
            f"/api/tracking/devices/{dev['id']}", headers=csrf(client), json={"polling": False}
        )
    ).json()
    assert d["polling"] is False and d["next_poll_in"] is None

    # GPX export of its track.
    r = await client.get(f"/api/tracking/devices/{dev['id']}/track.gpx")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/gpx+xml")
    assert r.text.count("<trkpt") >= 2 and "<name>Tracker (sim)</name>" in r.text
    assert courses.parse_gpx(r.content).segments

    # Clearing its history keeps it tracked; removing it deletes the rest.
    assert (
        await client.delete(f"/api/tracking/devices/{dev['id']}/positions", headers=csrf(client))
    ).status_code == 204
    assert (await _device(client, dev["id"]))["last_position"] is None
    assert (
        await client.delete(f"/api/tracking/devices/{dev['id']}", headers=csrf(client))
    ).status_code == 204
    assert (await client.get("/api/tracking")).json()["devices"] == []
    async with db.session_factory()() as s:
        assert (await s.execute(select(TrackPosition))).first() is None
        assert await s.get(Contact, contact["id"]) is not None  # the contact stays


async def test_stale_route_floods_and_no_answer_backs_off(client, sim_tracker):
    await do_setup(client)
    await _connected_conversations(client)
    contact = await _tracker_contact(client)
    await _enable(client)
    # The radio has a route to the tracker that no longer reaches it.
    sim_tracker["contact_paths"][simulated.SIM_TRACKER_KEY] = (1, "aa")
    sim_tracker["tracker_route_stale"] = True
    async with db.session_factory()() as s:
        c = await s.get(Contact, contact["id"])
        c.meta = {**(c.meta or {}), "path_len": 1, "path_hex": "aa"}
        await s.commit()
    sim_tracker["tracker_requests"] = 0
    dev = (
        await client.post("/api/tracking/devices", headers=csrf(client), json={"contact_id": contact["id"]})
    ).json()

    async def located():
        d = await _device(client, dev["id"])
        return d if d["status"] == "ok" else None

    await wait_for(located, timeout=25)
    # Unanswered on the route, so the route was forgotten and the flood got through.
    assert sim_tracker["tracker_requests"] == 2
    # The flood's reply taught the radio a new route in place of the stale one.
    assert sim_tracker["contact_paths"][simulated.SIM_TRACKER_KEY] != (1, "aa")
    assert (await _device(client, dev["id"]))["link"]["route"] == "flood"

    # Out of range: unanswered polls count up, and with back-off it's asked less often.
    sim_tracker["tracker_dropout"] = 1.0
    tracking.manager.forget(dev["id"])  # keep the scheduler out of the way
    async with db.session_factory()() as s:
        await s.execute(
            TrackedDevice.__table__.update().where(TrackedDevice.id == dev["id"]).values(polling=False)
        )
        await s.commit()
    for _ in range(3):
        assert await tracking.manager.poll(uuid.UUID(dev["id"])) == "no_answer"
    d = await _device(client, dev["id"])
    assert d["failures"] == 3 and "No answer" in d["status_detail"]
    assert tracking.manager.next_poll_in(uuid.UUID(dev["id"])) > 100


async def test_advert_positions(client, sim_tracker):
    await do_setup(client)
    await _connected_conversations(client)
    contact = await _tracker_contact(client)
    await _enable(client)
    sim_tracker["tracker_dropout"] = 1.0  # only adverts
    dev = (
        await client.post("/api/tracking/devices", headers=csrf(client), json={"contact_id": contact["id"]})
    ).json()
    async with db.session_factory()() as s:
        c = await s.get(Contact, contact["id"])
        radio_id = c.radio_id
    await tracking.manager.on_contacts_synced(radio_id)
    await tracking.manager.on_contacts_synced(radio_id)  # the same advert counts once

    async def positions():
        async with db.session_factory()() as s:
            return (
                (await s.execute(select(TrackPosition).where(TrackPosition.device_id == dev["id"])))
                .scalars()
                .all()
            )

    rows = await positions()
    assert len(rows) == 1 and rows[0].source == "advert"
    assert (rows[0].lat, rows[0].lon) == (40.0274, -105.2519)
    # A newer advert is another position.
    async with db.session_factory()() as s:
        c = await s.get(Contact, contact["id"])
        c.last_advert_at, c.lat = utcnow(), 40.03
        await s.commit()
    await tracking.manager.on_contacts_synced(radio_id)
    assert len(await positions()) == 2


async def test_courses(client):
    await do_setup(client)
    r = await client.post(
        "/api/tracking/courses",
        headers={**csrf(client), "Content-Type": "application/gpx+xml"},
        content=GPX,
    )
    assert r.status_code == 201, r.text
    c = r.json()
    assert c["name"] == "Green Loop" and c["color"] == courses.NAMED_COLORS["green"] and c["visible"]
    assert c["waypoints"][0]["name"] == "Exchange" and len(c["segments"][0]) == 4
    # The simulated tracker now runs it.
    lat, lon = simulated.sim_tracker_position()
    assert 40.029 <= lat <= 40.034 and -105.251 <= lon <= -105.246

    r = await client.post("/api/tracking/courses?name=Red%20loop", headers=csrf(client), content=b"<gpx/>")
    assert r.status_code == 422 and "no track" in r.json()["detail"]
    r = await client.post(
        "/api/tracking/courses", headers=csrf(client), content=b"x" * (courses.MAX_GPX_BYTES + 1)
    )
    assert r.status_code == 413

    r = await client.patch(
        f"/api/tracking/courses/{c['id']}", headers=csrf(client), json={"name": "Green", "visible": False}
    )
    assert r.json()["name"] == "Green" and r.json()["visible"] is False
    assert len((await client.get("/api/tracking/courses")).json()) == 1
    assert (await client.delete(f"/api/tracking/courses/{c['id']}", headers=csrf(client))).status_code == 204
    assert (await client.get("/api/tracking/courses")).json() == []


# ---- offline map -----------------------------------------------------------------------------------


@pytest.fixture
def tile_server(monkeypatch):
    """Tile requests answered locally: z/x/y as a tiny PNG, nothing at zoom 3 or x == 0."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        parts = request.url.path.strip("/").split("/")
        z, y, x = (int(p) for p in parts[-3:])  # USGS order: {z}/{y}/{x}
        if z == 3 or x == 0:
            return httpx.Response(404, text="no tile")
        return httpx.Response(
            200, content=b"\x89PNG" + f"{z}/{x}/{y}".encode(), headers={"content-type": "image/png"}
        )

    real = httpx.AsyncClient
    monkeypatch.setattr(
        offline_map.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    return seen


async def _job_finished(client):
    async def done():
        j = (await client.get("/api/offline-map")).json()
        return j if j["job"]["state"] not in ("running", "idle") else None

    return await wait_for(done, timeout=15)


async def test_offline_map(client, tile_server):
    await do_setup(client)
    data = (await client.get("/api/offline-map")).json()
    assert {s["id"] for s in data["sources"]} == {"usgs-topo", "usgs-imagery"}
    assert data["saved"]["tiles"] == 0 and data["job"]["state"] == "idle"

    area = {"bounds": {"south": -10, "west": -10, "north": 10, "east": 10}, "min_zoom": 2, "max_zoom": 4}
    est = (await client.post("/api/offline-map/estimate", headers=csrf(client), json=area)).json()["tiles"]
    assert est == offline_map.count_tiles(Bounds(-10, -10, 10, 10), 2, 4)

    # OpenStreetMap's servers are refused, and another server needs confirming.
    osm = {
        **area,
        "source": "custom",
        "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "permitted": True,
    }
    r = await client.post("/api/offline-map/download", headers=csrf(client), json=osm)
    assert r.status_code == 422 and "OpenStreetMap" in r.json()["detail"]
    r = await client.post(
        "/api/offline-map/download",
        headers=csrf(client),
        json={**area, "source": "custom", "url": "https://tiles.example/{z}/{x}/{y}.png"},
    )
    assert r.status_code == 422 and "Confirm" in r.json()["detail"]
    big = {
        **area,
        "min_zoom": 0,
        "max_zoom": 16,
        "bounds": {"south": -60, "west": -170, "north": 60, "east": 170},
    }
    r = await client.post("/api/offline-map/download", headers=csrf(client), json=big)
    assert r.status_code == 422 and "limit" in r.json()["detail"]

    r = await client.post("/api/offline-map/download", headers=csrf(client), json=area)
    assert r.status_code == 202, r.text
    j = await _job_finished(client)
    assert j["job"]["state"] == "done", j
    assert j["job"]["missing"] > 0 and j["job"]["failed"] == 0
    assert j["saved"]["tiles"] == j["job"]["done"] > 0 and j["saved"]["source"] == "usgs-topo"
    assert j["saved"]["min_zoom"] == 2 and j["saved"]["max_zoom"] == 4
    # Turned on for the maps once something is saved.
    assert (await client.get("/api/settings/map")).json()["offline"] is True
    assert (await client.get("/api/map")).json()["offline"]["tiles"] == j["saved"]["tiles"]

    x, y = offline_map.tile_xy(0, 0, 4)
    r = await client.get(f"/api/tiles/4/{x}/{y}")
    assert r.status_code == 200 and r.content == b"\x89PNG" + f"4/{x}/{y}".encode()
    assert r.headers["content-type"] == "image/png" and "max-age" in r.headers["cache-control"]
    assert (await client.get("/api/tiles/3/1/1")).status_code == 404
    # A map asks for many tiles at once; they must not exhaust the connection pool.
    many = await asyncio.wait_for(
        asyncio.gather(*(client.get(f"/api/tiles/4/{x}/{y}") for _ in range(30))), timeout=20
    )
    assert all(r.status_code == 200 for r in many)

    # Again: only what's missing is fetched.
    before = len(tile_server)
    await client.post("/api/offline-map/download", headers=csrf(client), json=area)
    j = await _job_finished(client)
    assert j["job"]["skipped"] == j["saved"]["tiles"]
    assert len(tile_server) - before == j["job"]["missing"] * 1  # the gaps are asked for again

    assert (await client.delete("/api/offline-map", headers=csrf(client))).status_code == 204
    assert (await client.get("/api/offline-map")).json()["saved"]["tiles"] == 0
    assert (await client.get("/api/settings/map")).json()["offline"] is False


async def test_offline_map_without_internet(client, monkeypatch):
    await do_setup(client)

    def down(request):
        raise httpx.ConnectError("no route to host")

    real = httpx.AsyncClient
    monkeypatch.setattr(
        offline_map.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(down), **kw)
    )
    monkeypatch.setattr(offline_map, "ATTEMPTS", 1)
    area = {"bounds": {"south": -60, "west": -170, "north": 60, "east": 170}, "min_zoom": 4, "max_zoom": 5}
    assert (
        await client.post("/api/offline-map/download", headers=csrf(client), json=area)
    ).status_code == 202
    j = await _job_finished(client)
    assert j["job"]["state"] == "error" and "Couldn't reach" in j["job"]["error"]
    assert j["job"]["failed"] >= offline_map.GIVE_UP_AFTER
    await asyncio.sleep(0)
