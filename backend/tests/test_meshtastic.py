"""Meshtastic: the protobuf codec, and MeshHome running on a simulated Meshtastic radio (which
speaks the same messages as meshtasticd): messages, contacts, channels, node settings and trackers.
MESHTASTICD_TEST_PORT runs one more test against a real meshtasticd (e.g.
`docker run -p 4403:4403 meshtastic/meshtasticd meshtasticd -s`)."""

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


# ---- MeshHome on the simulated Meshtastic radio ----------------------------------------------


async def _meshtastic(client, **radio):
    await do_setup(client, mode="simulated", tech="meshtastic", **radio)

    async def connected():
        st = (await client.get("/api/status")).json()["radio"]
        return st if st["state"] == "connected" and st["tech"] == "meshtastic" else None

    return await wait_for(connected, timeout=15)


async def _contacts(client):
    return {c["name"]: c for c in (await client.get("/api/contacts?page_size=50")).json()["items"]}


async def _conversations(client):
    return {c["title"]: c for c in (await client.get("/api/conversations")).json()}


async def test_messaging_on_meshtastic(client):
    st = await _meshtastic(client)
    assert st["is_simulated"] and st["radio_name"] == "MeshHome base"
    radio = (await client.get("/api/settings/radio")).json()
    assert radio["tech"] == "meshtastic" and radio["mode"] == "simulated"

    # Contacts: the nodes the radio has heard (not itself), with their Meshtastic details.
    async def heard():
        cs = await _contacts(client)
        return cs if {"Car Router", "X1 Tracker 1", "X1 Tracker 2", "Hiker Sam"} <= set(cs) else None

    contacts = await wait_for(heard)
    assert "MeshHome base" not in contacts
    sam = contacts["Hiker Sam"]
    assert sam["node_id"] == P.node_id(sim.HIKER[0]) and sam["kind"] == 1
    assert contacts["Car Router"]["kind"] == 2  # a relay
    detail = (await client.get(f"/api/contacts/{sam['id']}")).json()
    assert detail["meshtastic"]["short_name"] == "SAM" and detail["meshtastic"]["role"] == "CLIENT"
    assert detail["meshtastic"]["battery_level"] == 76 and detail["preferred_route"] is None
    assert detail["lat"] is not None  # its position, from the node list
    # MeshCore's contact sharing doesn't apply.
    assert (await client.get("/api/contacts/me")).status_code == 409

    # The primary channel, named after its preset as Meshtastic does.
    convs = await _conversations(client)
    assert "LongFast" in convs and convs["LongFast"]["kind"] == "channel"
    assert convs["LongFast"]["max_bytes"] == 200
    chan = convs["LongFast"]

    # A channel message from the hiker.
    async def simulate():
        return (await client.post("/api/radio/simulate-incoming", headers=csrf(client))).status_code == 200

    await wait_for(simulate)

    async def chatter():
        page = (await client.get(f"/api/conversations/{chan['id']}/messages")).json()
        return page["messages"] if page["messages"] else None

    items = await wait_for(chatter)
    assert items[-1]["sender_label"] == "Hiker Sam" and items[-1]["direction"] == "in"

    # Send on the channel.
    r = await client.post(
        f"/api/conversations/{chan['id']}/messages",
        headers=csrf(client),
        json={"client_message_id": "mtchan01", "body": "Runners through the gate"},
    )
    assert r.status_code == 200, r.text
    too_long = "x" * 201
    r = await client.post(
        f"/api/conversations/{chan['id']}/messages",
        headers=csrf(client),
        json={"client_message_id": "mtchan02", "body": too_long},
    )
    assert r.status_code == 422

    # A direct message: acknowledged by the hiker, who answers.
    r = await client.post(f"/api/contacts/{sam['id']}/conversation", headers=csrf(client))
    assert r.status_code in (200, 201), r.text
    dm = r.json()["conversation_id"] if "conversation_id" in r.json() else r.json()["id"]
    r = await client.post(
        f"/api/conversations/{dm}/messages",
        headers=csrf(client),
        json={"client_message_id": "mtdm0001", "body": "Are you at the summit?"},
    )
    assert r.status_code == 200, r.text
    mid = r.json()["id"]

    async def delivered():
        page = (await client.get(f"/api/conversations/{dm}/messages")).json()["messages"]
        mine = next(m for m in page if m["id"] == mid)
        answer = [m for m in page if m["direction"] == "in"]
        return page if mine["state"] == "acknowledged" and answer else None

    page = await wait_for(delivered, timeout=15)
    assert "Got your message: Are you at the summit?" in page[-1]["body"]

    # One attempt per message: the radio retries on its own. A node that never answers: the
    # radio gives up, and the message says so.
    assert (
        await client.delete(f"/api/contacts/{contacts['X1 Tracker 2']['id']}", headers=csrf(client))
    ).status_code == 204
    r = await client.post(
        f"/api/contacts/{contacts['X1 Tracker 2']['id']}/conversation", headers=csrf(client)
    )
    dm2 = r.json()["conversation_id"] if "conversation_id" in r.json() else r.json()["id"]
    r = await client.post(
        f"/api/conversations/{dm2}/messages",
        headers=csrf(client),
        json={"client_message_id": "mtdm0002", "body": "hello?"},
    )
    assert r.status_code == 200, r.text
    mid2 = r.json()["id"]

    async def failed():
        page = (await client.get(f"/api/conversations/{dm2}/messages")).json()["messages"]
        m = next(m for m in page if m["id"] == mid2)
        return m if m["state"] == "no_ack" else None

    m = await wait_for(failed, timeout=15)
    assert "no acknowledgement after retries" in m["error"]

    # A node that doesn't have this radio's key yet refuses a direct message; the radios swap
    # keys, and sending it again works.
    r = await client.post(f"/api/contacts/{contacts['Car Router']['id']}/conversation", headers=csrf(client))
    dm3 = r.json()["conversation_id"]
    r = await client.post(
        f"/api/conversations/{dm3}/messages",
        headers=csrf(client),
        json={"client_message_id": "mtdm0003", "body": "car check"},
    )
    mid3 = r.json()["id"]

    async def state_of(conv, mid, want):
        page = (await client.get(f"/api/conversations/{conv}/messages")).json()["messages"]
        m = next(m for m in page if m["id"] == mid)
        return m if m["state"] == want else None

    m = await wait_for(lambda: state_of(dm3, mid3, "failed"), timeout=15)
    assert "swapped keys" in m["error"]
    r = await client.post(f"/api/messages/{mid3}/retry", headers=csrf(client), json={})
    assert r.status_code == 200, r.text
    m = await wait_for(lambda: state_of(dm3, mid3, "acknowledged"), timeout=15)
    assert m["error"] is None

    # Favourites go to the radio.
    r = await client.post(
        f"/api/contacts/{sam['id']}/favorite", headers=csrf(client), json={"favorite": True}
    )
    assert r.status_code == 200 and r.json()["favorite"]

    # MeshCore is a separate world: its conversations come back when switching back.
    r = await client.put(
        "/api/settings/radio", headers=csrf(client), json={"tech": "meshcore", "mode": "simulated"}
    )
    assert r.status_code == 200

    async def meshcore():
        st = (await client.get("/api/status")).json()["radio"]
        return st["state"] == "connected" and st["tech"] == "meshcore"

    await wait_for(meshcore, timeout=15)
    convs = await _conversations(client)
    assert "LongFast" not in convs and "Public" in convs
    assert "Hiker Sam" not in await _contacts(client)


async def test_node_settings(client):
    await _meshtastic(client)
    cfg = (await client.get("/api/node/meshtastic")).json()
    assert cfg["node"]["long_name"] == "MeshHome base" and cfg["lora"]["region"] == "US"
    assert cfg["channels"] == [
        {
            "index": 0,
            "name": "",
            "display_name": "LongFast",
            "primary": True,
            "key": "default",
            "position_precision": 13,
        }
    ]
    assert "LONG_MODERATE" in cfg["presets"] and "CLIENT_BASE" in cfg["roles"]

    # Names, role and a fixed position.
    r = await client.put(
        "/api/node/meshtastic/owner",
        headers=csrf(client),
        json={"long_name": "Camp Base", "short_name": "CAMP"},
    )
    assert r.status_code == 204, r.text
    assert (
        await client.put("/api/node/meshtastic/role", headers=csrf(client), json={"role": "CLIENT_BASE"})
    ).status_code == 204
    r = await client.put(
        "/api/node/meshtastic/position", headers=csrf(client), json={"lat": 40.02, "lon": -105.25}
    )
    assert r.status_code == 204
    cfg = (await client.get("/api/node/meshtastic")).json()
    assert cfg["node"]["long_name"] == "Camp Base" and cfg["node"]["role"] == "CLIENT_BASE"
    assert cfg["node"]["lat"] == 40.02
    assert (await client.get("/api/status")).json()["radio"]["radio_name"] == "Camp Base"

    # A private team channel as the primary: positions at full precision; share it as a link.
    r = await client.put(
        "/api/node/meshtastic/channels/0",
        headers=csrf(client),
        json={"name": "Ragnar", "key": "random", "position_precision": 32},
    )
    assert r.status_code == 200, r.text
    url = (await client.get("/api/node/meshtastic/share-url?channels=0")).json()["url"]
    assert url.startswith("https://meshtastic.org/e/#")
    preview = (
        await client.post("/api/node/meshtastic/link-preview", headers=csrf(client), json={"url": url})
    ).json()
    assert preview["channels"] == [{"name": "Ragnar", "key": "private"}] and preview["region"] == "US"
    convs = await _conversations(client)
    assert "Ragnar" in convs and "LongFast" not in convs  # a new channel: a new conversation

    # A secondary channel, and joining one from someone's link.
    r = await client.post("/api/node/meshtastic/channels", headers=csrf(client), json={"name": "Crew"})
    assert r.status_code == 201 and r.json()["index"] == 1
    other = P.channel_url([P.encode({"name": "Medics", "psk": b"m" * 32}, P.CHANNEL_SETTINGS)], b"")
    r = await client.post("/api/node/meshtastic/import", headers=csrf(client), json={"url": other})
    assert r.status_code == 200 and r.json() == {"added": 1, "restarts": False}
    cfg = (await client.get("/api/node/meshtastic")).json()
    assert [c["name"] for c in cfg["channels"]] == ["Ragnar", "Crew", "Medics"]
    assert {"Crew", "Medics"} <= set(await _conversations(client))
    assert (await client.delete("/api/node/meshtastic/channels/2", headers=csrf(client))).status_code == 204
    assert (await client.delete("/api/node/meshtastic/channels/0", headers=csrf(client))).status_code == 422
    bad = await client.post(
        "/api/node/meshtastic/import", headers=csrf(client), json={"url": "https://example.com/x"}
    )
    assert bad.status_code == 422

    # A preset change restarts the radio, which reconnects with it.
    r = await client.put("/api/node/meshtastic/lora", headers=csrf(client), json={"preset": "LONG_MODERATE"})
    assert r.status_code == 200 and r.json()["restarts"] is True

    async def back():
        st = (await client.get("/api/status")).json()["radio"]
        if st["state"] != "connected":
            return None
        cfg = (await client.get("/api/node/meshtastic")).json()
        return cfg if cfg["lora"]["preset"] == "LONG_MODERATE" else None

    cfg = await wait_for(back, timeout=20)
    assert cfg["channels"][0]["name"] == "Ragnar"
    # MeshCore's features answer that they're MeshCore's.
    import uuid

    some = uuid.uuid4()
    for method, path, body in (
        ("GET", "/api/radio/config", None),
        ("GET", "/api/radio/firmware", None),
        ("GET", f"/api/remote/{some}", None),
        ("POST", "/api/radio/trace", {"hops": ["ab"]}),
        ("PUT", "/api/settings/routing", {"preferred_repeater": None}),
        ("POST", "/api/contacts/import", {"uri": "meshcore://x"}),
        ("POST", f"/api/contacts/{some}/reset-path", None),
    ):
        r = await client.request(method, path, headers=csrf(client), json=body)
        assert r.status_code == 409 and "MeshCore feature" in r.json()["detail"], (path, r.text)


async def test_meshtastic_trackers(client):
    await _meshtastic(client)
    r = await client.put(
        "/api/settings/tracking", headers=csrf(client), json={"enabled": True, "trail_minutes": 60}
    )
    assert r.status_code == 200, r.text

    async def nodes():
        ns = (await client.get("/api/tracking/meshtastic/nodes")).json()
        return ns if any(n["has_position"] and n["role"] == "TRACKER" for n in ns) else None

    ns = await wait_for(nodes, timeout=10)
    names = {n["long_name"]: n for n in ns}
    assert names["Car Router"]["role"] == "ROUTER" and names["MeshHome base"]["is_self"]
    x1 = names["X1 Tracker 1"]
    assert x1["simulated"] and not x1["tracked"]

    # Track it from its contact: a Meshtastic contact is tracked as its node.
    contact = (await _contacts(client))["X1 Tracker 1"]
    r = await client.post(
        "/api/tracking/devices", headers=csrf(client), json={"contact_id": contact["id"], "label": "Leg 1"}
    )
    assert r.status_code == 201, r.text
    dev = r.json()
    assert dev["source"] == "meshtastic" and dev["node_id"] == x1["node_id"] and dev["contact_id"] is None
    assert dev["polling"] is False and dev["is_simulated"] and dev["current_radio"]
    assert (
        await client.post("/api/tracking/devices", headers=csrf(client), json={"node_num": x1["num"]})
    ).status_code == 409

    async def tracked():
        data = (await client.get("/api/tracking")).json()
        d = next(x for x in data["devices"] if x["id"] == dev["id"])
        ready = d["link"] and d["battery_pct"] is not None and d["sensors"] and len(d["trail"]) >= 3
        return (data, d) if ready else None

    data, d = await wait_for(tracked, timeout=15)
    assert data["tech"] == "meshtastic" and "meshtastic" not in data
    assert d["status"] == "ok" and d["last_position"]["source"] == "meshtastic"
    assert d["link"]["route"] == "mesh"
    assert d["link"]["hops"] in ([], [{"hash": f"{sim.CAR_NUM & 0xFF:02x}", "name": "Car Router"}])
    assert 0 < d["battery_pct"] <= 100 and d["battery_v"] > 3.5
    assert d["sensors"]["temp_c"] and d["sensors"]["pressure_hpa"]

    # Its telemetry: the latest readings with their times, and the history behind the charts.
    async def full_telemetry():
        data = (await client.get("/api/tracking")).json()
        t = next(x for x in data["devices"] if x["id"] == dev["id"])["telemetry"]
        return (
            t if t and t.get("device", {}).get("uptime_s") and t.get("environment", {}).get("lux") else None
        )

    tel = await wait_for(full_telemetry, timeout=15)
    assert 0 < tel["device"]["battery_pct"] <= 100 and tel["device"]["voltage"] > 3.5
    assert tel["device"]["channel_util"] and tel["device"]["air_util_tx"] and tel["device"]["at"]
    env = tel["environment"]
    assert env["temp_c"] and env["humidity"] and env["pressure_hpa"] and env["lux"] and env["at"]
    hist = (await client.get(f"/api/tracking/devices/{dev['id']}/telemetry?hours=6")).json()
    assert hist["hours"] == 6 and hist["points"]
    assert any(p["battery_v"] and p["battery_pct"] for p in hist["points"])
    assert any(p["temp_c"] and p["humidity"] for p in hist["points"])
    assert [p["at"] for p in hist["points"]] == sorted(p["at"] for p in hist["points"])
    # On the default public channel positions come blurred (as Meshtastic sends them).
    assert d["last_position"]["lat"] == round(d["last_position"]["lat"], 2)

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

    # A private primary channel with full precision: positions arrive exact.
    r = await client.put(
        "/api/node/meshtastic/channels/0",
        headers=csrf(client),
        json={"name": "Ragnar", "key": "random", "position_precision": 32},
    )
    assert r.status_code == 200

    async def precise():
        d = next(x for x in (await client.get("/api/tracking")).json()["devices"] if x["id"] == dev["id"])
        lat = d["last_position"]["lat"]
        return d if lat != round(lat, 2) else None

    await wait_for(precise, timeout=10)

    # On MeshCore, Meshtastic trackers aren't listed (and come back with Meshtastic).
    r = await client.put(
        "/api/settings/radio", headers=csrf(client), json={"tech": "meshcore", "mode": "simulated"}
    )
    assert r.status_code == 200
    data = (await client.get("/api/tracking")).json()
    assert data["tech"] == "meshcore" and all(x["source"] == "meshcore" for x in data["devices"])

    assert (
        await client.delete(f"/api/tracking/devices/{dev['id']}", headers=csrf(client))
    ).status_code == 204
    ns = (await client.get("/api/tracking/meshtastic/nodes")).json()
    assert any(n["num"] == x1["num"] and not n["tracked"] for n in ns)


async def test_settings_from_1_1(client):
    """1.1.x kept Meshtastic trackers' connection apart, and set the radio mode to None while the
    HAT ran Meshtastic: that becomes the Meshtastic radio."""
    from app.services import app_settings

    async with db.session_factory()() as s:
        await app_settings.put_radio_config(s, app_settings.RadioConfig(mode="none"))
        await app_settings.put_raw(
            s, app_settings.MESHTASTIC_KEY, {"source": "network", "host": "10.0.0.9", "port": 4403}
        )
        assert await app_settings.migrate_meshtastic_settings(s)
        cfg = await app_settings.get_radio_config(s)
        assert (cfg.tech, cfg.mode, cfg.host, cfg.port) == ("meshtastic", "tcp", "10.0.0.9", 4403)
        assert await app_settings.get_raw(s, app_settings.MESHTASTIC_KEY) is None
        assert not await app_settings.migrate_meshtastic_settings(s)  # once

        await app_settings.put_radio_config(s, app_settings.RadioConfig(mode="none"))
        await app_settings.put_raw(s, app_settings.HAT_SWITCH_KEY, {"radio_mode": "hat"})
        await app_settings.migrate_meshtastic_settings(s)
        cfg = await app_settings.get_radio_config(s)
        assert (cfg.tech, cfg.mode) == ("meshtastic", "hat")

        # A MeshCore radio stays as it was.
        await app_settings.put_radio_config(s, app_settings.RadioConfig(mode="tcp", host="10.0.0.5"))
        await app_settings.put_raw(s, app_settings.MESHTASTIC_KEY, {"source": "simulated"})
        await app_settings.migrate_meshtastic_settings(s)
        cfg = await app_settings.get_radio_config(s)
        assert (cfg.tech, cfg.mode) == ("meshcore", "tcp")
        await s.rollback()


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
