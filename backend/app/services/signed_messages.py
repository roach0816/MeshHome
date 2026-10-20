"""Signed channel messages: a signature carried after the text of a channel message.

MeshOS (companion app 1.0.26+) signs channel messages with the sender's key and puts the signature
inside the encrypted payload, after the text's terminating zero byte, so other MeshCore apps and
radios show the text as usual. The decrypted GRP_TXT plaintext is then:

    [timestamp 4, LE][flags 1]["<name>: <text>"][0x00]
    [0x53 0x01][signer key id 8][Ed25519 signature 64][zero padding up to the 16-byte block]

(0x53 0x01 is "S" + version 1; the key id is the first 8 bytes of the signer's public key.)

The signature is Ed25519 (pure, as the radio's sign command makes it) over:

    "MeshOS signed channel v1"          24 bytes, no terminator
    SHA-256(channel key[:16])[:16]      binds it to the channel
    timestamp                           4 bytes, LE: the message's own
    "<name>: <text>"                    exactly as sent

As MeshOS 1.0.29 and its firmware 1.7.8 build it, checked against a real message signed by a MeshOS
app through a MeshCore radio (October 2026). The radio's text frame stops at the zero byte, so
MeshHome reads the signature by decrypting the packet it heard (radio/packets.py decrypt_group).

Whether a message is shown as signed by a contact is worked out when it's read (`state()`), so
adding or removing a contact changes it straight away; `refresh_hidden()` re-applies a channel's
"only messages from my contacts" filter.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from Crypto.Signature import eddsa
from sqlalchemy import and_, false, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Contact, Conversation, Message

TAG = b"MeshOS signed channel v1"

MARKER = b"\x53\x01"
KEY_ID_SIZE = 8
SIG_SIZE = 64
ENDING_SIZE = len(MARKER) + KEY_ID_SIZE + SIG_SIZE  # 74, after the text's zero byte
OVERHEAD = 1 + ENDING_SIZE  # bytes a signature takes from the text (with the zero byte)

State = Literal["self", "contact", "known", "unknown", "invalid"]


@dataclass(frozen=True)
class SignedEnding:
    timestamp: int
    flags: int
    text: bytes  # "<name>: <text>" as sent, UTF-8
    key_id: bytes
    signature: bytes


def parse(plaintext: bytes) -> SignedEnding | None:
    """The signature of a decrypted channel message, or None if it carries none."""
    if len(plaintext) < 5:
        return None
    end = plaintext.find(b"\0", 5)
    if end < 0:
        return None
    ext = end + 1
    tail = plaintext[ext + ENDING_SIZE :]
    if plaintext[ext : ext + len(MARKER)] != MARKER or len(plaintext) < ext + ENDING_SIZE:
        return None
    if len(tail) > 15 or any(tail):  # only the block's zero padding may follow
        return None
    k = ext + len(MARKER)
    return SignedEnding(
        timestamp=int.from_bytes(plaintext[:4], "little"),
        flags=plaintext[4],
        text=plaintext[5:end],
        key_id=plaintext[k : k + KEY_ID_SIZE],
        signature=plaintext[k + KEY_ID_SIZE : k + KEY_ID_SIZE + SIG_SIZE],
    )


def channel_tag(secret: bytes) -> bytes:
    """What binds a signature to its channel: the first 16 bytes of SHA-256 of the channel key."""
    return hashlib.sha256(secret[:16]).digest()[:16]


def signed_bytes(chan: bytes, timestamp: int, text: bytes) -> bytes:
    return TAG + chan + timestamp.to_bytes(4, "little") + text


def ending(public_key: bytes, signature: bytes) -> bytes:
    """What goes after the text's zero byte."""
    return MARKER + public_key[:KEY_ID_SIZE] + signature


def verify(public_key_hex: str, chan: bytes, timestamp: int, text: bytes, signature: bytes) -> bool:
    try:
        key = eddsa.import_public_key(bytes.fromhex(public_key_hex))
        eddsa.new(key, "rfc8032").verify(signed_bytes(chan, timestamp, text), signature)
        return True
    except (ValueError, TypeError):
        return False


def meta_for(e: SignedEnding, secret: bytes) -> dict[str, Any]:
    """What a received signed message keeps in its meta["signed"]. Not the key: only the hash
    the signature covers (whose first byte is sent in the clear anyway)."""
    return {
        "v": 1,
        "key_id": e.key_id.hex(),
        "signature": e.signature.hex(),
        "chan": channel_tag(secret).hex(),
    }


def sent_text(m: Message) -> bytes:
    """The text as the sender's radio sent it: "<name>: <text>"."""
    return (f"{m.sender_label}: {m.body}" if m.sender_label else m.body).encode()


async def check(db: AsyncSession, radio_id: uuid.UUID, m: Message) -> None:
    """Check a received message's signature against the signer's key, if this radio knows it (a
    contact, now or before). Sets meta["signed"]["valid"] and, if it checks out, signed_by."""
    sig = (m.meta or {}).get("signed")
    if not sig or "chan" not in sig or sig.get("valid") is not None or m.sender_timestamp is None:
        return
    key = (
        await db.execute(
            select(Contact.public_key).where(
                Contact.radio_id == radio_id, Contact.public_key.like(f"{sig['key_id']}%")
            )
        )
    ).scalar_one_or_none()
    if key is None:
        return
    ok = verify(
        key, bytes.fromhex(sig["chan"]), m.sender_timestamp, sent_text(m), bytes.fromhex(sig["signature"])
    )
    m.meta = {**m.meta, "signed": {**sig, "valid": ok}}
    m.signed_by = key if ok else None


async def check_pending(db: AsyncSession, radio_id: uuid.UUID) -> int:
    """Check signatures that arrived before their signer's key was known. Returns how many."""
    convs = select(Conversation.id).where(Conversation.radio_id == radio_id, Conversation.kind == "channel")
    rows = (
        await db.execute(
            select(Message).where(
                Message.conversation_id.in_(convs),
                Message.direction == "in",
                Message.signed_by.is_(None),
                Message.meta["signed"]["chan"].isnot(None),
                Message.meta["signed"]["valid"].is_(None),
            )
        )
    ).scalars()
    n = 0
    for m in rows:
        await check(db, radio_id, m)
        n += (m.meta or {}).get("signed", {}).get("valid") is not None
    await db.flush()
    return n


async def contact_keys(db: AsyncSession, radio_id: uuid.UUID) -> dict[str, str]:
    """The radio's contacts (not blocked): public key -> name."""
    rows = await db.execute(
        select(Contact.public_key, Contact.name).where(
            Contact.radio_id == radio_id, Contact.on_radio.is_(True), Contact.blocked.is_(False)
        )
    )
    return {k: n for k, n in rows.all()}


def state(m: Message, contacts: dict[str, str], own_key: str | None) -> State | None:
    """How a message's signature shows: None if it isn't signed."""
    sig = (m.meta or {}).get("signed")
    if not sig:
        return None
    if m.direction == "out":
        return "self"
    if sig.get("valid") is False:
        return "invalid"
    if m.signed_by is None:
        return "unknown"
    if own_key and m.signed_by == own_key:
        return "self"
    return "contact" if m.signed_by in contacts else "known"


async def refresh_hidden(
    db: AsyncSession, radio_id: uuid.UUID, conv_ids: list[uuid.UUID] | None = None
) -> int:
    """Re-apply which received channel messages are hidden (suppressed): those from a blocked name,
    and, where a channel shows only contacts' messages, any not signed by one of the radio's
    contacts. Unblocking, adding a contact or turning the filter off brings messages back."""
    q = select(Conversation).where(Conversation.radio_id == radio_id, Conversation.kind == "channel")
    if conv_ids is not None:
        q = q.where(Conversation.id.in_(conv_ids))
    blocked_names = select(Contact.name).where(
        Contact.radio_id == radio_id, Contact.blocked.is_(True), Contact.name != ""
    )
    keys = select(Contact.public_key).where(
        Contact.radio_id == radio_id, Contact.on_radio.is_(True), Contact.blocked.is_(False)
    )
    changed = 0
    for conv in (await db.execute(q)).scalars():
        blocked = Message.sender_label.in_(blocked_names)
        hidden = (
            or_(blocked, Message.signed_by.is_(None), Message.signed_by.not_in(keys))
            if conv.contacts_only
            else or_(blocked, false())
        )
        res = await db.execute(
            update(Message)
            .where(
                Message.conversation_id == conv.id, Message.direction == "in", Message.suppressed != hidden
            )
            .values(suppressed=hidden)
        )
        changed += res.rowcount or 0
    await db.flush()
    return changed


async def hidden_on_arrival(db: AsyncSession, conv: Conversation, m: Message) -> bool:
    """For a new channel message in a "contacts only" channel: not signed by a contact."""
    if not conv.contacts_only:
        return False
    if m.signed_by is None:
        return True
    hit = await db.execute(
        select(Contact.id).where(
            and_(
                Contact.radio_id == conv.radio_id,
                Contact.public_key == m.signed_by,
                Contact.on_radio.is_(True),
                Contact.blocked.is_(False),
            )
        )
    )
    return hit.first() is None
