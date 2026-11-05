"""Preferred repeater: send direct traffic through one repeater (say, the one on your roof)
whenever that works, and go around it only when it doesn't.

The radio keeps one route per contact and replaces it with whichever route answered a flood first,
which may skip the repeater (a weak direct link or a neighbour's repeater can win that race). So
MeshHome remembers, per contact, a route that starts at the preferred repeater (contacts.routing)
and puts it on the radio before each direct send: messages, bot replies, room server posts and
remote administration. A remembered route comes from:

- the radio's own stored route, when it starts at the repeater (the radio learned it from a reply);
- an incoming direct message or advert that reached this radio through the repeater: its flood
  path, reversed, is a route back;
- a route set by hand that starts at the repeater.

When two attempts on the route get no ACK, the retry floods as usual (messaging.next_retry) and the
contact goes without the preference for COOLDOWN. When that happens for two contacts in a row, the
repeater itself counts as unavailable until this radio hears a packet it relayed (or
REPEATER_DOWN_SECONDS pass). Channel messages always flood, so none of this applies to them.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Contact
from app.radio.base import MAX_PATH_BYTES, hops_from_path

COOLDOWN = timedelta(minutes=30)
REPEATER_DOWN_SECONDS = 30 * 60
REPEATER_DOWN_AFTER_FAILURES = 2
SOURCES = ("radio", "message", "advert", "manual")


def _iso(ts: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None


def starts_at(hops: Sequence[str], repeater_key: str) -> bool:
    return bool(hops) and bool(hops[0]) and repeater_key.lower().startswith(hops[0].lower())


def route_from_flood(hops: Sequence[str], repeater_key: str) -> list[str] | None:
    """A flood packet's path (repeaters in the order travelled) as a route back from this radio,
    if the last repeater it came through was the preferred one."""
    back = [h.lower() for h in reversed(hops)]
    return back if starts_at(back, repeater_key) else None


def same_route(a: Sequence[str], b: Sequence[str]) -> bool:
    """The same repeaters, allowing for one side having shorter hash prefixes."""
    return len(a) == len(b) and all(x.startswith(y) or y.startswith(x) for x, y in zip(a, b, strict=True))


def paused_until(contact: Contact) -> datetime | None:
    failed = _iso((contact.routing or {}).get("failed_at"))
    return failed + COOLDOWN if failed else None


def enabled(contact: Contact) -> bool:
    return not (contact.routing or {}).get("off")


def remembered(contact: Contact, repeater_key: str) -> list[str]:
    """The remembered route through this repeater, or [] (a route through another one is ignored)."""
    hops = list((contact.routing or {}).get("hops") or [])
    return hops if starts_at(hops, repeater_key) else []


def route_for_radio(contact: Contact, repeater_key: str, hash_size: int, now: datetime) -> str | None:
    """The path (hex, at the radio's hash size) to put on the radio before a direct send, or None
    to leave the radio's own route alone."""
    if not enabled(contact) or contact.public_key == repeater_key:
        return None
    hops = remembered(contact, repeater_key)
    if not hops or any(len(h) < hash_size * 2 for h in hops) or len(hops) * hash_size > MAX_PATH_BYTES:
        return None
    until = paused_until(contact)
    if until and now < until:
        return None
    return "".join(h[: hash_size * 2] for h in hops)


def remember(contact: Contact, hops: Sequence[str], source: str, now: datetime) -> bool:
    """Remember a route through the repeater. A different route replaces the old one and ends a
    pause (it is fresh evidence). The same route heard again on the air is re-dated; from the
    radio it changes nothing (the radio may only be showing the route MeshHome put there). A route
    set by hand also turns the preference on and ends a pause. Returns whether anything changed."""
    hops = [h.lower() for h in hops]
    r: dict[str, Any] = dict(contact.routing or {})
    old = list(r.get("hops") or [])
    if old and same_route(old, hops):
        if source == "radio":
            return False
        if len(hops[0]) < len(old[0]):
            hops = old  # keep the longer (less ambiguous) prefixes
    else:
        r.pop("failed_at", None)
    if source == "manual":
        r.pop("failed_at", None)
        r.pop("off", None)
    r.update(hops=hops, source=source, learned_at=now.isoformat())
    contact.routing = r
    return True


def mark_failed(contact: Contact, now: datetime) -> None:
    contact.routing = {**(contact.routing or {}), "failed_at": now.isoformat()}


def set_enabled(contact: Contact, on: bool) -> None:
    r = dict(contact.routing or {})
    if on:
        r.pop("off", None)
        r.pop("failed_at", None)  # turning it back on means "try it now"
    else:
        r["off"] = True
    contact.routing = r


# ---- learning ------------------------------------------------------------------------------


async def learn_from_contacts(
    db: AsyncSession, radio_id, repeater_key: str, hash_size: int, now: datetime
) -> int:
    """Remember the radio's stored routes that start at the repeater. Returns how many changed."""
    changed = 0
    for c in (await db.execute(select(Contact).where(Contact.radio_id == radio_id))).scalars():
        meta = c.meta or {}
        path_len = int(meta.get("path_len") if meta.get("path_len") is not None else -1)
        hops = hops_from_path(str(meta.get("path_hex") or ""), path_len, hash_size)
        if c.public_key != repeater_key and starts_at(hops, repeater_key):
            changed += remember(c, hops, "radio", now)
    await db.flush()
    return changed


async def contact_by_key(db: AsyncSession, radio_id, key_or_prefix: str) -> Contact | None:
    """The one contact whose key is (or starts with) this; None if none or several match."""
    key = key_or_prefix.lower()
    q = select(Contact).where(Contact.radio_id == radio_id)
    q = q.where(Contact.public_key == key) if len(key) == 64 else q.where(Contact.public_key.startswith(key))
    rows = list((await db.execute(q.limit(2))).scalars())
    return rows[0] if len(rows) == 1 else None


async def learn_from_flood(
    db: AsyncSession,
    radio_id,
    key_or_prefix: str,
    hops: Sequence[str],
    repeater_key: str,
    source: str,
    now: datetime,
    *,
    only_if_none: bool = False,
) -> bool:
    """Remember the route back to whoever sent a flood packet that came through the repeater."""
    route = route_from_flood(hops, repeater_key)
    if route is None:
        return False
    c = await contact_by_key(db, radio_id, key_or_prefix)
    if c is None or c.public_key == repeater_key:
        return False
    if only_if_none and remembered(c, repeater_key):
        return False
    return remember(c, route, source, now)
