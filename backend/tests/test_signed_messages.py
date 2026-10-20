"""Signed channel messages: reading the signature after the text (services/signed_messages.py),
opening channel packets (radio/packets.py decrypt_group), and recording it with the message."""

import time

import pytest

from app.radio import packets
from app.radio.base import IncomingMessage
from app.services import signed_messages
from tests.conftest import do_setup, radio_adapter, wait_for
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


async def _received(client, conv_id: str, text: str) -> dict:
    async def found():
        msgs = (await client.get(f"/api/conversations/{conv_id}/messages?limit=50")).json()["messages"]
        return next((m for m in msgs if m["body"] == text), None)

    return await wait_for(found)


async def test_signature_is_recorded_with_the_message(client):
    from app.radio import simulated

    simulated._SIM_STATE = None
    await do_setup(client)
    convs = await _connected_conversations(client)
    public = next(c for c in convs if c["title"] == "Public")
    now = int(time.time())
    key_id, sig = bytes.fromhex("2f7708e2ab973a55"), bytes(range(64))
    radio = await radio_adapter()
    radio.inject(
        IncomingMessage(
            kind="channel",
            text="a signed hello",
            channel_slot=public["channel_slot"],
            sender_label="Tracker (sim)",
            sender_timestamp=now,
        ),
        after_text=signed_messages.MARKER + key_id + sig,
    )
    m = await _received(client, public["id"], "a signed hello")
    signed = m["meta"]["signed"]
    assert signed["v"] == 1 and signed["key_id"] == key_id.hex() and signed["signature"] == sig.hex()
    plain = bytes.fromhex(signed["plaintext"])
    assert plain[:4] == now.to_bytes(4, "little") and b"Tracker (sim): a signed hello\0" in plain
    assert plain.endswith(sig)

    # Unsigned messages carry nothing extra.
    radio.inject(
        IncomingMessage(
            kind="channel",
            text="plain hello",
            channel_slot=public["channel_slot"],
            sender_label="Tracker (sim)",
            sender_timestamp=now,
        )
    )
    m = await _received(client, public["id"], "plain hello")
    assert "signed" not in m["meta"] and m["meta"].get("paths")
