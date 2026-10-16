"""Contacts: searchable, filterable, paginated list plus per-contact actions.

Actions that change the radio (favourite flag, path, remove, share, export) go through the radio
supervisor and need a live connection. Alias and block are app-side and always available.
"""

import math
import re
import uuid
from datetime import datetime
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, require_auth
from app.db import get_db
from app.models import AuditEvent, Contact, Conversation, Message, Radio
from app.radio.base import MAX_PATH_BYTES, NotSupported, RadioError, hops_from_path
from app.radio.supervisor import supervisor
from app.realtime import hub
from app.services import messaging

router = APIRouter(prefix="/api/contacts", tags=["contacts"])

PAGE_SIZES = (10, 25, 50)


class ContactOut(BaseModel):
    id: uuid.UUID
    public_key: str
    name: str
    alias: str | None
    kind: int
    last_advert_at: datetime | None
    on_radio: bool
    favorite: bool
    blocked: bool
    is_simulated: bool
    conversation_id: uuid.UUID | None


class ContactPage(BaseModel):
    items: list[ContactOut]
    total: int
    page: int
    page_size: int


class ContactDetail(ContactOut):
    lat: float | None
    lon: float | None
    # -1 = flood (no learned path), 0 = direct, n = via n repeater hops
    path_len: int
    path_hops: list[str]
    path_hash_size: int
    messages_received: int
    messages_sent: int


class ContactMatch(ContactOut):
    lat: float | None
    lon: float | None
    # From your radio's advertised position to this contact's, when both are known.
    distance_km: float | None


class ContactLookup(BaseModel):
    prefix: str
    # Repeaters and room servers first, then the most recently heard. A short prefix (a path hop
    # or a neighbour) can match several contacts; at most 10 are returned.
    items: list[ContactMatch]


class ContactPatch(BaseModel):
    alias: str | None = Field(default=None, max_length=64)
    blocked: bool | None = None


class FavoriteIn(BaseModel):
    favorite: bool


class PathIn(BaseModel):
    # Ordered repeater hops, each the hex hash prefix of a repeater key (or a full key, which is
    # shortened to the radio's hash size). An empty list means "direct, no repeaters".
    hops: list[str] = Field(default_factory=list, max_length=64)


def _out(c: Contact, sim: bool, conv_id: uuid.UUID | None) -> ContactOut:
    return ContactOut(
        id=c.id,
        public_key=c.public_key,
        name=c.name,
        alias=c.alias,
        kind=c.kind,
        last_advert_at=c.last_advert_at,
        on_radio=c.on_radio,
        favorite=c.favorite,
        blocked=c.blocked,
        is_simulated=sim,
        conversation_id=conv_id,
    )


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


@router.get("", response_model=ContactPage)
async def list_contacts(
    q: str | None = Query(default=None, max_length=100),
    kind: int | None = Query(default=None, ge=0, le=255),
    show: Literal["all", "favorites", "blocked", "removed"] = "all",
    sort: Literal["last_heard", "name", "kind"] = "last_heard",
    # Default: newest first for last_heard, A→Z for name and kind.
    order: Literal["asc", "desc"] | None = None,
    favorites_first: bool = False,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25),
    ctx: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    if page_size not in PAGE_SIZES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"page_size must be one of {PAGE_SIZES}")
    base = (
        select(Contact, Radio.is_simulated, Conversation.id)
        .join(Radio, Radio.id == Contact.radio_id)
        .outerjoin(Conversation, Conversation.contact_id == Contact.id)
    )
    if show == "removed":
        base = base.where(Contact.on_radio.is_(False))
    else:
        base = base.where(Contact.on_radio.is_(True))
        if show == "favorites":
            base = base.where(Contact.favorite.is_(True))
        elif show == "blocked":
            base = base.where(Contact.blocked.is_(True))
    if kind is not None:
        base = base.where(Contact.kind == kind)
    if q and q.strip():
        pat = _like(q.strip())
        base = base.where(
            or_(
                Contact.name.ilike(pat, escape="\\"),
                Contact.alias.ilike(pat, escape="\\"),
                Contact.public_key.ilike(pat, escape="\\"),
            )
        )
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    display = func.lower(func.coalesce(Contact.alias, Contact.name))
    desc = (order or ("desc" if sort == "last_heard" else "asc")) == "desc"
    heard = Contact.last_advert_at.desc() if desc else Contact.last_advert_at.asc()
    named = display.desc() if desc else display.asc()
    if sort == "last_heard":
        ordering = [heard.nulls_last(), display]  # never-heard contacts stay at the end
    elif sort == "name":
        ordering = [named, Contact.last_advert_at.desc().nulls_last()]
    else:
        ordering = [Contact.kind.desc() if desc else Contact.kind.asc(), display]
    if favorites_first:
        ordering.insert(0, Contact.favorite.desc())
    ordering.append(Contact.id)  # stable paging when everything else ties
    rows = (await db.execute(base.order_by(*ordering).offset((page - 1) * page_size).limit(page_size))).all()
    return ContactPage(
        items=[_out(c, sim, cid) for c, sim, cid in rows], total=total, page=page, page_size=page_size
    )


def _distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance (haversine), in kilometres."""
    (lat1, lon1), (lat2, lon2) = (tuple(map(math.radians, p)) for p in (a, b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(h))


@router.get("/lookup", response_model=ContactLookup)
async def lookup_contacts(
    prefix: str = Query(min_length=2, max_length=64, pattern=r"^[0-9A-Fa-f]+$"),
    ctx: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """Contacts whose public key starts with `prefix`: who a path hop or a repeater's neighbour is."""
    prefix = prefix.lower()
    rows = (
        await db.execute(
            select(Contact, Radio, Conversation.id)
            .join(Radio, Radio.id == Contact.radio_id)
            .outerjoin(Conversation, Conversation.contact_id == Contact.id)
            .where(Contact.public_key.startswith(prefix))
            .order_by(Contact.kind.in_((2, 3)).desc(), Contact.last_advert_at.desc().nulls_last(), Contact.id)
            .limit(10)
        )
    ).all()
    items = []
    for c, radio, conv_id in rows:
        loc = (radio.self_info or {}).get("location") or {}
        here = (loc.get("lat"), loc.get("lon"))
        known = None not in here and c.lat is not None and c.lon is not None
        items.append(
            ContactMatch(
                **_out(c, radio.is_simulated, conv_id).model_dump(),
                lat=c.lat,
                lon=c.lon,
                distance_km=round(_distance_km(here, (c.lat, c.lon)), 1) if known else None,
            )
        )
    return ContactLookup(prefix=prefix, items=items)


async def _contact(db: AsyncSession, contact_id: uuid.UUID) -> Contact:
    c = await db.get(Contact, contact_id)
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact not found")
    return c


@router.get("/{contact_id}", response_model=ContactDetail)
async def contact_detail(
    contact_id: uuid.UUID, ctx: AuthContext = Depends(require_auth), db: AsyncSession = Depends(get_db)
):
    c = await _contact(db, contact_id)
    radio = await db.get(Radio, c.radio_id)
    conv_id = (await db.execute(select(Conversation.id).where(Conversation.contact_id == c.id))).scalar()
    received = sent = 0
    if conv_id:
        received, sent = (
            await db.execute(
                select(
                    func.count().filter(Message.direction == "in"),
                    func.count().filter(Message.direction == "out"),
                ).where(Message.conversation_id == conv_id)
            )
        ).one()
    meta = c.meta or {}
    size = supervisor.path_hash_size()
    path_len = int(meta.get("path_len", -1) if meta.get("path_len") is not None else -1)
    return ContactDetail(
        **_out(c, radio.is_simulated, conv_id).model_dump(),
        lat=c.lat,
        lon=c.lon,
        path_len=path_len,
        path_hops=hops_from_path(str(meta.get("path_hex") or ""), path_len, size),
        path_hash_size=size,
        messages_received=received,
        messages_sent=sent,
    )


@router.patch("/{contact_id}", response_model=ContactOut)
async def patch_contact(
    contact_id: uuid.UUID,
    body: ContactPatch,
    ctx: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    c = await _contact(db, contact_id)
    fields = body.model_fields_set
    if "alias" in fields:
        c.alias = (body.alias or "").strip() or None
    if "blocked" in fields and body.blocked is not None and body.blocked != c.blocked:
        changed = await messaging.set_contact_blocked(db, c, body.blocked)
        db.add(
            AuditEvent(
                kind="contact.blocked" if body.blocked else "contact.unblocked", detail={"messages": changed}
            )
        )
    await db.commit()
    hub.publish("contacts-updated")
    hub.publish("conversations-updated")
    hub.publish("delivery-updated")  # refetch open threads so hidden/restored messages update
    radio = await db.get(Radio, c.radio_id)
    conv_id = (await db.execute(select(Conversation.id).where(Conversation.contact_id == c.id))).scalar()
    return _out(c, radio.is_simulated, conv_id)


async def radio_contact_op(
    db: AsyncSession, c: Contact, op: str, params: dict[str, Any] | None = None
) -> dict:
    if not c.on_radio:
        raise HTTPException(status.HTTP_409_CONFLICT, "This contact is no longer on the radio")
    if supervisor.radio is None or supervisor.radio.id != c.radio_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "The radio for this contact is not connected")
    try:
        result = await supervisor.configure_node(op, {"public_key": c.public_key, **(params or {})})
    except NotSupported as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except (RadioError, TimeoutError) as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"The radio did not accept this: {exc}") from exc
    db.add(AuditEvent(kind=f"node.{op}", detail={"contact": c.public_key[:12]}))
    await db.commit()
    return result


@router.post("/{contact_id}/favorite", response_model=ContactOut)
async def set_favorite(
    contact_id: uuid.UUID,
    body: FavoriteIn,
    ctx: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    c = await _contact(db, contact_id)
    await radio_contact_op(db, c, "contact_favorite", {"favorite": body.favorite})
    hub.publish("conversations-updated")  # a DM's star is the contact's favourite
    await db.refresh(c)
    radio = await db.get(Radio, c.radio_id)
    return _out(c, radio.is_simulated, None)


@router.post("/{contact_id}/reset-path", status_code=204)
async def reset_path(
    contact_id: uuid.UUID, ctx: AuthContext = Depends(require_auth), db: AsyncSession = Depends(get_db)
):
    await radio_contact_op(db, await _contact(db, contact_id), "contact_reset_path")


@router.put("/{contact_id}/path", status_code=204)
async def set_path(
    contact_id: uuid.UUID,
    body: PathIn,
    ctx: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    c = await _contact(db, contact_id)
    size = supervisor.path_hash_size()
    hops = []
    for h in body.hops:
        h = h.strip().lower()
        if not re.fullmatch(r"[0-9a-f]+", h) or len(h) < size * 2:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, f"Each hop must be at least {size * 2} hex characters"
            )
        hops.append(h[: size * 2])
    if len(hops) * size > MAX_PATH_BYTES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Path is too long for the radio")
    await radio_contact_op(db, c, "contact_set_path", {"path_hex": "".join(hops)})


@router.delete("/{contact_id}", status_code=204)
async def remove_contact(
    contact_id: uuid.UUID, ctx: AuthContext = Depends(require_auth), db: AsyncSession = Depends(get_db)
):
    """Remove from the radio. The archive keeps the contact (shown under "Removed") and its messages."""
    await radio_contact_op(db, await _contact(db, contact_id), "contact_remove")


@router.post("/{contact_id}/share")
async def share_contact(
    contact_id: uuid.UUID, ctx: AuthContext = Depends(require_auth), db: AsyncSession = Depends(get_db)
):
    """Re-broadcast this contact's advert to nearby nodes (zero hop)."""
    await radio_contact_op(db, await _contact(db, contact_id), "contact_share")
    return {"ok": True}


@router.get("/{contact_id}/export")
async def export_contact(
    contact_id: uuid.UUID, ctx: AuthContext = Depends(require_auth), db: AsyncSession = Depends(get_db)
):
    """A meshcore:// contact card that other MeshCore apps can import."""
    result = await radio_contact_op(db, await _contact(db, contact_id), "contact_export")
    if not result.get("uri"):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "The radio returned no contact card")
    return {"uri": result["uri"]}


@router.post("/refresh")
async def refresh_contacts(ctx: AuthContext = Depends(require_auth)):
    try:
        n = await supervisor.refresh_contacts()
    except (RadioError, TimeoutError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Could not refresh contacts: {exc}") from exc
    return {"count": n}


class ContactImport(BaseModel):
    """A contact from a QR code (``uri``) or typed in (``public_key``, ``name``, ``kind``)."""

    uri: str | None = Field(default=None, max_length=1024)
    public_key: str | None = Field(default=None, max_length=64)
    name: str | None = Field(default=None, max_length=64)
    kind: int = Field(default=1, ge=1, le=4)


class ContactImportResult(BaseModel):
    contact: ContactOut
    added: bool  # false: it was already one of your contacts (left unchanged)


def _parse_contact_uri(uri: str) -> tuple[str, str, int]:
    """meshcore://contact/add?name=…&public_key=<64 hex>&type=1 (docs.meshcore.io/qr_codes)."""
    parts = urlsplit(uri.strip())
    if parts.scheme != "meshcore" or f"{parts.netloc}{parts.path}" != "contact/add":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "That isn't a MeshCore contact code")
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    try:
        kind = int(q.get("type", "1"))
    except ValueError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "The contact code has an invalid type"
        ) from None
    return q.get("public_key", ""), q.get("name", ""), kind


@router.post("/import", response_model=ContactImportResult)
async def import_contact(
    body: ContactImport, ctx: AuthContext = Depends(require_auth), db: AsyncSession = Depends(get_db)
) -> ContactImportResult:
    """Add a contact the radio hasn't heard an advert from yet. It starts with no path (flood)
    and no position; both arrive with the node's next advert."""
    if body.uri:
        key, name, kind = _parse_contact_uri(body.uri)
    else:
        key, name, kind = body.public_key or "", body.name or "", body.kind
    key = key.strip().lower()
    name = name.strip()
    if not re.fullmatch(r"[0-9a-f]{64}", key):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "The public key must be 64 hexadecimal characters"
        )
    if not name:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Enter the contact's name")
    if len(name.encode()) > 31:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "The name is too long (at most 31 bytes)")
    if kind not in (1, 2, 3, 4):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown contact type")
    radio = supervisor.radio
    if radio is None or not supervisor.connected:
        raise HTTPException(status.HTTP_409_CONFLICT, "The radio is not connected")
    if radio.public_key and radio.public_key.lower() == key:
        raise HTTPException(status.HTTP_409_CONFLICT, "That's this radio's own contact code")

    async def find() -> Contact | None:
        return (
            await db.execute(select(Contact).where(Contact.radio_id == radio.id, Contact.public_key == key))
        ).scalar_one_or_none()

    existing = await find()
    if existing is not None and existing.on_radio:
        return ContactImportResult(contact=_out(existing, radio.is_simulated, None), added=False)
    try:
        await supervisor.configure_node("contact_add", {"public_key": key, "name": name, "kind": kind})
    except NotSupported as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except (RadioError, TimeoutError) as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"The radio did not accept this: {exc}") from exc
    db.add(AuditEvent(kind="node.contact_add", detail={"contact": key[:12]}))
    await db.commit()
    db.expire_all()
    added = await find()
    if added is None:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "The radio accepted the contact but doesn't list it")
    return ContactImportResult(contact=_out(added, radio.is_simulated, None), added=True)


@router.post("/{contact_id}/conversation")
async def open_dm(
    contact_id: uuid.UUID, ctx: AuthContext = Depends(require_auth), db: AsyncSession = Depends(get_db)
):
    contact = await _contact(db, contact_id)
    conv = await messaging.dm_conversation_for_contact(db, contact)
    await db.commit()
    hub.publish("conversations-updated")
    return {"conversation_id": str(conv.id)}
