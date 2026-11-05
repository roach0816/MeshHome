"""Preferred repeater (services/preferred_route.py): direct sends go on a remembered route through
one repeater, and around it (by the usual flood retry) only when that doesn't work."""

import time
import uuid
from datetime import timedelta

from app.models import Contact, utcnow
from app.radio import packets, simulated
from app.radio.supervisor import supervisor
from app.services import preferred_route as pr
from tests.conftest import csrf, do_setup, radio_adapter, wait_for
from tests.test_api import _connected_conversations
from tests.test_retries import _message, _send, _sent, _settled, quick_acks  # noqa: F401 - fixture

ROOF = simulated._key("roof")
HILL = simulated._key("hilltop")
R, H = ROOF[:2], HILL[:2]


# ---- the rules ----------------------------------------------------------------------------


def _contact(**routing) -> Contact:
    return Contact(public_key="c" * 64, routing=routing or None)


def test_route_from_flood():
    assert pr.route_from_flood(["b2", R], ROOF) == [R, "b2"]  # reversed: this radio's side first
    assert pr.route_from_flood([R, "b2"], ROOF) is None  # the repeater wasn't the last hop
    assert pr.route_from_flood([], ROOF) is None


def test_route_for_radio():
    now = utcnow()
    c = _contact(hops=[ROOF[:4], "b2c3"])
    assert pr.route_for_radio(c, ROOF, 1, now) == R + "b2"  # shortened to the radio's hash size
    assert pr.route_for_radio(c, ROOF, 2, now) == ROOF[:4] + "b2c3"
    assert pr.route_for_radio(c, ROOF, 3, now) is None  # hops too short for 3-byte hashes
    assert pr.route_for_radio(c, HILL, 1, now) is None  # remembered through another repeater
    assert pr.route_for_radio(_contact(), ROOF, 1, now) is None
    assert pr.route_for_radio(_contact(hops=[R], off=True), ROOF, 1, now) is None
    paused = _contact(hops=[R], failed_at=(now - timedelta(minutes=5)).isoformat())
    assert pr.route_for_radio(paused, ROOF, 1, now) is None
    assert pr.route_for_radio(paused, ROOF, 1, now + pr.COOLDOWN) == R


def test_remember():
    now = utcnow()
    c = _contact(hops=[R], failed_at=now.isoformat())
    assert not pr.remember(c, [R], "radio", now)  # the radio echoing the route changes nothing
    assert c.routing["failed_at"]
    assert pr.remember(c, [R], "message", now)  # heard again: re-dated, still paused
    assert c.routing["failed_at"] and c.routing["source"] == "message"
    assert pr.remember(c, [R, "b2"], "advert", now)  # a different route ends the pause
    assert "failed_at" not in c.routing and c.routing["hops"] == [R, "b2"]
    c.routing = {**c.routing, "hops": [ROOF[:4], "b2c3"]}
    pr.remember(c, [R, "b2"], "message", now)
    assert c.routing["hops"] == [ROOF[:4], "b2c3"]  # the longer prefixes are kept
    pr.set_enabled(c, False)
    assert pr.remember(c, [R], "manual", now) and pr.enabled(c)  # set by hand: on again


# ---- in the running app -------------------------------------------------------------------


async def _setup(client, *, acks: float):
    simulated._SIM_STATE = None
    await do_setup(client)
    await _connected_conversations(client)
    adapter = await radio_adapter()
    adapter.ack_probability = acks

    async def no_reply(*_a, **_k):  # replies would teach routes mid-test
        return None

    adapter._auto_reply = no_reply
    contacts = (await client.get("/api/contacts")).json()["items"]
    by_name = {c["name"]: c for c in contacts}
    r = await client.put("/api/settings/routing", headers=csrf(client), json={"preferred_repeater": ROOF})
    assert r.status_code == 200, r.text
    return by_name


async def _detail(client, c) -> dict:
    return (await client.get(f"/api/contacts/{c['id']}")).json()


async def _advert(public_key: str, hops: list[str]) -> None:
    payload = bytes.fromhex(public_key) + uuid.uuid4().bytes * 4
    await supervisor._on_rx_packet(
        packets.build(packets.TYPE_ADVERT, payload, [bytes.fromhex(h) for h in hops]), 5.0, -90
    )


async def _conversation(client, c) -> str:
    r = await client.post(f"/api/contacts/{c['id']}/conversation", headers=csrf(client))
    return r.json()["conversation_id"]


async def test_sends_go_through_the_repeater(client):
    by_name = await _setup(client, acks=1.0)
    tracker = by_name["Tracker (sim)"]
    # An advert from the tracker reached this radio through the hilltop, then the roof.
    await _advert(tracker["public_key"], [H, R])
    d = await _detail(client, tracker)
    assert d["preferred_route"]["hops"] == [R, H]
    assert d["preferred_route"]["source"] == "advert"
    assert d["preferred_route"]["repeater_name"] == "Roof Repeater (sim)"
    # Meanwhile the radio learned a route that skips the roof.
    simulated.sim_state()["contact_paths"][tracker["public_key"]] = (1, H)

    conv_id = await _conversation(client, tracker)
    mid = await _send(client, conv_id, "via the roof")
    m = await _settled(client, conv_id, mid, "acknowledged")
    assert [d["path"] for d in _sent("via the roof")] == [R + H]
    assert m["meta"]["delivery"]["via"] == "Roof Repeater (sim)"


async def test_falls_back_and_pauses(client, quick_acks):  # noqa: F811
    by_name = await _setup(client, acks=0.0)
    tracker = by_name["Tracker (sim)"]
    r = await client.put(f"/api/contacts/{tracker['id']}/path", headers=csrf(client), json={"hops": [R]})
    assert r.status_code == 204
    assert (await _detail(client, tracker))["preferred_route"]["source"] == "manual"
    simulated.sim_state()["contact_paths"][tracker["public_key"]] = (1, H)

    conv_id = await _conversation(client, tracker)
    mid = await _send(client, conv_id, "anyone?")
    m = await _settled(client, conv_id, mid, "no_ack")
    assert [(d["path"], d["flood"]) for d in _sent("anyone?")] == [(R, False), (R, False), (None, True)]
    assert m["meta"]["delivery"]["route"] == "flood"
    pref = (await _detail(client, tracker))["preferred_route"]
    assert pref["paused_until"] is not None
    assert pref["repeater_available"]  # one contact's failure doesn't condemn the repeater

    # While paused, the radio's own route is used (none now, after the flood): no roof.
    mid = await _send(client, conv_id, "again")
    await _settled(client, conv_id, mid, "no_ack")
    assert all(d["path"] is None for d in _sent("again"))

    # Turning it back on for the contact means "try it now".
    r = await client.put(
        f"/api/contacts/{tracker['id']}/preferred-route", headers=csrf(client), json={"enabled": True}
    )
    assert r.status_code == 204
    assert (await _detail(client, tracker))["preferred_route"]["paused_until"] is None


async def test_off_for_a_contact(client):
    by_name = await _setup(client, acks=1.0)
    tracker = by_name["Tracker (sim)"]
    await _advert(tracker["public_key"], [R])
    simulated.sim_state()["contact_paths"][tracker["public_key"]] = (1, H)
    r = await client.put(
        f"/api/contacts/{tracker['id']}/preferred-route", headers=csrf(client), json={"enabled": False}
    )
    assert r.status_code == 204
    conv_id = await _conversation(client, tracker)
    mid = await _send(client, conv_id, "your way")
    m = await _settled(client, conv_id, mid, "acknowledged")
    assert [d["path"] for d in _sent("your way")] == [H]
    assert "via" not in m["meta"]["delivery"]


async def test_a_route_set_by_hand_elsewhere_turns_it_off(client):
    by_name = await _setup(client, acks=1.0)
    tracker = by_name["Tracker (sim)"]
    await _advert(tracker["public_key"], [R])
    r = await client.put(f"/api/contacts/{tracker['id']}/path", headers=csrf(client), json={"hops": [H]})
    assert r.status_code == 204
    pref = (await _detail(client, tracker))["preferred_route"]
    assert pref["enabled"] is False and pref["hops"] == [R]


async def test_learns_the_radios_route(client):
    by_name = await _setup(client, acks=1.0)
    # The radio learned a route through the roof (from a reply to a flood).
    simulated.sim_state()["contact_paths"][simulated._key("neighbor")] = (2, R + H)
    await supervisor.refresh_contacts()
    pref = (await _detail(client, by_name["Neighbor (sim)"]))["preferred_route"]
    assert pref["hops"] == [R, H] and pref["source"] == "radio"
    # The repeater itself has no preferred route; nor does anyone without a setting.
    assert (await _detail(client, by_name["Roof Repeater (sim)"]))["preferred_route"] is None
    await client.put("/api/settings/routing", headers=csrf(client), json={"preferred_repeater": None})
    assert (await _detail(client, by_name["Neighbor (sim)"]))["preferred_route"] is None


async def test_learns_from_a_direct_message(client):
    by_name = await _setup(client, acks=1.0)
    hiker = by_name["Hiker (sim, no location)"]
    from app.radio.base import IncomingMessage

    adapter = await radio_adapter()
    adapter._rng.shuffle = lambda routes: routes.sort(key=len, reverse=True)  # [hill, roof] first
    adapter.inject(IncomingMessage(kind="dm", text="hi", pubkey_prefix=hiker["public_key"][:12]))

    async def learned():
        return (await _detail(client, hiker))["preferred_route"]["hops"] == [R, H]

    await wait_for(learned)


async def test_repeater_unavailable_until_heard(client):
    await _setup(client, acks=1.0)
    await supervisor._preferred_route_failed(simulated._key("tracker"))
    assert supervisor.repeater_available
    await supervisor._preferred_route_failed(simulated._key("neighbor"))
    assert not supervisor.repeater_available
    # Anything relayed by the roof shows it's back.
    await supervisor._on_rx_packet(
        packets.build(packets.TYPE_GRP_TXT, b"\x11" * 20, [bytes.fromhex(R)]), 1, -80
    )
    assert supervisor.repeater_available


async def test_goes_around_a_repeater_that_is_down(client):
    by_name = await _setup(client, acks=1.0)
    tracker = by_name["Tracker (sim)"]
    await _advert(tracker["public_key"], [R])
    conv_id = await _conversation(client, tracker)
    mid = await _send(client, conv_id, "first")
    await _settled(client, conv_id, mid, "acknowledged")
    assert simulated.sim_state()["contact_paths"][tracker["public_key"]] == (1, R)
    supervisor._repeater_down_until = time.monotonic() + 3600
    mid = await _send(client, conv_id, "second")
    await _settled(client, conv_id, mid, "acknowledged")
    assert [d["path"] for d in _sent("second")] == [None]  # the roof route was dropped: flood


async def test_remote_requests_use_it(client):
    await _setup(client, acks=1.0)
    await _advert(HILL, [R])
    simulated.sim_state()["contact_paths"].pop(HILL, None)
    ticket = await supervisor.remote_send(HILL, "owner")
    assert ticket.via == ROOF
    assert simulated.sim_state()["contact_paths"][HILL] == (1, R)
    await supervisor.remote_wait(ticket, 5)
