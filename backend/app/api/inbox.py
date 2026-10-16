"""Conversations, messages, contacts, search and export."""

import uuid
from datetime import datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy import delete as sa_delete
from sqlalchemy.dialects.postgresql import distinct_on, insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.contacts import radio_contact_op
from app.api.deps import AuthContext, require_auth
from app.config import get_settings
from app.db import get_db
from app.models import AuditEvent, Channel, Contact, Conversation, Message, Radio, ReadPosition, utcnow
from app.radio.base import CHANNEL_MAX_BYTES, DM_MAX_BYTES
from app.radio.supervisor import supervisor
from app.realtime import hub
from app.security import send_limiter
from app.services import messaging
from app.services.messaging import States

router = APIRouter(prefix="/api", tags=["inbox"])


# ---- schemas ----------------------------------------------------------------------------


class MessageOut(BaseModel):
    id: uuid.UUID
    position: int
    conversation_id: uuid.UUID
    direction: str
    sender_label: str | None
    sender_key_prefix: str | None
    body: str
    sender_timestamp: int | None
    created_at: datetime
    state: str
    error: str | None
    duplicate_count: int
    is_simulated: bool
    client_message_id: str | None
    meta: dict[str, Any]

    model_config = {"from_attributes": True}


class Preview(BaseModel):
    body: str
    direction: str
    sender_label: str | None
    state: str
    created_at: datetime


class ConversationOut(BaseModel):
    id: uuid.UUID
    kind: str
    title: str
    favorite: bool
    muted: bool
    sound: str | None = None  # per-conversation override: None = global setting, "on" / "off"
    blocked: bool = False
    last_message_at: datetime | None
    last_position: int
    read_position: int
    unread: int
    is_simulated: bool
    archived: bool
    channel_slot: int | None = None
    contact_id: uuid.UUID | None = None
    contact_public_key: str | None = None
    peer_prefix: str | None = None
    max_bytes: int
    preview: Preview | None = None


class MessagePage(BaseModel):
    messages: list[MessageOut]
    has_more: bool


class SendRequest(BaseModel):
    client_message_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    body: str = Field(min_length=1, max_length=4096)


class RetryRequest(BaseModel):
    confirm_possible_duplicate: bool = False


class ReadPositionRequest(BaseModel):
    position: int = Field(ge=0)


class ConversationPatch(BaseModel):
    favorite: bool | None = None
    muted: bool | None = None
    # "on"/"off" override the global sound setting; "default" clears the override.
    sound: Literal["on", "off", "default"] | None = None


class SearchHit(MessageOut):
    conversation_title: str


# ---- helpers ----------------------------------------------------------------------------


def channel_budget(radio_name: str) -> int:
    # Channel text is transmitted as "<node name>: <body>".
    return max(0, CHANNEL_MAX_BYTES - len(radio_name.encode()) - 2)


async def _conversation(db: AsyncSession, conv_id: uuid.UUID) -> Conversation:
    conv = await db.get(Conversation, conv_id)
    if conv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    return conv


async def _conversation_rows(db: AsyncSession, user_id: uuid.UUID, conv_id: uuid.UUID | None = None):
    q = (
        select(Conversation, Radio, Channel, Contact, ReadPosition.position)
        .join(Radio, Radio.id == Conversation.radio_id)
        .outerjoin(Channel, Channel.id == Conversation.channel_id)
        .outerjoin(Contact, Contact.id == Conversation.contact_id)
        .outerjoin(
            ReadPosition,
            and_(ReadPosition.conversation_id == Conversation.id, ReadPosition.user_id == user_id),
        )
    )
    if conv_id:
        q = q.where(Conversation.id == conv_id)
    rows = (await db.execute(q)).all()
    if not rows:
        return []

    ids = [r[0].id for r in rows]
    unread_q = (
        select(Message.conversation_id, func.count())
        .outerjoin(
            ReadPosition,
            and_(ReadPosition.conversation_id == Message.conversation_id, ReadPosition.user_id == user_id),
        )
        .where(
            Message.conversation_id.in_(ids),
            Message.direction == "in",
            Message.position > func.coalesce(ReadPosition.position, 0),
            Message.suppressed.is_(False),
        )
        .group_by(Message.conversation_id)
    )
    unread = dict((await db.execute(unread_q)).all())
    previews = {
        m.conversation_id: m
        for m in (
            await db.execute(
                select(Message)
                .ext(distinct_on(Message.conversation_id))
                .where(Message.conversation_id.in_(ids), Message.suppressed.is_(False))
                .order_by(Message.conversation_id, Message.position.desc())
            )
        ).scalars()
    }

    out = []
    for conv, radio, channel, contact, read_pos in rows:
        archived = (channel is not None and not channel.active) or (
            contact is not None and not contact.on_radio
        )
        if conv.kind == "channel" and archived and conv.id not in previews:
            continue  # nothing to show for an empty, retired channel generation
        if contact is not None and contact.blocked and conv_id is None:
            continue  # blocked contacts' DMs are hidden from the list (still reachable directly)
        p = previews.get(conv.id)
        out.append(
            ConversationOut(
                id=conv.id,
                kind=conv.kind,
                title=(contact.alias or contact.name) if contact else conv.title,
                # One star per person: a DM's star is its contact's favourite flag on the radio.
                favorite=contact.favorite if contact else conv.favorite,
                muted=conv.muted,
                sound=conv.sound,
                blocked=bool(contact and contact.blocked),
                last_message_at=conv.last_message_at,
                last_position=conv.last_position,
                read_position=read_pos or 0,
                unread=unread.get(conv.id, 0),
                is_simulated=radio.is_simulated,
                archived=archived,
                channel_slot=channel.slot if channel else None,
                contact_id=contact.id if contact else None,
                contact_public_key=contact.public_key if contact else None,
                peer_prefix=conv.peer_prefix,
                max_bytes=channel_budget(radio.name) if conv.kind == "channel" else DM_MAX_BYTES,
                preview=Preview(
                    body=p.body[:140],
                    direction=p.direction,
                    sender_label=p.sender_label,
                    state=p.state,
                    created_at=p.created_at,
                )
                if p
                else None,
            )
        )
    out.sort(
        key=lambda c: (
            c.last_message_at is None,
            -(c.last_message_at.timestamp() if c.last_message_at else 0),
        )
    )
    return out


# ---- conversations ----------------------------------------------------------------------


@router.get("/conversations", response_model=list[ConversationOut])
async def list_conversations(ctx: AuthContext = Depends(require_auth), db: AsyncSession = Depends(get_db)):
    return await _conversation_rows(db, ctx.user.id)


@router.get("/conversations/{conv_id}", response_model=ConversationOut)
async def get_conversation(
    conv_id: uuid.UUID, ctx: AuthContext = Depends(require_auth), db: AsyncSession = Depends(get_db)
):
    rows = await _conversation_rows(db, ctx.user.id, conv_id)
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    return rows[0]


@router.patch("/conversations/{conv_id}", status_code=204)
async def patch_conversation(
    conv_id: uuid.UUID,
    body: ConversationPatch,
    ctx: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    conv = await _conversation(db, conv_id)
    if body.favorite is not None:
        contact = await db.get(Contact, conv.contact_id) if conv.contact_id else None
        if contact is not None:
            # Starring a person sets their favourite flag on the radio (and the bot follows it).
            await radio_contact_op(db, contact, "contact_favorite", {"favorite": body.favorite})
        else:
            conv.favorite = body.favorite
    if body.muted is not None:
        conv.muted = body.muted
    if body.sound is not None:
        conv.sound = None if body.sound == "default" else body.sound
    await db.commit()
    hub.publish("conversations-updated")


class ConversationContactInfo(BaseModel):
    id: uuid.UUID
    name: str
    alias: str | None
    public_key: str
    kind: int
    last_advert_at: datetime | None
    on_radio: bool


class ConversationChannelInfo(BaseModel):
    slot: int
    name: str
    generation: int
    active: bool
    flood_scope: str | None


class ConversationStats(BaseModel):
    total: int
    incoming: int
    outgoing: int
    first_message_at: datetime | None
    last_message_at: datetime | None


class ConversationInfo(BaseModel):
    conversation: ConversationOut
    created_at: datetime
    radio_name: str
    contact: ConversationContactInfo | None
    channel: ConversationChannelInfo | None
    stats: ConversationStats
    # What DELETE /conversations/{id} will do: remove the conversation, or only clear its history.
    delete_action: str


def _delete_action(conv: Conversation, channel: Channel | None) -> str:
    # An active channel is still configured on the radio, so its conversation must stay
    # (new messages for that slot need somewhere to land); only its history can be cleared.
    return "clear" if conv.kind == "channel" and channel is not None and channel.active else "delete"


@router.get("/conversations/{conv_id}/info", response_model=ConversationInfo)
async def conversation_info(
    conv_id: uuid.UUID, ctx: AuthContext = Depends(require_auth), db: AsyncSession = Depends(get_db)
):
    rows = await _conversation_rows(db, ctx.user.id, conv_id)
    conv = await _conversation(db, conv_id)
    radio = await db.get(Radio, conv.radio_id)
    channel = await db.get(Channel, conv.channel_id) if conv.channel_id else None
    contact = await db.get(Contact, conv.contact_id) if conv.contact_id else None
    total, incoming, first_at, last_at = (
        await db.execute(
            select(
                func.count(),
                func.count().filter(Message.direction == "in"),
                func.min(Message.created_at),
                func.max(Message.created_at),
            ).where(Message.conversation_id == conv_id, Message.suppressed.is_(False))
        )
    ).one()
    # _conversation_rows hides empty retired channels; build the summary directly in that case.
    summary = rows[0] if rows else None
    if summary is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    return ConversationInfo(
        conversation=summary,
        created_at=conv.created_at,
        radio_name=radio.name,
        contact=ConversationContactInfo(
            id=contact.id,
            name=contact.name,
            alias=contact.alias,
            public_key=contact.public_key,
            kind=contact.kind,
            last_advert_at=contact.last_advert_at,
            on_radio=contact.on_radio,
        )
        if contact
        else None,
        channel=ConversationChannelInfo(
            slot=channel.slot,
            name=channel.name,
            generation=channel.generation,
            active=channel.active,
            flood_scope=channel.flood_scope,
        )
        if channel
        else None,
        stats=ConversationStats(
            total=total,
            incoming=incoming,
            outgoing=total - incoming,
            first_message_at=first_at,
            last_message_at=last_at,
        ),
        delete_action=_delete_action(conv, channel),
    )


@router.delete("/conversations/{conv_id}")
async def delete_conversation(
    conv_id: uuid.UUID, ctx: AuthContext = Depends(require_auth), db: AsyncSession = Depends(get_db)
):
    """Delete from this archive only. Nothing is transmitted and the radio's configuration is unchanged."""
    conv = await _conversation(db, conv_id)
    channel = await db.get(Channel, conv.channel_id) if conv.channel_id else None
    action = _delete_action(conv, channel)
    removed = (await db.execute(sa_delete(Message).where(Message.conversation_id == conv.id))).rowcount or 0
    if action == "clear":
        conv.last_message_at = None  # last_position stays: positions are never reused
    else:
        # Read positions cascade with the conversation. A DM reappears if the contact writes again.
        await db.execute(sa_delete(Conversation).where(Conversation.id == conv.id))
    db.add(
        AuditEvent(
            kind="conversation.cleared" if action == "clear" else "conversation.deleted",
            detail={"kind": conv.kind, "messages": removed},
        )
    )
    await db.commit()
    hub.publish("conversations-updated")
    hub.publish("delivery-updated", conversation_id=str(conv_id))
    return {"action": "cleared" if action == "clear" else "deleted", "messages_removed": removed}


@router.get("/conversations/{conv_id}/messages", response_model=MessagePage)
async def list_messages(
    conv_id: uuid.UUID,
    before: int | None = Query(default=None, ge=1),
    after: int | None = Query(default=None, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    ctx: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    await _conversation(db, conv_id)
    q = select(Message).where(Message.conversation_id == conv_id, Message.suppressed.is_(False))
    if after is not None:
        rows = (
            (await db.execute(q.where(Message.position > after).order_by(Message.position).limit(limit + 1)))
            .scalars()
            .all()
        )
        return MessagePage(messages=rows[:limit], has_more=len(rows) > limit)
    if before is not None:
        q = q.where(Message.position < before)
    rows = (await db.execute(q.order_by(Message.position.desc()).limit(limit + 1))).scalars().all()
    page = list(reversed(rows[:limit]))
    return MessagePage(messages=page, has_more=len(rows) > limit)


@router.post("/conversations/{conv_id}/messages", response_model=MessageOut)
async def send_message(
    conv_id: uuid.UUID,
    body: SendRequest,
    ctx: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    conv = await _conversation(db, conv_id)
    # Idempotent replay first: a retried HTTP request must return the original, even if offline now.
    existing = (
        await db.execute(select(Message).where(Message.client_message_id == body.client_message_id))
    ).scalar_one_or_none()
    if existing is not None:
        if existing.body != body.body or existing.conversation_id != conv.id:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "client_message_id was already used for another message"
            )
        return existing

    radio = await db.get(Radio, conv.radio_id)
    if not supervisor.connected or supervisor.radio is None or supervisor.radio.id != conv.radio_id:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "The radio for this conversation is offline. Your draft has been kept."
        )
    if conv.kind == "dm" and conv.contact_id is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This sender is not a known contact, so replies cannot be sent"
        )
    if not body.body.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Message is empty")
    budget = channel_budget(radio.name) if conv.kind == "channel" else DM_MAX_BYTES
    size = len(body.body.encode("utf-8"))
    if size > budget:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Message is {size} bytes; the limit here is {budget}. Please shorten it.",
        )
    key = str(ctx.user.id)
    if send_limiter.blocked(key):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Sending too quickly; wait a moment")
    send_limiter.hit(key)

    try:
        msg, created = await messaging.create_outgoing(
            db,
            conv,
            body.body,
            body.client_message_id,
            get_settings().send_expiry_seconds,
            radio.is_simulated,
        )
    except messaging.IdempotencyConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "client_message_id was already used") from exc
    await db.commit()
    if created:
        hub.publish("message-created", conversation_id=str(conv.id), message_id=str(msg.id))
        supervisor.wake_sender()
    return msg


@router.post("/messages/{message_id}/retry", response_model=MessageOut)
async def retry_message(
    message_id: uuid.UUID,
    body: RetryRequest,
    ctx: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    msg = await db.get(Message, message_id, with_for_update=True)
    if msg is None or msg.direction != "out":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Message not found")
    if msg.state not in States.RETRYABLE:
        raise HTTPException(status.HTTP_409_CONFLICT, f"A message in state '{msg.state}' cannot be retried")
    if msg.state in {States.UNCERTAIN, States.NO_ACK} and not body.confirm_possible_duplicate:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This message may already have been transmitted. Confirm to send it again (it could arrive twice).",
        )
    if not supervisor.connected:
        raise HTTPException(status.HTTP_409_CONFLICT, "The radio is offline")
    msg.state = States.QUEUED
    msg.error = None
    msg.expected_ack = None
    msg.ack_deadline = None
    msg.expires_at = utcnow() + timedelta(seconds=get_settings().send_expiry_seconds)
    await db.commit()
    hub.publish("delivery-updated", message_id=str(msg.id), conversation_id=str(msg.conversation_id))
    supervisor.wake_sender()
    return msg


@router.put("/conversations/{conv_id}/read-position", status_code=204)
async def put_read_position(
    conv_id: uuid.UUID,
    body: ReadPositionRequest,
    ctx: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    conv = await _conversation(db, conv_id)
    pos = min(body.position, conv.last_position)
    stmt = insert(ReadPosition).values(
        user_id=ctx.user.id, conversation_id=conv.id, position=pos, updated_at=utcnow()
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[ReadPosition.user_id, ReadPosition.conversation_id],
        set_={
            "position": func.greatest(ReadPosition.position, stmt.excluded.position),
            "updated_at": utcnow(),
        },
    )
    await db.execute(stmt)
    await db.commit()
    hub.publish("read-position-updated", conversation_id=str(conv.id))


# ---- contacts ---------------------------------------------------------------------------


# ---- search / export --------------------------------------------------------------------


def _like_escape(q: str) -> str:
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.get("/search", response_model=list[SearchHit])
async def search(
    q: str = Query(min_length=1, max_length=200),
    conversation_id: uuid.UUID | None = None,
    before: int | None = Query(default=None, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
    ctx: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(Message, Conversation, Contact)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .outerjoin(Contact, Contact.id == Conversation.contact_id)
        .where(Message.body.ilike(f"%{_like_escape(q)}%", escape="\\"), Message.suppressed.is_(False))
    )
    if conversation_id:
        stmt = stmt.where(Message.conversation_id == conversation_id)
    if before:
        stmt = stmt.where(Message.position < before)
    rows = (await db.execute(stmt.order_by(Message.position.desc()).limit(limit))).all()
    return [
        SearchHit(
            **MessageOut.model_validate(m).model_dump(),
            conversation_title=(contact.alias or contact.name) if contact else conv.title,
        )
        for m, conv, contact in rows
    ]


@router.get("/export")
async def export_archive(ctx: AuthContext = Depends(require_auth), db: AsyncSession = Depends(get_db)):
    convs = await _conversation_rows(db, ctx.user.id)
    msgs = (await db.execute(select(Message).order_by(Message.position))).scalars().all()
    by_conv: dict[uuid.UUID, list[dict]] = {}
    for m in msgs:
        by_conv.setdefault(m.conversation_id, []).append(MessageOut.model_validate(m).model_dump(mode="json"))
    payload = {
        "exported_at": utcnow().isoformat(),
        "format": "meshhome-export/1",
        "conversations": [
            {**c.model_dump(mode="json", exclude={"preview"}), "messages": by_conv.get(c.id, [])}
            for c in convs
        ],
    }
    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    return JSONResponse(
        payload, headers={"Content-Disposition": f'attachment; filename="meshhome-export-{stamp}.json"'}
    )


# ---- one message: details, sender, paths, delete ----------------------------------------

DIRECT_ROUTE = 255  # path_len of a message that arrived on a known (direct) route


def _contact_brief(c: Contact) -> dict[str, Any]:
    return {
        "id": str(c.id),
        "name": c.name,
        "alias": c.alias,
        "public_key": c.public_key,
        "kind": c.kind,
        "last_advert_at": c.last_advert_at,
        "on_radio": c.on_radio,
        "favorite": c.favorite,
        "blocked": c.blocked,
    }


@router.get("/messages/{message_id}/info")
async def message_info(
    message_id: uuid.UUID, ctx: AuthContext = Depends(require_auth), db: AsyncSession = Depends(get_db)
):
    m = await db.get(Message, message_id)
    if m is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Message not found")
    conv = await db.get(Conversation, m.conversation_id)
    meta = m.meta or {}

    # Sender: the DM's contact, or for a channel the one contact whose advertised name matches the
    # label (names are self-chosen and not verified, so several or none may match).
    contact: Contact | None = None
    match = None
    if m.direction == "in":
        if conv.kind == "dm" and conv.contact_id:
            contact, match = await db.get(Contact, conv.contact_id), "key"
        elif conv.kind == "channel" and m.sender_label:
            named = (
                (
                    await db.execute(
                        select(Contact)
                        .where(Contact.radio_id == conv.radio_id, Contact.name == m.sender_label)
                        .limit(2)
                    )
                )
                .scalars()
                .all()
            )
            if len(named) == 1:
                contact, match = named[0], "name"

    # What the radio reported with the message; gaps are filled from the first copy it heard.
    paths = [p for p in meta.get("paths") or [] if isinstance(p, dict)]
    first = paths[0] if paths else {}
    hops = meta.get("path_len")
    hash_mode = meta.get("path_hash_mode")
    if hops is None and first:
        hops = DIRECT_ROUTE if first.get("route") == "direct" else len(first.get("hops") or [])
    hash_size = hash_mode + 1 if isinstance(hash_mode, int) and hash_mode >= 0 else first.get("hash_size")
    received = {
        "snr": meta.get("SNR", first.get("snr")),
        "rssi": meta.get("RSSI", first.get("rssi")),
        "route": None if hops is None else ("direct" if hops == DIRECT_ROUTE else "flood"),
        "hops": None if hops in (None, DIRECT_ROUTE) else hops,
        "path_hash_size": hash_size if hops != DIRECT_ROUTE else None,
    }

    # Hop hashes are prefixes of repeaters' public keys: name them from the contact list.
    prefixes = {h.lower() for p in paths for h in p.get("hops") or [] if isinstance(h, str)}
    names: dict[str, list[str]] = {x: [] for x in prefixes}
    if prefixes:
        rows = await db.execute(
            select(Contact.public_key, Contact.alias, Contact.name, Contact.kind).where(
                Contact.radio_id == conv.radio_id, or_(*(Contact.public_key.startswith(x) for x in prefixes))
            )
        )
        for key, alias, name, _kind in sorted(rows, key=lambda r: r[3] not in (2, 3)):  # repeaters first
            for x in prefixes:
                if key.startswith(x):
                    names[x].append(alias or name)
    return {
        "message": MessageOut.model_validate(m),
        "conversation_kind": conv.kind,
        "sender": {
            "label": m.sender_label,
            "key_prefix": m.sender_key_prefix,
            "contact": _contact_brief(contact) if contact else None,
            "match": match,
        },
        "received": received,
        "paths": [
            {
                "hops": [{"hash": h, "names": names.get(h.lower(), [])} for h in p.get("hops") or []],
                "hash_size": p.get("hash_size"),
                "route": p.get("route"),
                "snr": p.get("snr"),
                "rssi": p.get("rssi"),
            }
            for p in paths
        ],
    }


@router.delete("/messages/{message_id}", status_code=204)
async def delete_message(
    message_id: uuid.UUID, ctx: AuthContext = Depends(require_auth), db: AsyncSession = Depends(get_db)
):
    """Remove one message from this archive. Nothing is transmitted and the radio is unchanged."""
    m = await db.get(Message, message_id, with_for_update=True)
    if m is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Message not found")
    if m.direction == "out" and m.state in (States.QUEUED, States.SENDING):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This message is still being sent; delete it once it has gone out"
        )
    conv = await db.get(Conversation, m.conversation_id, with_for_update=True)
    await db.delete(m)
    await db.flush()
    conv.last_message_at = (
        await db.execute(
            select(func.max(Message.created_at)).where(
                Message.conversation_id == conv.id, Message.suppressed.is_(False)
            )
        )
    ).scalar()
    db.add(AuditEvent(kind="message.deleted", detail={"conversation": str(conv.id)}))
    await db.commit()
    hub.publish("conversations-updated")
    hub.publish("delivery-updated", conversation_id=str(conv.id))
