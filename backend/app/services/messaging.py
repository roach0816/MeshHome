"""Persistence rules for radios, contacts, channels, conversations and messages."""

from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Channel, Contact, Conversation, Message, Radio, utcnow
from app.radio.base import DeviceSnapshot, IncomingMessage, RadioChannel, RadioContact


class States:
    RECEIVED = "received"
    QUEUED = "queued"
    SENDING = "sending"
    ACCEPTED = "accepted"
    ACKNOWLEDGED = "acknowledged"
    NO_ACK = "no_ack"
    UNCERTAIN = "uncertain"
    FAILED = "failed"
    EXPIRED = "expired"

    RETRYABLE = {"failed", "uncertain", "no_ack", "expired"}


# Arbitrary constant used with pg_advisory_xact_lock to serialise position assignment, so a
# reader can never observe position N+1 committed while N is still in flight.
POSITION_LOCK_ID = 0x4D43_504F  # "MCPO"


async def next_position(db: AsyncSession) -> int:
    await db.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": POSITION_LOCK_ID})
    current = (await db.execute(select(func.coalesce(func.max(Message.position), 0)))).scalar_one()
    return int(current) + 1


def _epoch(ts: int | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts), UTC)
    except (OverflowError, OSError, ValueError):
        return None


# ---- radio / contacts / channels -------------------------------------------------------


async def upsert_radio(db: AsyncSession, snap: DeviceSnapshot) -> Radio:
    radio = (await db.execute(select(Radio).where(Radio.public_key == snap.public_key))).scalar_one_or_none()
    if radio is None:
        radio = Radio(public_key=snap.public_key, is_simulated=snap.is_simulated)
        db.add(radio)
    radio.name = snap.name
    radio.device_info = {"model": snap.model, "firmware": snap.firmware, **snap.raw_device_info}
    radio.self_info = {
        "radio": snap.radio,
        "location": {"lat": snap.lat, "lon": snap.lon} if snap.lat is not None else None,
    }
    radio.last_connected_at = utcnow()
    await db.flush()
    return radio


async def sync_contacts(db: AsyncSession, radio: Radio, contacts: list[RadioContact]) -> int:
    existing = {
        c.public_key: c
        for c in (await db.execute(select(Contact).where(Contact.radio_id == radio.id))).scalars()
    }
    seen = set()
    for rc in contacts:
        seen.add(rc.public_key)
        row = existing.get(rc.public_key)
        if row is None:
            row = Contact(radio_id=radio.id, public_key=rc.public_key)
            db.add(row)
        row.name = rc.name
        row.kind = rc.kind
        row.last_advert_at = _epoch(rc.last_advert)
        row.lat, row.lon = rc.lat, rc.lon
        row.favorite = bool(rc.flags & 0x01)
        row.on_radio = True
        row.meta = {**rc.meta, "flags": rc.flags, "path_len": rc.path_len, "path_hex": rc.path_hex}
    for key, row in existing.items():
        if key not in seen:
            row.on_radio = False  # archive keeps it; the radio no longer has it
    await db.flush()
    return len(contacts)


def channel_fingerprint(key: bytes, ch: RadioChannel) -> str:
    return hmac.new(key, ch.name.encode() + b"\x00" + ch.secret, hashlib.sha256).hexdigest()


async def sync_channels(db: AsyncSession, radio: Radio, channels: list[RadioChannel], fp_key: bytes) -> None:
    active = {
        c.slot: c
        for c in (
            await db.execute(select(Channel).where(Channel.radio_id == radio.id, Channel.active.is_(True)))
        ).scalars()
    }
    seen_slots = set()
    for rc in channels:
        seen_slots.add(rc.slot)
        fp = channel_fingerprint(fp_key, rc)
        row = active.get(rc.slot)
        if row is not None and row.fingerprint == fp:
            continue
        generation = 1
        if row is not None:
            # Slot reused for a different channel: archive the old generation, never merge.
            row.active = False
            generation = row.generation + 1
        else:
            prev = (
                await db.execute(
                    select(func.max(Channel.generation)).where(
                        Channel.radio_id == radio.id, Channel.slot == rc.slot
                    )
                )
            ).scalar_one_or_none()
            generation = (prev or 0) + 1
        ch = Channel(radio_id=radio.id, slot=rc.slot, generation=generation, name=rc.name, fingerprint=fp)
        db.add(ch)
        await db.flush()
        db.add(Conversation(radio_id=radio.id, kind="channel", channel_id=ch.id, title=rc.name))
    for slot, row in active.items():
        if slot not in seen_slots:
            row.active = False
    await db.flush()


# ---- conversations ---------------------------------------------------------------------


async def dm_conversation_for_contact(db: AsyncSession, contact: Contact) -> Conversation:
    conv = (
        await db.execute(select(Conversation).where(Conversation.contact_id == contact.id))
    ).scalar_one_or_none()
    if conv is None:
        conv = Conversation(
            radio_id=contact.radio_id, kind="dm", contact_id=contact.id, title=contact.alias or contact.name
        )
        db.add(conv)
        await db.flush()
    return conv


async def resolve_dm_conversation(db: AsyncSession, radio: Radio, prefix: str) -> Conversation:
    prefix = (prefix or "").lower()
    matches = (
        (
            await db.execute(
                select(Contact).where(Contact.radio_id == radio.id, Contact.public_key.startswith(prefix))
            )
        )
        .scalars()
        .all()
        if prefix
        else []
    )
    if len(matches) == 1:
        return await dm_conversation_for_contact(db, matches[0])
    # Unknown or ambiguous sender: keep it separate, never guess by name.
    label = f"Unknown {prefix[:8]}" if prefix else "Unknown sender"
    conv = (
        await db.execute(
            select(Conversation).where(Conversation.radio_id == radio.id, Conversation.peer_prefix == prefix)
        )
    ).scalar_one_or_none()
    if conv is None:
        conv = Conversation(radio_id=radio.id, kind="dm", peer_prefix=prefix, title=label)
        db.add(conv)
        await db.flush()
    return conv


async def channel_conversation(db: AsyncSession, radio: Radio, slot: int) -> Conversation | None:
    return (
        await db.execute(
            select(Conversation)
            .join(Channel, Channel.id == Conversation.channel_id)
            .where(Channel.radio_id == radio.id, Channel.slot == slot, Channel.active.is_(True))
        )
    ).scalar_one_or_none()


# ---- incoming --------------------------------------------------------------------------


def incoming_fingerprint(radio: Radio, conv: Conversation, msg: IncomingMessage) -> str:
    parts = [
        str(radio.id),
        str(conv.id),
        msg.pubkey_prefix or "",
        msg.sender_label or "",
        str(msg.sender_timestamp or ""),
        str(msg.txt_type),
        msg.text,
    ]
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


DEDUP_WINDOW = timedelta(hours=24)


async def _sender_blocked(db: AsyncSession, radio: Radio, conv: Conversation, msg: IncomingMessage) -> bool:
    if conv.kind == "dm":
        if conv.contact_id is None:
            return False
        contact = await db.get(Contact, conv.contact_id)
        return bool(contact and contact.blocked)
    # Channel messages carry only the sender's self-chosen name, so blocking matches by name.
    if not msg.sender_label:
        return False
    hit = await db.execute(
        select(Contact.id).where(
            Contact.radio_id == radio.id, Contact.blocked.is_(True), Contact.name == msg.sender_label
        )
    )
    return hit.first() is not None


async def set_contact_blocked(db: AsyncSession, contact: Contact, blocked: bool) -> int:
    """Block/unblock and re-flag that sender's archived incoming messages. Returns rows changed."""
    contact.blocked = blocked
    changed = 0
    conv = (
        await db.execute(select(Conversation).where(Conversation.contact_id == contact.id))
    ).scalar_one_or_none()
    if conv is not None:
        res = await db.execute(
            update(Message)
            .where(Message.conversation_id == conv.id, Message.direction == "in")
            .values(suppressed=blocked)
        )
        changed += res.rowcount or 0
    if contact.name:
        channel_convs = select(Conversation.id).where(
            Conversation.radio_id == contact.radio_id, Conversation.kind == "channel"
        )
        res = await db.execute(
            update(Message)
            .where(
                Message.conversation_id.in_(channel_convs),
                Message.direction == "in",
                Message.sender_label == contact.name,
            )
            .values(suppressed=blocked)
        )
        changed += res.rowcount or 0
    await db.flush()
    return changed


async def ingest_incoming(db: AsyncSession, radio: Radio, msg: IncomingMessage) -> tuple[Message, bool]:
    """Persist one received message. Returns (message, created). Caller commits."""
    if msg.kind == "channel":
        conv = await channel_conversation(db, radio, int(msg.channel_slot or 0))
        if conv is None:
            # Message for a slot we have no record of: archive under a placeholder channel.
            ch = Channel(
                radio_id=radio.id,
                slot=int(msg.channel_slot or 0),
                generation=0,
                name=f"Channel {msg.channel_slot}",
                fingerprint="unknown",
            )
            db.add(ch)
            await db.flush()
            conv = Conversation(radio_id=radio.id, kind="channel", channel_id=ch.id, title=ch.name)
            db.add(conv)
            await db.flush()
    else:
        conv = await resolve_dm_conversation(db, radio, msg.pubkey_prefix or "")

    suppressed = await _sender_blocked(db, radio, conv, msg)
    fp = incoming_fingerprint(radio, conv, msg)
    # Conservative duplicate suppression: only an exact repeat (same sender metadata, protocol
    # timestamp, type and text) within a bounded window. The repeat is counted, not discarded silently.
    dup = (
        await db.execute(
            select(Message)
            .where(Message.fingerprint == fp, Message.created_at > utcnow() - DEDUP_WINDOW)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if dup is not None:
        dup.duplicate_count += 1
        await db.flush()
        return dup, False

    position = await next_position(db)
    now = utcnow()
    m = Message(
        position=position,
        conversation_id=conv.id,
        direction="in",
        sender_label=msg.sender_label,
        sender_key_prefix=msg.pubkey_prefix,
        body=msg.text,
        txt_type=msg.txt_type,
        sender_timestamp=msg.sender_timestamp,
        created_at=now,
        state=States.RECEIVED,
        fingerprint=fp,
        is_simulated=radio.is_simulated,
        suppressed=suppressed,
        meta=msg.meta,
    )
    db.add(m)
    if not suppressed:
        conv.last_message_at = now
    conv.last_position = position
    await db.flush()
    return m, True


# ---- outgoing --------------------------------------------------------------------------


class IdempotencyConflict(Exception):
    pass


async def create_outgoing(
    db: AsyncSession,
    conv: Conversation,
    body: str,
    client_message_id: str,
    expiry_seconds: int,
    is_simulated: bool,
) -> tuple[Message, bool]:
    existing = (
        await db.execute(select(Message).where(Message.client_message_id == client_message_id))
    ).scalar_one_or_none()
    if existing is not None:
        if existing.body != body or existing.conversation_id != conv.id:
            raise IdempotencyConflict()
        return existing, False
    position = await next_position(db)
    now = utcnow()
    m = Message(
        id=uuid.uuid4(),
        position=position,
        conversation_id=conv.id,
        direction="out",
        body=body,
        created_at=now,
        state=States.QUEUED,
        client_message_id=client_message_id,
        expires_at=now + timedelta(seconds=expiry_seconds),
        is_simulated=is_simulated,
    )
    db.add(m)
    conv.last_message_at = now
    conv.last_position = position
    await db.flush()
    return m, True


async def mark_interrupted_sends_uncertain(db: AsyncSession) -> int:
    """After a crash/restart, an attempt that began but never finished has an unknown outcome."""
    res = await db.execute(
        update(Message)
        .where(Message.state == States.SENDING)
        .values(
            state=States.UNCERTAIN,
            error="Interrupted while sending; it may or may not have been transmitted.",
        )
    )
    return res.rowcount or 0


async def expire_and_timeout(db: AsyncSession) -> list[uuid.UUID]:
    now = utcnow()
    expired = await db.execute(
        update(Message)
        .where(Message.state == States.QUEUED, Message.expires_at < now)
        .values(state=States.EXPIRED, error="Not sent before the request expired.")
        .returning(Message.id)
    )
    timed_out = await db.execute(
        update(Message)
        .where(
            and_(
                Message.state == States.ACCEPTED,
                Message.ack_deadline.is_not(None),
                Message.ack_deadline < now,
            )
        )
        .values(state=States.NO_ACK)
        .returning(Message.id)
    )
    return [r[0] for r in expired] + [r[0] for r in timed_out]
