"""Meshtastic trackers: the protobuf codec, and the whole flow against the simulated node (which
speaks the same messages as meshtasticd). MESHTASTICD_TEST_PORT runs one more test against a real
meshtasticd (e.g. `docker run -p 4403:4403 meshtastic/meshtasticd meshtasticd -s`)."""

import asyncio
import os

import pytest
from sqlalchemy import select

from app import db
from app.meshtastic import proto as P
from app.meshtastic import sim
from app.meshtastic.client import Connection, TcpLink
from app.models import TrackPosition
from app.services import meshtastic
from tests.conftest import csrf, do_setup, wait_for


@pytest.fixture(autouse=True)
def fast_sim(monkeypatch):
    monkeypatch.setenv("MESHHOME_SIM_MESHTASTIC_FAST", "1")
    sim.reset()
    yield
    sim.reset()


# ---- the codec -------------------------------------------------------------------------------


def test_round_trip_and_unknown_fields():
    pos = {
        "latitude_i": 375446000,
        "longitude_i": -780842000,
        "altitude": -12,
        "time": 1794504525,
        "precision_bits": 32,
    }
    pkt = {
        "from": 0xCC000002,
        "to": P.BROADCAST,
        "id": 0xDEADBEEF,
        "rx_snr": 6.25,
        "rx_rssi": -97,
        "hop_start": 3,
        "hop_limit": 2,
        "relay_node": 0x2A,
        "decoded": {"portnum": P.POSITION_APP, "payload": P.encode(pos, P.POSITION)},
    }
    raw = P.encode({"packet": pkt}, P.FROM_RADIO)
    back = P.decode(raw, P.FROM_RADIO)["packet"]
    assert back["from"] == 0xCC000002 and back["to"] == P.BROADCAST and back["rx_rssi"] == -97
    assert back["rx_snr"] == 6.25 and back["relay_node"] == 0x2A
    fix = P.position_fix(P.decode(back["decoded"]["payload"], P.POSITION))
    assert fix == {"lat": 37.5446, "lon": -78.0842, "alt_m": -12.0}
    assert P.position_fix({}) is None  # no fix
    assert P.node_id(0xCC000002) == "!cc000002"

    # Changing a setting keeps the fields this codec doesn't know (field 105 here).
    lora = P.encode({"use_preset": True, "region": 1, "hop_limit": 3, "tx_enabled": True}, P.LORA_CONFIG)
    lora += bytes([0xC8, 0x06, 0x01])  # field 105, varint 1
    patched = P.patch(lora, P.LORA_CONFIG, {"hop_limit": 5})
    assert P.decode(patched, P.LORA_CONFIG)["hop_limit"] == 5
    assert bytes([0xC8, 0x06, 0x01]) in patched
    assert P.decode(patched, P.LORA_CONFIG)["region"] == 1

    # The share link decodes back to the channel and LoRa settings.
    import base64

    settings = P.encode(
        {"name": "Ragnar", "psk": b"k" * 32, "module_settings": {"position_precision": 32}},
        P.CHANNEL_SETTINGS,
    )
    url = P.channel_url([settings], lora)
    assert url.startswith("https://meshtastic.org/e/#")
    b64 = url.split("#", 1)[1]
    cs = P.decode(base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)), P.CHANNEL_SET)
    assert cs["settings"][0]["name"] == "Ragnar" and cs["lora_config"]["region"] == 1
    assert P.frame(b"abc") == b"\x94\xc3\x00\x03abc"


def test_hop_path():
    assert meshtastic.hop_path({"from": 0xAB, "hop_start": 3, "hop_limit": 3, "relay_node": 0xAB}) == []
    assert meshtastic.hop_path({"from": 0xAB, "hop_start": 3, "hop_limit": 2, "relay_node": 0x00}) == ["?"]
    assert meshtastic.hop_path({"from": 0xAB, "hop_start": 3, "hop_limit": 1, "relay_node": 0x2A}) == [
        "?",
        "2a",
    ]
    assert meshtastic.hop_path({"from": 0xAB}) is None  # older firmware: unknown


# ---- the flow, against the simulated node ----------------------------------------------------


async def _start(client):
    await do_setup(client)
    r = await client.put(
        "/api/settings/tracking", headers=csrf(client), json={"enabled": True, "trail_minutes": 60}
    )
    assert r.status_code == 200, r.text
    assert (
        await client.put("/api/settings/meshtastic", headers=csrf(client), json={"source": "network"})
    ).status_code == 422  # needs an address
    r = await client.put("/api/settings/meshtastic", headers=csrf(client), json={"source": "simulated"})
    assert r.status_code == 200, r.text

    async def connected():
        st = (await client.get("/api/tracking/meshtastic")).json()["status"]
        return st if st and st["state"] == "connected" else None

    return await wait_for(connected, timeout=15)


async def test_meshtastic_trackers(client):
    st = await _start(client)
    assert st["node"]["node_id"] == P.node_id(sim.BASE_NUM) and st["region"] == "US"
    assert st["channel"] == {"name": "", "private": False, "precise": False}  # the public default

    async def nodes():
        ns = (await client.get("/api/tracking/meshtastic/nodes")).json()
        return ns if any(n["has_position"] for n in ns) else None

    ns = await wait_for(nodes, timeout=10)
    names = {n["long_name"]: n for n in ns}
    assert names["Car Router"]["role"] == "ROUTER" and names["MeshHome base"]["is_self"]
    x1 = names["X1 Tracker 1"]
    assert x1["role"] == "TRACKER" and x1["simulated"] and not x1["tracked"]

    # Track it: either a contact or a node.
    bad = await client.post("/api/tracking/devices", headers=csrf(client), json={"label": "x"})
    assert bad.status_code == 422
    assert (
        await client.post("/api/tracking/devices", headers=csrf(client), json={"node_num": 12345})
    ).status_code == 404
    r = await client.post(
        "/api/tracking/devices", headers=csrf(client), json={"node_num": x1["num"], "label": "Leg 1"}
    )
    assert r.status_code == 201, r.text
    dev = r.json()
    assert dev["source"] == "meshtastic" and dev["node_id"] == x1["node_id"] and dev["contact_id"] is None
    assert dev["polling"] is False and dev["is_simulated"] and dev["current_radio"]
    assert (
        await client.post("/api/tracking/devices", headers=csrf(client), json={"node_num": x1["num"]})
    ).status_code == 409

    async def tracked():
        d = next(x for x in (await client.get("/api/tracking")).json()["devices"] if x["id"] == dev["id"])
        ready = d["link"] and d["battery_pct"] is not None and d["sensors"] and len(d["trail"]) >= 3
        return d if ready else None

    d = await wait_for(tracked, timeout=15)
    assert d["status"] == "ok" and d["last_position"]["source"] == "meshtastic"
    assert d["link"]["route"] == "mesh"
    # Through the car (named from the node list) or heard directly.
    assert d["link"]["hops"] in ([], [{"hash": f"{sim.CAR_NUM & 0xFF:02x}", "name": "Car Router"}])
    assert 0 < d["battery_pct"] <= 100 and d["battery_v"] > 3.5  # as the tracker reports it
    assert d["sensors"]["temp_c"] and d["sensors"]["pressure_hpa"]
    # On the default public channel positions come blurred (as Meshtastic sends them).
    assert d["last_position"]["lat"] == round(d["last_position"]["lat"], 2)

    # Interval and polling don't apply; label and course do.
    r = await client.patch(
        f"/api/tracking/devices/{dev['id']}",
        headers=csrf(client),
        json={"polling": True, "interval_s": 300, "label": "Leg 2"},
    )
    assert r.status_code == 200 and r.json()["polling"] is False and r.json()["label"] == "Leg 2"

    # Ask now: a position request, answered by the tracker.
    async with db.session_factory()() as s:
        before = len((await s.execute(select(TrackPosition.id))).all())
    r = await client.post(f"/api/tracking/devices/{dev['id']}/poll", headers=csrf(client))
    assert r.status_code == 202, r.text

    async def answered():
        async with db.session_factory()() as s:
            return len((await s.execute(select(TrackPosition.id))).all()) > before

    await wait_for(answered, timeout=5)

    # The team channel: private, full precision. The QR link needs a signed-in session.
    r = await client.post(
        "/api/tracking/meshtastic/channel", headers=csrf(client), json={"name": "Bad name!"}
    )
    assert r.status_code == 422
    r = await client.post(
        "/api/tracking/meshtastic/channel", headers=csrf(client), json={"name": "Ragnar", "preset": 7}
    )
    assert r.status_code == 202, r.text

    async def private():
        s = (await client.get("/api/tracking/meshtastic")).json()["status"]
        return s if s and s["state"] == "connected" and s["channel"]["private"] else None

    st = await wait_for(private, timeout=15)
    assert (
        st["channel"] == {"name": "Ragnar", "private": True, "precise": True}
        and st["preset"] == "LONG_MODERATE"
    )
    url = (await client.get("/api/tracking/meshtastic/channel-url")).json()["url"]
    assert url.startswith("https://meshtastic.org/e/#")

    # Now positions arrive at full precision.
    async def precise():
        d = next(x for x in (await client.get("/api/tracking")).json()["devices"] if x["id"] == dev["id"])
        lat = d["last_position"]["lat"]
        return d if lat != round(lat, 2) else None

    await wait_for(precise, timeout=10)

    # Removing the tracker keeps the node.
    assert (
        await client.delete(f"/api/tracking/devices/{dev['id']}", headers=csrf(client))
    ).status_code == 204
    ns = (await client.get("/api/tracking/meshtastic/nodes")).json()
    assert any(n["num"] == x1["num"] and not n["tracked"] for n in ns)

    # Off again: disconnected, and the tracking page says nothing about Meshtastic.
    r = await client.put("/api/settings/meshtastic", headers=csrf(client), json={"source": "off"})
    assert r.status_code == 200

    async def off():
        return (await client.get("/api/tracking")).json()["meshtastic"] is None

    await wait_for(off, timeout=5)


async def test_meshtastic_needs_tracking_on(client):
    await do_setup(client)
    r = await client.put("/api/settings/meshtastic", headers=csrf(client), json={"source": "simulated"})
    assert r.status_code == 200
    await asyncio.sleep(0.5)
    assert meshtastic.source.state == "off"  # tracking is off
    assert (await client.get("/api/tracking/meshtastic/channel-url")).status_code == 409


@pytest.mark.skipif(not os.environ.get("MESHTASTICD_TEST_PORT"), reason="needs a running meshtasticd")
async def test_real_meshtasticd():
    c = Connection(TcpLink("127.0.0.1", int(os.environ["MESHTASTICD_TEST_PORT"])))
    snap = await c.start()
    assert snap.my_num and snap.metadata.get("firmware_version") and 0 in snap.channels and snap.lora
    run = asyncio.create_task(c.run(lambda m: asyncio.sleep(0)))
    try:
        assert await c.admin({"begin_edit_settings": True}) == {}  # session key fetched and accepted
        assert await c.admin({"commit_edit_settings": True}) == {}
    finally:
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)
        await c.close()
