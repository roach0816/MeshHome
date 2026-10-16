"""Remote administration of repeaters, against the simulated radio's simulated repeaters."""

from Crypto.PublicKey import ECC
from sqlalchemy import select

from app import db
from app.models import AuditEvent, Message
from app.radio import sim_repeater
from app.services import remote_admin
from tests.conftest import csrf, do_setup, wait_for
from tests.test_api import _connected_conversations


async def _repeater(client, name="Roof Repeater (sim)"):
    page = (await client.get("/api/contacts", params={"q": name})).json()
    return page["items"][0]


async def test_remote_admin_flow(client):
    sim_repeater._REPEATERS.clear()
    remote_admin._sessions.clear()
    await do_setup(client)
    await _connected_conversations(client)
    rep = await _repeater(client)
    base = f"/api/remote/{rep['id']}"

    state = (await client.get(base)).json()
    assert state["session"] is None and state["sections"] == {} and state["contact"]["kind"] == 2

    # Chat contacts are not manageable.
    chat = await _repeater(client, "Tracker (sim)")
    assert (await client.get(f"/api/remote/{chat['id']}")).status_code == 409

    # Anonymous requests work without a login.
    r = await client.post(f"{base}/request", headers=csrf(client), json={"kind": "owner"})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["text"].startswith("Roof Repeater (sim)\n")

    # Wrong password, then the factory default.
    r = await client.post(f"{base}/login", headers=csrf(client), json={"password": "nope"})
    assert r.status_code == 403
    r = await client.post(f"{base}/login", headers=csrf(client), json={"password": "password"})
    assert r.status_code == 200 and r.json()["session"]["admin"] is True

    for kind in ("status", "telemetry", "neighbours", "acl", "regions"):
        r = await client.post(f"{base}/request", headers=csrf(client), json={"kind": kind})
        assert r.status_code == 200, (kind, r.text)
    state = (await client.get(base)).json()
    assert state["sections"]["status"]["data"]["uptime"] > 0
    assert state["sections"]["neighbours"]["data"]["results_count"] >= 1
    assert state["sections"]["acl"]["data"]["acl"]
    assert "Hilltop Repeater (sim)" in state["names"].values()  # neighbour prefixes resolved
    assert "This radio (you)" in state["names"].values()  # our own ACL entry after logging in

    # CLI: get/set are remembered; replies are not archived as chat messages.
    r = await client.post(f"{base}/cli", headers=csrf(client), json={"command": "get name"})
    assert r.json()["reply"] == "> Roof Repeater (sim)"
    r = await client.post(f"{base}/cli", headers=csrf(client), json={"command": "set advert.interval 180"})
    assert r.json()["reply"] == "OK"
    state = (await client.get(base)).json()
    assert state["values"]["name"]["value"] == "Roof Repeater (sim)"
    assert state["values"]["advert.interval"]["value"] == "180"
    async with db.session_factory()() as s:
        texts = (await s.execute(select(Message.body))).scalars().all()
    assert not any("Roof Repeater (sim)" == (t or "").removeprefix("> ") for t in texts)

    # Passwords never reach the console log, the cache or the audit log.
    r = await client.post(
        f"{base}/cli", headers=csrf(client), json={"command": "set guest.password s3cret-guest"}
    )
    assert r.status_code == 200
    r = await client.post(f"{base}/cli", headers=csrf(client), json={"command": "get guest.password"})
    assert r.json()["reply"] == "> s3cret-guest"  # the caller sees it, nothing stores it
    state = (await client.get(base)).json()
    assert "s3cret-guest" not in str(state)
    async with db.session_factory()() as s:
        audit = (
            (await s.execute(select(AuditEvent.detail).where(AuditEvent.kind.like("remote.%"))))
            .scalars()
            .all()
        )
    assert audit and "s3cret-guest" not in str(audit)

    # One command per request; multi-line input is rejected.
    r = await client.post(f"{base}/cli", headers=csrf(client), json={"command": "ver\nreboot"})
    assert r.status_code == 422

    # Reboot gets no reply and is not an error.
    r = await client.post(f"{base}/cli", headers=csrf(client), json={"command": "reboot"})
    assert r.status_code == 200 and r.json()["reply"] is None
    assert (await client.get(base)).json()["session"] is None

    # After the restart the node has forgotten us: binary requests time out (shortened here).
    remote_admin.MIN_WAIT, saved = 0.5, remote_admin.MIN_WAIT
    remote_admin.MAX_WAIT, saved_max = 1.0, remote_admin.MAX_WAIT
    try:
        r = await client.post(f"{base}/request", headers=csrf(client), json={"kind": "status"})
        assert r.status_code == 504
    finally:
        remote_admin.MIN_WAIT, remote_admin.MAX_WAIT = saved, saved_max

    r = await client.post(f"{base}/logout", headers=csrf(client))
    assert r.status_code == 204
    assert (await client.get(base)).json()["session"] is None

    r = await client.delete(f"{base}/console", headers=csrf(client))
    assert r.status_code == 204
    assert (await client.get(base)).json()["console"] == []


async def test_remote_identity_change(client):
    sim_repeater._REPEATERS.clear()
    await do_setup(client)
    await _connected_conversations(client)
    rep = await _repeater(client, "Hilltop Repeater (sim)")
    base = f"/api/remote/{rep['id']}"
    await client.post(f"{base}/login", headers=csrf(client), json={"password": "password"})
    r = await client.post(f"{base}/identity", headers=csrf(client), json={"prefix": "ff"})
    assert r.status_code == 422
    r = await client.post(f"{base}/identity", headers=csrf(client), json={"prefix": "a7"})
    assert r.status_code == 200, r.text
    assert r.json()["new_public_key"].startswith("a7")
    assert r.json()["reply"].startswith("OK")
    state = (await client.get(base)).json()
    assert "set prv.key ••••" in state["console"][-2]["text"]

    async def wait_idle():
        return not remote_admin.busy()

    await wait_for(wait_idle)


def test_identity_key_matches_meshcore_format():
    from Crypto.PublicKey import ECC as ecc_module

    prv, pub = remote_admin.generate_identity("3c")
    assert pub.startswith("3c") and len(prv) == 128 and len(pub) == 64
    # The first half of the firmware's private key is the clamped Ed25519 scalar: scalar x G
    # must give back the public key.
    curve = ecc_module._curves["ed25519"]
    g = ECC.EccPoint(curve.Gx, curve.Gy, curve="ed25519")
    scalar = int.from_bytes(bytes.fromhex(prv)[:32], "little")
    assert ECC.EccKey(curve="Ed25519", point=g * scalar).export_key(format="raw").hex() == pub


def test_access_list_allows_blank_password_login():
    """A node on the repeater's access list logs in with a blank password (as the firmware does)."""
    node = sim_repeater.SimRepeater("ab" * 32, "Test Repeater", None, None)
    me = "cd" * 32
    assert node.login(me, "")["ok"] is False  # not on the list yet
    node.acl[me[:12]] = 2  # read/write
    out = node.login(me, "")
    assert out == {"ok": True, "admin": False, "permissions": 2}
    assert node.login(me, "wrong password")["ok"] is False  # a wrong password is still refused
