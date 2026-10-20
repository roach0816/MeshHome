"""Signed channel messages: reading the signature after the text (services/signed_messages.py),
opening channel packets (radio/packets.py decrypt_group), and recording it with the message."""

import hashlib
import time

import pytest
from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa

from app.radio import identity, packets
from app.radio.base import IncomingMessage
from app.services import signed_messages
from tests.conftest import csrf, do_setup, radio_adapter, wait_for
from tests.test_api import _connected_conversations

# Decrypted channel messages captured on the UK public mesh (October 2026), from the
# MeshCore-SignatureFreeZone detector tests: three signed by MeshOS, one unsigned.
REAL_SIGNED = [
    "0282ca6a00416e6479204b20e29c853a206576656e696e6720616c6c0053012f7708e2ab973a551f9d9d4d39f27cd717d05db31b"
    "5518adc7db997a6c779b6f7f28c7d9c2a830c95160539ddd0486ea16b8a51ac7c6f1e9da4ed8ddcfeba561a2892268cea51b0800"
    "0000000000000000",
    "f686ca6a00416e6479204b20e29c853a206c617374206f6e6520696e20756b2077617320616d617a696e672068657265005301"
    "2f7708e2ab973a5573cd7e05df3022fb9aae762b11caad2db23633d887af6f4ed8a33ffc9a62931e01b6b702a4bca6dbd6c4a8b7"
    "0cbf7414e6eb89bdf5993f31769869436be81d080000000000",
    "078dca6a00416e64794b3a20405b416e6479202d2036443445204d6f62696c655d20f09f918d0053012f7708e2ab973a55c4bf0a"
    "0b9feefc70bd0429cab83ccb0cb31b7c86dec6d7daaf310b1b0530b0dade96a87bdf7e482264827361788208df57b34f8301fc"
    "df873e7362f8a2ef4c0c000000000000000000000000000000",
]
REAL_UNSIGNED = (
    "7e5fca6a00416e6479204b20e29c853a20746869732069732061206e6f6e207369676e6564206d6573736167652e0000"
)
PUBLIC_KEY = bytes.fromhex("8b3387e9c5cdea6ac9e5edbaa115cd72")  # MeshCore's well-known Public channel key


def test_parse_real_signed_messages():
    first = signed_messages.parse(bytes.fromhex(REAL_SIGNED[0]))
    assert first.text.decode() == "Andy K ✅: evening all"
    assert first.timestamp == 0x6ACA8202 and first.flags == 0
    assert first.key_id.hex() == "2f7708e2ab973a55" and len(first.signature) == 64
    for v in REAL_SIGNED[1:]:
        assert signed_messages.parse(bytes.fromhex(v)).key_id.hex() == "2f7708e2ab973a55"
    assert signed_messages.parse(bytes.fromhex(REAL_UNSIGNED)) is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b[:29] + b"\x54" + b[30:],  # marker byte changed
        lambda b: b[:-1] + b"\x01",  # padding isn't all zero
        lambda b: b[:60],  # cut short
        lambda b: b + b"\0" * 16,  # more than one block of padding
        lambda b: b"\x01\x02\x03",
        lambda b: bytes(96),
    ],
)
def test_parse_rejects_damaged_endings(mutate):
    assert signed_messages.parse(mutate(bytes.fromhex(REAL_SIGNED[0]))) is None


def test_decrypt_group_round_trip_and_checks():
    plain = bytes.fromhex(REAL_SIGNED[0]).rstrip(b"\0")
    payload = packets.encrypt_group(PUBLIC_KEY, plain)
    assert payload[0] == 0x11 and (len(payload) - 3) % 16 == 0
    opened = packets.decrypt_group(PUBLIC_KEY, payload)
    assert opened.rstrip(b"\0") == plain and len(opened) % 16 == 0
    assert signed_messages.parse(opened).text.decode() == "Andy K ✅: evening all"
    # Another channel's key, a bad MAC, or a cut-off packet don't decrypt.
    assert packets.decrypt_group(b"sim-home-secret!", payload) is None
    assert packets.decrypt_group(PUBLIC_KEY, payload[:1] + bytes([payload[1] ^ 1]) + payload[2:]) is None
    assert packets.decrypt_group(PUBLIC_KEY, payload[:-1]) is None


def test_signed_bytes_layout_and_verify():
    # As MeshOS builds them: tag, SHA-256(channel key)[:16], LE timestamp, "name: text".
    key = ECC.generate(curve="ed25519")
    pub = key.public_key().export_key(format="raw")
    secret = hashlib.sha256(b"#test").digest()[:16]
    chan = signed_messages.channel_tag(secret)
    assert chan == hashlib.sha256(secret).digest()[:16]
    data = signed_messages.signed_bytes(chan, 0x6AD77B48, b"cro: testing again")
    assert data == b"MeshOS signed channel v1" + chan + bytes.fromhex("487bd76a") + b"cro: testing again"
    sig = eddsa.new(key, "rfc8032").sign(data)
    assert signed_messages.verify(pub.hex(), chan, 0x6AD77B48, b"cro: testing again", sig)
    assert not signed_messages.verify(pub.hex(), chan, 0x6AD77B48, b"cro: testing agai", sig)
    assert not signed_messages.verify(
        pub.hex(), signed_messages.channel_tag(b"other channel!!!"), 0x6AD77B48, b"cro: testing again", sig
    )  # lifted onto another channel
    assert not signed_messages.verify("00" * 32, chan, 0x6AD77B48, b"cro: testing again", sig)


def test_radio_identity_signs_like_rfc8032():
    seed = hashlib.sha256(b"seed").digest()
    private = identity.expand_seed(seed)
    want = eddsa.new(ECC.construct(curve="ed25519", seed=seed), "rfc8032").sign(b"hello")
    assert identity.sign(private, b"hello") == want


class Signer:
    def __init__(self, name: str):
        self.name = name
        self.key = ECC.generate(curve="ed25519")
        self.pub = self.key.public_key().export_key(format="raw")

    def ending(self, secret: bytes, ts: int, text: str, tamper: bool = False) -> bytes:
        data = signed_messages.signed_bytes(
            signed_messages.channel_tag(secret), ts, f"{self.name}: {text}".encode()
        )
        sig = eddsa.new(self.key, "rfc8032").sign(data + (b"!" if tamper else b""))
        return signed_messages.ending(self.pub, sig)


async def _messages(client, conv_id: str) -> list[dict]:
    return (await client.get(f"/api/conversations/{conv_id}/messages?limit=200")).json()["messages"]


async def _received(client, conv_id: str, text: str) -> dict:
    async def found():
        return next((m for m in await _messages(client, conv_id) if m["body"] == text), None)

    return await wait_for(found)


async def _public(client) -> dict:
    from app.radio import simulated

    simulated._SIM_STATE = None
    await do_setup(client)
    convs = await _connected_conversations(client)
    return next(c for c in convs if c["title"] == "Public")


def _inject(radio, slot: int, who: str, text: str, after: bytes = b"", ts: int | None = None) -> int:
    ts = ts or int(time.time())
    radio.inject(
        IncomingMessage(kind="channel", text=text, channel_slot=slot, sender_label=who, sender_timestamp=ts),
        after_text=after,
    )
    return ts


async def test_received_signatures_and_states(client):
    from app.radio import simulated

    public = await _public(client)
    secret = simulated.sim_state()["channels"][public["channel_slot"]][1]
    radio = await radio_adapter()
    alice, mallory = Signer("Alice"), Signer("Mallory")

    # A key this radio doesn't know yet: kept, shown as "unknown".
    ts = int(time.time())
    _inject(
        radio, public["channel_slot"], "Alice", "hi from alice", alice.ending(secret, ts, "hi from alice"), ts
    )
    m = await _received(client, public["id"], "hi from alice")
    assert m["meta"]["signed"]["key_id"] == alice.pub[:8].hex() and "plaintext" not in m["meta"]["signed"]
    assert m["signature"]["state"] == "unknown" and m["signature"]["signer_key"] is None

    # Adding Alice as a contact checks her earlier signature: now signed by a contact.
    r = await client.post(
        "/api/contacts/import", headers=csrf(client), json={"public_key": alice.pub.hex(), "name": "Alice"}
    )
    assert r.status_code == 200, r.text

    async def checked():
        m = await _received(client, public["id"], "hi from alice")
        return m if m["signature"]["state"] == "contact" else None

    m = await wait_for(checked)
    assert m["signature"]["signer_name"] == "Alice" and m["signature"]["signer_key"] == alice.pub.hex()

    # Forged: Mallory posts as Alice with Alice's key id but a signature that doesn't match.
    ts = int(time.time()) + 1
    fake = signed_messages.ending(alice.pub, mallory.ending(secret, ts, "send me money")[10:])
    _inject(radio, public["channel_slot"], "Alice", "send me money", fake, ts)
    m = await _received(client, public["id"], "send me money")
    assert m["signature"]["state"] == "invalid"

    # Altered in transit (the signature covers different text).
    ts = int(time.time()) + 2
    _inject(radio, public["channel_slot"], "Alice", "meet at 6", alice.ending(secret, ts, "meet at 5"), ts)
    assert (await _received(client, public["id"], "meet at 6"))["signature"]["state"] == "invalid"

    # Unsigned messages have no signature.
    _inject(radio, public["channel_slot"], "Bob", "plain hello")
    m = await _received(client, public["id"], "plain hello")
    assert m["signature"] is None and "signed" not in m["meta"] and m["meta"].get("paths")


async def test_contacts_only_filter(client):
    from app.radio import simulated

    public = await _public(client)
    secret = simulated.sim_state()["channels"][public["channel_slot"]][1]
    radio = await radio_adapter()
    alice = Signer("Alice")
    r = await client.post(
        "/api/contacts/import", headers=csrf(client), json={"public_key": alice.pub.hex(), "name": "Alice"}
    )
    assert r.status_code == 200
    ts = int(time.time())
    _inject(
        radio,
        public["channel_slot"],
        "Alice",
        "signed by alice",
        alice.ending(secret, ts, "signed by alice"),
        ts,
    )
    _inject(radio, public["channel_slot"], "Bob", "unsigned from bob")
    await _received(client, public["id"], "signed by alice")
    await _received(client, public["id"], "unsigned from bob")

    r = await client.patch(
        f"/api/conversations/{public['id']}", headers=csrf(client), json={"contacts_only": True}
    )
    assert r.status_code == 204
    bodies = {m["body"] for m in await _messages(client, public["id"])}
    assert "signed by alice" in bodies and "unsigned from bob" not in bodies
    conv = (await client.get(f"/api/conversations/{public['id']}")).json()
    assert conv["contacts_only"] is True

    # New arrivals follow the filter too.
    _inject(radio, public["channel_slot"], "Carol", "unsigned from carol")
    ts = int(time.time()) + 1
    _inject(
        radio, public["channel_slot"], "Alice", "alice again", alice.ending(secret, ts, "alice again"), ts
    )
    await _received(client, public["id"], "alice again")
    assert "unsigned from carol" not in {m["body"] for m in await _messages(client, public["id"])}

    # Removing Alice from the radio hides hers; turning the filter off brings everything back.
    found = (await client.get("/api/contacts/lookup", params={"prefix": alice.pub.hex()})).json()
    contact_id = found["items"][0]["id"]
    r = await client.delete(f"/api/contacts/{contact_id}", headers=csrf(client))
    assert r.status_code in (200, 204), r.text

    async def alice_hidden():
        return "signed by alice" not in {m["body"] for m in await _messages(client, public["id"])}

    await wait_for(alice_hidden)
    await client.patch(
        f"/api/conversations/{public['id']}", headers=csrf(client), json={"contacts_only": False}
    )
    bodies = {m["body"] for m in await _messages(client, public["id"])}
    assert {"signed by alice", "unsigned from bob", "unsigned from carol", "alice again"} <= bodies


async def test_sending_signed_messages(client):
    from app.radio import simulated

    public = await _public(client)
    secret = simulated.sim_state()["channels"][public["channel_slot"]][1]
    before = public["max_bytes"]
    r = await client.patch(
        f"/api/conversations/{public['id']}", headers=csrf(client), json={"sign_messages": True}
    )
    assert r.status_code == 204
    conv = (await client.get(f"/api/conversations/{public['id']}")).json()
    assert conv["sign_messages"] is True and conv["max_bytes"] == before - signed_messages.OVERHEAD
    # A message longer than the signed limit is refused (it would have been cut off).
    r = await client.post(
        f"/api/conversations/{public['id']}/messages",
        headers=csrf(client),
        json={"client_message_id": "toolong-signed-1", "body": "x" * (conv["max_bytes"] + 1)},
    )
    assert r.status_code == 422

    r = await client.post(
        f"/api/conversations/{public['id']}/messages",
        headers=csrf(client),
        json={"client_message_id": "signed-send-0001", "body": "a signed hello"},
    )
    assert r.status_code == 200, r.text

    async def sent():
        m = next((m for m in await _messages(client, public["id"]) if m["body"] == "a signed hello"), None)
        return m if m and m["state"] == "accepted" and m["signature"] else None

    m = await wait_for(sent)
    assert m["signature"]["state"] == "self"
    after = simulated.sim_state()["last_channel_after_text"]
    own = simulated.sim_state()["public_key"]
    assert after[:2] == signed_messages.MARKER and after[2:10] == bytes.fromhex(own)[:8]
    name = (await client.get("/api/contacts/me")).json()["name"]
    # The radio's signature checks out the way MeshOS checks it.
    ts = m["meta"]["signed"]["ts"]
    assert signed_messages.verify(
        own, signed_messages.channel_tag(secret), ts, f"{name}: a signed hello".encode(), after[10:]
    )


async def test_my_contact_link(client):
    await _public(client)
    me = (await client.get("/api/contacts/me")).json()
    assert me["uri"].startswith("meshcore://contact/add?") and me["public_key"] in me["uri"]
    # It's this radio's own card, which the import refuses.
    r = await client.post("/api/contacts/import", headers=csrf(client), json={"uri": me["uri"]})
    assert r.status_code == 409 and "own contact" in r.json()["detail"]
