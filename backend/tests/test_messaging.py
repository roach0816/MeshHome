"""Persistence rules: channel generations, DM sender resolution, duplicate handling."""

import uuid

from sqlalchemy import select

from app import db
from app.models import Channel, Conversation, Radio
from app.radio.base import DeviceSnapshot, IncomingMessage, RadioChannel, RadioContact, split_channel_text
from app.services import messaging


def test_split_channel_text():
    assert split_channel_text("Alice: hello: world") == ("Alice", "hello: world")
    assert split_channel_text("no prefix here") == (None, "no prefix here")
    assert split_channel_text("line\nbreak: x") == (None, "line\nbreak: x")


async def _radio(s) -> Radio:
    return await messaging.upsert_radio(
        s, DeviceSnapshot(public_key=uuid.uuid4().hex + uuid.uuid4().hex, name="Test", is_simulated=True)
    )


async def test_channel_slot_reuse_creates_new_generation(client):
    key = b"k" * 32
    async with db.session_factory()() as s:
        radio = await _radio(s)
        await messaging.sync_channels(s, radio, [RadioChannel(0, "Public", b"a" * 16)], key)
        await messaging.sync_channels(s, radio, [RadioChannel(0, "Public", b"a" * 16)], key)  # unchanged
        await messaging.sync_channels(s, radio, [RadioChannel(0, "Hikers", b"b" * 16)], key)  # slot reused
        rows = (
            (
                await s.execute(
                    select(Channel).where(Channel.radio_id == radio.id).order_by(Channel.generation)
                )
            )
            .scalars()
            .all()
        )
        assert [(c.name, c.generation, c.active) for c in rows] == [("Public", 1, False), ("Hikers", 2, True)]
        convs = (
            (await s.execute(select(Conversation).where(Conversation.radio_id == radio.id))).scalars().all()
        )
        assert len(convs) == 2  # archives are never merged
        await s.rollback()


async def test_dm_prefix_resolution_and_ambiguity(client):
    async with db.session_factory()() as s:
        radio = await _radio(s)
        k1 = "aabbcc" + "1" * 58
        k2 = "aabbcc" + "2" * 58
        await messaging.sync_contacts(s, radio, [RadioContact(k1, "One"), RadioContact(k2, "Two")])
        unique = await messaging.resolve_dm_conversation(s, radio, k1[:12])
        assert unique.contact_id is not None
        ambiguous = await messaging.resolve_dm_conversation(s, radio, "aabbcc")
        assert ambiguous.contact_id is None and ambiguous.peer_prefix == "aabbcc"
        await s.rollback()


async def test_exact_repeat_is_counted_not_duplicated(client):
    async with db.session_factory()() as s:
        radio = await _radio(s)
        await messaging.sync_channels(s, radio, [RadioChannel(0, "Public", b"a" * 16)], b"k" * 32)
        msg = IncomingMessage(
            kind="channel", text="hi", channel_slot=0, sender_label="Bob", sender_timestamp=1000
        )
        m1, created1 = await messaging.ingest_incoming(s, radio, msg)
        m2, created2 = await messaging.ingest_incoming(s, radio, msg)
        other = IncomingMessage(
            kind="channel", text="hi", channel_slot=0, sender_label="Bob", sender_timestamp=1001
        )
        m3, created3 = await messaging.ingest_incoming(s, radio, other)
        assert created1 and not created2 and created3
        assert m1.id == m2.id and m1.duplicate_count == 1
        assert m3.position > m1.position
        await s.rollback()


def test_advert_position_normalisation():
    from app.radio.base import advert_position

    assert advert_position(40.1, -105.2) == (40.1, -105.2)
    assert advert_position(0, 0) == (None, None)  # MeshCore's "no location"
    assert advert_position(91, 0) == (None, None)
    assert advert_position(None, "x") == (None, None)
