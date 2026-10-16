"""Adding contacts from a QR code or typed in (POST /api/contacts/import), on the simulated radio."""

from urllib.parse import quote

from tests.conftest import csrf, do_setup
from tests.test_api import _connected_conversations

KEY = "9cd8fcf22a47333b591d96a2b848b73f457b1bb1a3ea2453a885f9e5787765b1"


async def _import(client, **body):
    return await client.post("/api/contacts/import", headers=csrf(client), json=body)


async def test_add_contact_from_qr_code_and_by_hand(client):
    await do_setup(client)
    await _connected_conversations(client)

    uri = f"meshcore://contact/add?name={quote('Trail Buddy')}&public_key={KEY}&type=1"
    r = await _import(client, uri=uri)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["added"] is True
    c = out["contact"]
    assert (c["name"], c["public_key"], c["kind"], c["on_radio"]) == ("Trail Buddy", KEY, 1, True)
    assert c["last_advert_at"] is None  # nothing heard from it yet

    # Scanning it again leaves the existing contact alone.
    r = await _import(client, uri=uri)
    assert r.json()["added"] is False and r.json()["contact"]["id"] == c["id"]

    # It behaves like any other contact: listed, favourite, open a conversation.
    names = [x["name"] for x in (await client.get("/api/contacts?q=Trail")).json()["items"]]
    assert names == ["Trail Buddy"]
    r = await client.post(f"/api/contacts/{c['id']}/favorite", headers=csrf(client), json={"favorite": True})
    assert r.status_code == 200 and r.json()["favorite"] is True
    assert (
        await client.post(f"/api/contacts/{c['id']}/conversation", headers=csrf(client))
    ).status_code == 200

    # Typed in: a repeater, key in capitals.
    r = await _import(client, public_key=("ab" * 32).upper(), name="  Ridge Repeater ", kind=2)
    assert r.status_code == 200, r.text
    assert (r.json()["contact"]["name"], r.json()["contact"]["kind"]) == ("Ridge Repeater", 2)


async def test_bad_contact_codes_are_refused(client):
    await do_setup(client)
    await _connected_conversations(client)
    cases = [
        ({"uri": "meshcore://channel/add?name=x&secret=00"}, "isn't a MeshCore contact code"),
        ({"uri": f"meshcore://contact/add?name=X&public_key={KEY}&type=nine"}, "invalid type"),
        ({"public_key": "1234", "name": "Short"}, "64 hexadecimal"),
        ({"public_key": KEY, "name": "   "}, "Enter the contact's name"),
        ({"public_key": KEY, "name": "é" * 16}, "too long"),
        ({"public_key": KEY, "name": "X", "kind": 9}, None),
    ]
    for body, message in cases:
        r = await _import(client, **body)
        assert r.status_code == 422, (body, r.text)
        if message:
            assert message in r.text, (body, r.text)

    own = (await client.get("/api/device")).json()["radio"]["public_key"]
    r = await _import(client, public_key=own, name="Me")
    assert r.status_code == 409 and "own contact code" in r.text
