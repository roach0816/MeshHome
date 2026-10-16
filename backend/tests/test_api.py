import uuid

from sqlalchemy import select

from app import db
from app.models import Message
from app.radio.supervisor import supervisor
from app.services.messaging import States, mark_interrupted_sends_uncertain
from tests.conftest import HEADERS, PASSWORD, csrf, do_setup, radio_adapter, wait_for


async def test_setup_requires_token_and_runs_once(client):
    assert (await client.get("/api/setup/status")).json()["needs_setup"] is True
    bad = await client.post(
        "/api/setup",
        headers=HEADERS,
        json={"setup_token": "wrong", "username": "owner", "password": PASSWORD, "radio": {"mode": "none"}},
    )
    assert bad.status_code == 403
    await do_setup(client, mode="none")
    assert (await client.get("/api/setup/status")).json()["needs_setup"] is False
    again = await client.post(
        "/api/setup",
        headers=HEADERS,
        json={
            "setup_token": "test-setup-token",
            "username": "x",
            "password": PASSWORD,
            "radio": {"mode": "none"},
        },
    )
    assert again.status_code == 409


async def test_unauthenticated_and_csrf_rejected(client):
    await do_setup(client, mode="none")
    anon = client.__class__(transport=client._transport, base_url="http://test")
    assert (await anon.get("/api/conversations")).status_code == 401
    assert (await anon.get("/api/status")).status_code == 401
    # Signed in, but mutation without CSRF token / custom header.
    assert (await client.post("/api/radio/pause", headers=HEADERS)).status_code == 403
    assert (await client.post("/api/radio/pause", headers=csrf(client))).status_code == 200


async def test_login_logout(client):
    await do_setup(client, mode="none")
    client.cookies.clear()
    bad = await client.post(
        "/api/auth/login", headers=HEADERS, json={"username": "owner", "password": "nope"}
    )
    assert bad.status_code == 401
    ok = await client.post(
        "/api/auth/login", headers=HEADERS, json={"username": "owner", "password": PASSWORD}
    )
    assert ok.status_code == 200
    assert (await client.get("/api/auth/me")).json()["username"] == "owner"
    assert (await client.post("/api/auth/logout", headers=csrf(client))).status_code == 204
    client.cookies.clear()
    assert (await client.get("/api/auth/me")).status_code == 401


async def _connected_conversations(client):
    async def ready():
        if supervisor.status.state != "connected":
            return None
        convs = (await client.get("/api/conversations")).json()
        return convs if len(convs) >= 2 else None

    return await wait_for(ready)


async def test_simulated_receive_send_ack_and_idempotency(client):
    await do_setup(client)
    convs = await _connected_conversations(client)
    public = next(c for c in convs if c["title"] == "Public")

    # Receive
    assert (await client.post("/api/radio/simulate-incoming", headers=csrf(client))).status_code == 200
    await wait_for(lambda: _total_messages(client))

    # Send to channel, idempotently
    body = {"client_message_id": "abc12345", "body": "Hi 👋\nthere"}
    r1 = await client.post(f"/api/conversations/{public['id']}/messages", headers=csrf(client), json=body)
    r2 = await client.post(f"/api/conversations/{public['id']}/messages", headers=csrf(client), json=body)
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["id"] == r2.json()["id"]
    conflict = await client.post(
        f"/api/conversations/{public['id']}/messages",
        headers=csrf(client),
        json={"client_message_id": "abc12345", "body": "different"},
    )
    assert conflict.status_code == 409

    async def accepted():
        page = (await client.get(f"/api/conversations/{public['id']}/messages")).json()
        mine = [m for m in page["messages"] if m["id"] == r1.json()["id"]]
        return mine and mine[0]["state"] == States.ACCEPTED

    await wait_for(accepted)

    # DM to a known contact gets an ACK from the simulator (force it).
    supervisor.adapter.ack_probability = 1.0
    contacts = (await client.get("/api/contacts")).json()["items"]
    tracker = next(c for c in contacts if c["name"] == "Tracker (sim)")
    conv_id = (await client.post(f"/api/contacts/{tracker['id']}/conversation", headers=csrf(client))).json()[
        "conversation_id"
    ]
    sent = await client.post(
        f"/api/conversations/{conv_id}/messages",
        headers=csrf(client),
        json={"client_message_id": uuid.uuid4().hex, "body": "ping"},
    )

    async def acked():
        page = (await client.get(f"/api/conversations/{conv_id}/messages")).json()
        return any(
            m["id"] == sent.json()["id"] and m["state"] == States.ACKNOWLEDGED for m in page["messages"]
        )

    await wait_for(acked)


async def _total_messages(client):
    return (await client.get("/api/status")).json()["database"]["messages"] > 0


async def test_byte_budget_enforced(client):
    await do_setup(client)
    convs = await _connected_conversations(client)
    public = next(c for c in convs if c["title"] == "Public")
    too_long = "é" * (public["max_bytes"] // 2 + 1)  # 2 bytes each
    r = await client.post(
        f"/api/conversations/{public['id']}/messages",
        headers=csrf(client),
        json={"client_message_id": "budget01", "body": too_long},
    )
    assert r.status_code == 422
    assert "bytes" in r.json()["detail"]


async def test_send_rejected_while_offline(client):
    await do_setup(client)
    convs = await _connected_conversations(client)
    await client.post("/api/radio/pause", headers=csrf(client))
    await wait_for(lambda: _state_is("paused"))
    r = await client.post(
        f"/api/conversations/{convs[0]['id']}/messages",
        headers=csrf(client),
        json={"client_message_id": "offline01", "body": "hi"},
    )
    assert r.status_code == 409
    status = (await client.get("/api/status")).json()
    assert any(g["open"] for g in status["gaps"])  # pausing records a collection gap


async def _state_is(state):
    return supervisor.status.state == state


async def test_read_position_is_monotonic(client):
    await do_setup(client)
    await _connected_conversations(client)
    supervisor.simulate_incoming()
    supervisor.simulate_incoming()

    async def unread_conv():
        convs = (await client.get("/api/conversations")).json()
        return next((c for c in convs if c["unread"] > 0), None)

    conv = await wait_for(unread_conv)
    url = f"/api/conversations/{conv['id']}/read-position"
    assert (
        await client.put(url, headers=csrf(client), json={"position": conv["last_position"]})
    ).status_code == 204
    assert (await client.put(url, headers=csrf(client), json={"position": 0})).status_code == 204
    after = (await client.get(f"/api/conversations/{conv['id']}")).json()
    assert after["read_position"] == conv["last_position"]
    assert after["unread"] == 0


async def test_interrupted_send_becomes_uncertain(client):
    await do_setup(client)
    convs = await _connected_conversations(client)
    public = next(c for c in convs if c["title"] == "Public")
    await client.post("/api/radio/pause", headers=csrf(client))
    async with db.session_factory()() as s:
        s.add(
            Message(
                position=10_000,
                conversation_id=uuid.UUID(public["id"]),
                direction="out",
                body="half sent",
                state=States.SENDING,
            )
        )
        await s.commit()
        assert await mark_interrupted_sends_uncertain(s) == 1
        await s.commit()
        m = (await s.execute(select(Message).where(Message.position == 10_000))).scalar_one()
        assert m.state == States.UNCERTAIN
    # Retrying an uncertain message requires explicit confirmation.
    await client.post("/api/radio/resume", headers=csrf(client))
    await wait_for(lambda: _state_is("connected"))
    r = await client.post(f"/api/messages/{m.id}/retry", headers=csrf(client), json={})
    assert r.status_code == 409
    r = await client.post(
        f"/api/messages/{m.id}/retry", headers=csrf(client), json={"confirm_possible_duplicate": True}
    )
    assert r.status_code == 200 and r.json()["state"] == States.QUEUED


async def test_device_endpoint_never_returns_channel_secrets(client):
    await do_setup(client)
    await _connected_conversations(client)
    text = (await client.get("/api/device")).text
    assert "secret" not in text.lower()
    assert "sim-public-key!!" not in text


async def test_conversation_info_and_delete_semantics(client):
    await do_setup(client)
    convs = await _connected_conversations(client)
    public = next(c for c in convs if c["title"] == "Public")

    # Active channel: info reports "clear"; deleting clears history but keeps the conversation.
    await client.post(
        f"/api/conversations/{public['id']}/messages",
        headers=csrf(client),
        json={"client_message_id": "info0001", "body": "hello"},
    )
    info = (await client.get(f"/api/conversations/{public['id']}/info")).json()
    assert info["delete_action"] == "clear"
    assert info["channel"]["slot"] == 0 and info["stats"]["outgoing"] == 1
    r = await client.delete(f"/api/conversations/{public['id']}", headers=csrf(client))
    assert r.json() == {"action": "cleared", "messages_removed": 1}
    page = (await client.get(f"/api/conversations/{public['id']}/messages")).json()
    assert page["messages"] == []
    assert any(c["id"] == public["id"] for c in (await client.get("/api/conversations")).json())

    # DM: info includes the contact's full key; deleting removes the conversation entirely.
    contacts = (await client.get("/api/contacts")).json()["items"]
    tracker = next(c for c in contacts if c["name"] == "Tracker (sim)")
    conv_id = (await client.post(f"/api/contacts/{tracker['id']}/conversation", headers=csrf(client))).json()[
        "conversation_id"
    ]
    info = (await client.get(f"/api/conversations/{conv_id}/info")).json()
    assert info["delete_action"] == "delete"
    assert info["contact"]["public_key"] == tracker["public_key"]
    assert (await client.delete(f"/api/conversations/{conv_id}", headers=HEADERS)).status_code == 403  # CSRF
    r = await client.delete(f"/api/conversations/{conv_id}", headers=csrf(client))
    assert r.json()["action"] == "deleted"
    assert (await client.get(f"/api/conversations/{conv_id}")).status_code == 404
    # The contact itself is untouched and a DM can be reopened.
    assert any(c["id"] == tracker["id"] for c in (await client.get("/api/contacts")).json()["items"])


async def test_map_nodes_and_settings(client):
    await do_setup(client)
    await _connected_conversations(client)
    data = (await client.get("/api/map")).json()
    kinds = {n["kind"] for n in data["nodes"]}
    assert {1, 2, 3, 4} <= kinds  # companions, repeaters, room server, sensor
    assert data["without_location"] == 1  # the simulated node that shares no position
    assert len(data["gateways"]) == 1 and data["gateways"][0]["live"] is True
    assert data["tiles"]["tile_url"].startswith("https://tile.openstreetmap.org/")
    bad = await client.put(
        "/api/settings/map", headers=csrf(client), json={"tile_url": "http://x/{z}/{x}/{y}.png"}
    )
    assert bad.status_code == 422
    ok = await client.put(
        "/api/settings/map",
        headers=csrf(client),
        json={
            "tile_url": "https://tiles.example.net/{z}/{x}/{y}.png",
            "attribution": "Example",
            "max_zoom": 18,
        },
    )
    assert ok.status_code == 200
    assert (await client.get("/api/map")).json()["tiles"]["attribution"] == "Example"


async def test_username_change_and_release_url(client):
    await do_setup(client, mode="none")
    status_ = (await client.get("/api/setup/status")).json()
    assert status_["release_url"].endswith(f"/releases/tag/v{status_['version']}")
    bad = await client.put(
        "/api/auth/username", headers=csrf(client), json={"username": "x", "current_password": "nope"}
    )
    assert bad.status_code == 403
    ok = await client.put(
        "/api/auth/username", headers=csrf(client), json={"username": "admin2", "current_password": PASSWORD}
    )
    assert ok.status_code == 200 and ok.json()["username"] == "admin2"
    client.cookies.clear()
    r = await client.post(
        "/api/auth/login", headers=HEADERS, json={"username": "admin2", "password": PASSWORD}
    )
    assert r.status_code == 200


async def test_node_configuration_on_simulated_radio(client):
    from app.radio import simulated

    simulated._SIM_STATE = None  # fresh simulated node
    await do_setup(client)
    await _connected_conversations(client)
    cfg = (await client.get("/api/radio/config")).json()
    assert cfg["simulated"] is True and cfg["identity"]["name"] == "Home (simulated)"
    assert "secret" not in str(cfg).lower()

    # Identity: rename and move; device info and map follow.
    r = await client.put(
        "/api/radio/config/identity",
        headers=csrf(client),
        json={"name": "Basecamp", "lat": 40.1, "lon": -105.1, "share_location": True},
    )
    assert r.status_code == 200 and r.json()["identity"]["name"] == "Basecamp"
    assert (await client.get("/api/device")).json()["radio"]["name"] == "Basecamp"
    too_long = await client.put(
        "/api/radio/config/identity", headers=csrf(client), json={"name": "x" * 40, "share_location": False}
    )
    assert too_long.status_code == 422

    # Radio: firmware ranges enforced before anything is sent.
    radio = {"freq_mhz": 915.0, "bw_khz": 250, "sf": 10, "cr": 5, "tx_power_dbm": 20}
    assert (await client.put("/api/radio/config/radio", headers=csrf(client), json=radio)).status_code == 200
    for bad in ({"sf": 13}, {"bw_khz": 100}, {"tx_power_dbm": 23}):
        assert (
            await client.put("/api/radio/config/radio", headers=csrf(client), json={**radio, **bad})
        ).status_code == 422

    # Channels: random key returned exactly once; a new channel gets a conversation.
    r = await client.put(
        "/api/radio/channels/3", headers=csrf(client), json={"name": "Family", "key_mode": "random"}
    )
    body = r.json()
    assert len(body["new_key"]["hex"]) == 32
    assert any(c["slot"] == 3 and c["key"] == "private" for c in body["config"]["channels"])
    assert "new_key" not in (await client.get("/api/radio/config")).text
    assert any(c["title"] == "Family" for c in (await client.get("/api/conversations")).json())
    r = await client.put(
        "/api/radio/channels/4", headers=csrf(client), json={"name": "#hikers", "key_mode": "hashtag"}
    )
    assert any(c["slot"] == 4 and c["key"] == "hashtag" for c in r.json()["config"]["channels"])
    r = await client.put(
        "/api/radio/channels/3", headers=csrf(client), json={"name": "Family Chat", "key_mode": "keep"}
    )
    assert any(c["slot"] == 3 and c["name"] == "Family Chat" for c in r.json()["config"]["channels"])
    r = await client.delete("/api/radio/channels/4", headers=csrf(client))
    assert not any(c["slot"] == 4 and c["name"] for c in r.json()["channels"])

    # Telemetry, behaviour, tuning, custom variables, actions.
    assert (
        await client.put(
            "/api/radio/config/telemetry",
            headers=csrf(client),
            json={"base": 2, "location": 0, "environment": 1},
        )
    ).json()["telemetry"] == {"base": 2, "location": 0, "environment": 1}
    assert (
        await client.put(
            "/api/radio/config/behavior",
            headers=csrf(client),
            json={
                "auto_add_contacts": False,
                "multi_acks": 1,
                "path_hash_mode": 1,
                "default_flood_scope": "#local",
            },
        )
    ).json()["behavior"]["default_flood_scope"] == "local"
    assert (
        await client.put(
            "/api/radio/config/tuning", headers=csrf(client), json={"rx_delay": 25, "airtime_factor": 1}
        )
    ).status_code == 422
    assert (
        await client.put("/api/radio/custom-vars", headers=csrf(client), json={"key": "gps", "value": "1"})
    ).json()["custom_vars"]["gps"] == "1"
    assert (
        await client.post("/api/radio/actions/advert", headers=csrf(client), json={"flood": True})
    ).status_code == 200
    assert (await client.post("/api/radio/actions/reboot", headers=csrf(client))).status_code == 200
    await wait_for(lambda: _state_is("connected"), timeout=15)  # reconnects after the simulated reboot
    simulated._SIM_STATE = None


async def test_add_channel_flows_and_region_scope(client):
    from app.radio import simulated
    from app.radio.base import PUBLIC_CHANNEL_KEY, hashtag_key

    simulated._SIM_STATE = None
    await do_setup(client)
    await _connected_conversations(client)

    # Hashtag key preview: derived from the name, so it may be shown.
    r = await client.get("/api/radio/channels/hashtag-key", params={"name": "#hikers"})
    assert r.json()["hex"] == hashtag_key("#hikers").hex()
    assert (await client.get("/api/radio/channels/hashtag-key", params={"name": "#"})).status_code == 422

    # Join a hashtag channel with a region scope: first free slot, key returned for sharing.
    r = await client.post(
        "/api/radio/channels",
        headers=csrf(client),
        json={"name": "#hikers", "key_mode": "hashtag", "flood_scope": "#boulder"},
    )
    assert r.status_code == 200, r.text
    added = r.json()
    assert added["slot"] == 2 and added["share"]["hex"] == hashtag_key("#hikers").hex()
    assert next(c for c in added["config"]["channels"] if c["slot"] == 2)["flood_scope"] == "boulder"
    info = (await client.get(f"/api/conversations/{added['conversation_id']}/info")).json()
    assert info["channel"]["flood_scope"] == "boulder"

    # The same channel again is refused (one key, one slot).
    dup = await client.post(
        "/api/radio/channels", headers=csrf(client), json={"name": "#hikers", "key_mode": "hashtag"}
    )
    assert dup.status_code == 409 and "already on the radio" in dup.json()["detail"]

    # Public, a new private channel (key returned once), and joining a private channel.
    r = await client.post(
        "/api/radio/channels", headers=csrf(client), json={"name": "Public", "key_mode": "public"}
    )
    assert r.json()["share"]["hex"] == PUBLIC_CHANNEL_KEY.hex()
    r = await client.post(
        "/api/radio/channels", headers=csrf(client), json={"name": "Family", "key_mode": "random"}
    )
    assert len(r.json()["share"]["hex"]) == 32
    r = await client.post(
        "/api/radio/channels",
        headers=csrf(client),
        json={"name": "Club", "key_mode": "custom", "key": "00112233445566778899aabbccddeeff"},
    )
    assert r.status_code == 200 and "share" not in r.json()
    bad = await client.post(
        "/api/radio/channels", headers=csrf(client), json={"name": "#x y", "key_mode": "hashtag"}
    )
    assert bad.status_code == 422

    # Sending on the scoped channel passes the scope to the radio for that message only.
    body = {"client_message_id": uuid.uuid4().hex, "body": "trail is clear"}
    sent = await client.post(
        f"/api/conversations/{added['conversation_id']}/messages", headers=csrf(client), json=body
    )
    assert sent.status_code == 200

    async def scoped_send():
        return simulated.sim_state().get("last_channel_scope") == "boulder"

    await wait_for(scoped_send)

    # Clearing the scope.
    r = await client.put("/api/radio/channels/2/scope", headers=csrf(client), json={"flood_scope": ""})
    assert r.status_code == 200 and r.json()["flood_scope"] is None
    assert (
        await client.put("/api/radio/channels/7/scope", headers=csrf(client), json={"flood_scope": "x"})
    ).status_code == 404

    # Fill the remaining slots, then adding reports that the radio is full.
    for i in range(2):
        r = await client.post(
            "/api/radio/channels", headers=csrf(client), json={"name": f"#fill{i}", "key_mode": "hashtag"}
        )
        assert r.status_code == 200
    full = await client.post(
        "/api/radio/channels", headers=csrf(client), json={"name": "#onemore", "key_mode": "hashtag"}
    )
    assert full.status_code == 409 and "slots" in full.json()["detail"]
    simulated._SIM_STATE = None


async def test_node_configuration_requires_connection(client):
    await do_setup(client, mode="none")
    r = await client.get("/api/radio/config")
    assert r.status_code == 409


async def test_contacts_search_filter_paginate(client):
    from app.radio import simulated

    simulated._SIM_STATE = None
    await do_setup(client)
    await _connected_conversations(client)
    page = (await client.get("/api/contacts?page_size=10")).json()
    assert page["total"] == len(simulated.SIM_CONTACTS) and len(page["items"]) == page["total"]
    # Sorted by last heard (most recent first).
    heard = [c["last_advert_at"] for c in page["items"]]
    assert heard == sorted(heard, reverse=True)
    assert (await client.get("/api/contacts?q=repeat")).json()["total"] == 2
    assert (await client.get("/api/contacts?kind=2")).json()["total"] == 2
    assert (await client.get("/api/contacts?page_size=10&page=2")).json()["items"] == []
    assert (await client.get("/api/contacts?page_size=7")).status_code == 422


async def test_contacts_sort_direction_and_favorites_first(client):
    from app.radio import simulated

    simulated._SIM_STATE = None
    await do_setup(client)
    await _connected_conversations(client)

    async def names(**params):
        page = (await client.get("/api/contacts", params={"page_size": 10, **params})).json()
        return [(c["alias"] or c["name"]).lower() for c in page["items"]], page["items"]

    asc, _ = await names(sort="name")
    desc, _ = await names(sort="name", order="desc")
    assert asc == sorted(asc) and desc == list(reversed(asc))
    _, oldest = await names(sort="last_heard", order="asc")
    heard = [c["last_advert_at"] for c in oldest if c["last_advert_at"]]
    assert heard == sorted(heard)
    _, by_kind = await names(sort="kind", order="desc")
    assert [c["kind"] for c in by_kind] == sorted((c["kind"] for c in by_kind), reverse=True)

    hiker = next(c for c in oldest if c["name"].startswith("Hiker"))
    r = await client.post(
        f"/api/contacts/{hiker['id']}/favorite", headers=csrf(client), json={"favorite": True}
    )
    assert r.status_code == 200
    first, _ = await names(sort="name", favorites_first="true")
    assert first[0].startswith("hiker") and first[1:] == [n for n in asc if not n.startswith("hiker")]
    plain, _ = await names(sort="name")
    assert plain == asc


async def test_contact_actions_and_block(client):
    from app.radio import simulated

    simulated._SIM_STATE = None
    await do_setup(client)
    await _connected_conversations(client)
    items = (await client.get("/api/contacts")).json()["items"]
    tracker = next(c for c in items if c["name"] == "Tracker (sim)")
    rpt = next(c for c in items if c["kind"] == 2)
    cid = tracker["id"]

    # Favourite is the radio's flag; it round-trips through a contact refresh.
    r = await client.post(f"/api/contacts/{cid}/favorite", headers=csrf(client), json={"favorite": True})
    assert r.json()["favorite"] is True
    assert (await client.get("/api/contacts?show=favorites")).json()["total"] == 1

    # Path: via one repeater (shortened to the hash size), then reset to flood.
    r = await client.put(
        f"/api/contacts/{cid}/path", headers=csrf(client), json={"hops": [rpt["public_key"]]}
    )
    assert r.status_code == 204
    d = (await client.get(f"/api/contacts/{cid}")).json()
    assert d["path_len"] == 1 and d["path_hops"] == [rpt["public_key"][: 2 * d["path_hash_size"]]]
    assert (await client.post(f"/api/contacts/{cid}/reset-path", headers=csrf(client))).status_code == 204
    assert (await client.get(f"/api/contacts/{cid}")).json()["path_len"] == -1
    bad = await client.put(f"/api/contacts/{cid}/path", headers=csrf(client), json={"hops": ["zz"]})
    assert bad.status_code == 422

    # Share and export.
    assert (await client.post(f"/api/contacts/{cid}/share", headers=csrf(client))).status_code == 200
    assert (await client.get(f"/api/contacts/{cid}/export")).json()["uri"].startswith("meshcore://")

    # Block: a DM from the contact is archived but hidden, not unread; unblock restores it.
    conv_id = (await client.post(f"/api/contacts/{cid}/conversation", headers=csrf(client))).json()[
        "conversation_id"
    ]
    await client.patch(f"/api/contacts/{cid}", headers=csrf(client), json={"blocked": True})
    from app.radio.base import IncomingMessage

    (await radio_adapter()).inject(
        IncomingMessage(
            kind="dm", text="spam spam", pubkey_prefix=tracker["public_key"][:12], sender_timestamp=1
        )
    )

    async def archived():
        async with db.session_factory()() as s:
            return (await s.execute(select(Message).where(Message.body == "spam spam"))).scalar_one_or_none()

    m = await wait_for(archived)
    assert m.suppressed is True
    assert (await client.get(f"/api/conversations/{conv_id}/messages")).json()["messages"] == []
    assert not any(c["id"] == conv_id for c in (await client.get("/api/conversations")).json())
    assert (await client.get("/api/search?q=spam")).json() == []
    assert (await client.get("/api/contacts?show=blocked")).json()["total"] == 1
    await client.patch(f"/api/contacts/{cid}", headers=csrf(client), json={"blocked": False})
    msgs = (await client.get(f"/api/conversations/{conv_id}/messages")).json()["messages"]
    assert [x["body"] for x in msgs] == ["spam spam"]

    # Remove from radio: kept in the archive under "removed".
    assert (await client.delete(f"/api/contacts/{cid}", headers=csrf(client))).status_code == 204
    assert (await client.get("/api/contacts?show=removed")).json()["items"][0]["id"] == cid
    assert (await client.post(f"/api/contacts/{cid}/share", headers=csrf(client))).status_code == 409
    simulated._SIM_STATE = None


async def test_notification_settings_and_sound_override(client):
    await do_setup(client)
    convs = await _connected_conversations(client)
    assert (await client.get("/api/settings/notifications")).json() == {"sound": "dms"}
    r = await client.put("/api/settings/notifications", headers=csrf(client), json={"sound": "all"})
    assert r.json()["sound"] == "all"
    assert (
        await client.put("/api/settings/notifications", headers=csrf(client), json={"sound": "x"})
    ).status_code == 422
    cid = convs[0]["id"]
    await client.patch(f"/api/conversations/{cid}", headers=csrf(client), json={"sound": "off"})
    assert (await client.get(f"/api/conversations/{cid}")).json()["sound"] == "off"
    await client.patch(f"/api/conversations/{cid}", headers=csrf(client), json={"sound": "default"})
    assert (await client.get(f"/api/conversations/{cid}")).json()["sound"] is None


async def test_contacts_follow_radio_advert_pushes(client, monkeypatch):
    """A new contact on the radio shows up after its advert push, without a manual refresh."""
    from app.radio import simulated
    from app.radio import supervisor as sup_module
    from app.radio.base import RadioContact

    monkeypatch.setattr(sup_module, "CONTACT_PUSH_SETTLE_SECONDS", 0.1)
    monkeypatch.setattr(sup_module, "CONTACT_PUSH_MIN_INTERVAL", 0.1)
    simulated._SIM_STATE = None
    await do_setup(client)
    await _connected_conversations(client)
    newcomer = RadioContact(public_key=simulated._key("newcomer"), name="Newcomer (sim)", kind=1)
    monkeypatch.setattr(simulated, "SIM_CONTACTS", [*simulated.SIM_CONTACTS, newcomer])
    await (await radio_adapter()).on_contacts_changed()

    async def listed():
        page = (await client.get("/api/contacts", params={"q": "Newcomer"})).json()
        return page["total"] == 1

    await wait_for(listed)


async def test_dm_star_is_the_contact_favourite(client):
    """One star per person: starring a DM sets the contact's favourite on the radio, and back."""
    from app.radio import simulated

    simulated._SIM_STATE = None
    await do_setup(client)
    await _connected_conversations(client)
    contact = (await client.get("/api/contacts", params={"q": "Neighbor"})).json()["items"][0]
    conv_id = (await client.post(f"/api/contacts/{contact['id']}/conversation", headers=csrf(client))).json()[
        "conversation_id"
    ]

    async def conv():
        return (await client.get(f"/api/conversations/{conv_id}")).json()

    async def contact_fav():
        return (await client.get(f"/api/contacts/{contact['id']}")).json()["favorite"]

    assert (await conv())["favorite"] is False
    r = await client.patch(f"/api/conversations/{conv_id}", headers=csrf(client), json={"favorite": True})
    assert r.status_code == 204
    assert (await conv())["favorite"] is True and await contact_fav() is True

    r = await client.post(
        f"/api/contacts/{contact['id']}/favorite", headers=csrf(client), json={"favorite": False}
    )
    assert r.status_code == 200
    assert (await conv())["favorite"] is False and await contact_fav() is False

    # Channels keep their own star.
    public = next(c for c in (await client.get("/api/conversations")).json() if c["title"] == "Public")
    await client.patch(f"/api/conversations/{public['id']}", headers=csrf(client), json={"favorite": True})
    assert (await client.get(f"/api/conversations/{public['id']}")).json()["favorite"] is True
