"""A small command bot: answers direct messages that start with "/" (e.g. /info, /ping).

Off until the owner turns it on (Settings → Bot). Only direct messages from known contacts are
considered, never channels; by default only favourite contacts may use it. Replies go through the
normal outgoing queue, so they are archived and delivered like any message the owner sends.

Airtime is precious, so replies are rate limited (per contact and overall; quick follow-up
commands wait their turn instead of being dropped), unknown commands get one short hint, and commands that arrive late (e.g. collected after the radio was offline) are
ignored rather than answered out of context.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from typing import Any

from sqlalchemy import func, select

from app import db
from app.config import APP_VERSION, get_settings
from app.models import Contact, Conversation, Message, Radio
from app.realtime import hub
from app.services import app_settings, bot_apps, bot_recipes, messaging

log = logging.getLogger(__name__)

DM_MAX_BYTES = 150
MAX_AGE_SECONDS = 15 * 60  # ignore commands older than this (sender's clock)
PER_CONTACT_SECONDS = 10.0
GLOBAL_PER_MINUTE = 6
MAX_WAITING = 3  # commands per contact that may queue for their turn
MAX_WAIT_SECONDS = 60.0
DIRECT_ROUTE = 255  # path_len for a message that came along a known (direct) route
# Built in; installed bot apps add their own (Settings → Bot → Apps; app/services/bot_apps.py).
COMMANDS = {
    "help": "list the commands",
    "info": "about this node",
    "ping": "how your message arrived",
}

_last_reply: dict[uuid.UUID, float] = {}
_recent: deque[float] = deque()
_waiting: dict[uuid.UUID, int] = {}


def _fit(text: str, limit: int = DM_MAX_BYTES) -> str:
    """Trim to the DM size limit in UTF-8 bytes without splitting a character."""
    raw = text.encode()
    if len(raw) <= limit:
        return text
    return raw[: limit - 3].decode(errors="ignore").rstrip() + "..."


def _duration(seconds: float) -> str:
    s = int(max(0, seconds))
    d, h, m = s // 86400, s % 86400 // 3600, s % 3600 // 60
    return f"{d}d {h}h" if d else f"{h}h {m}m" if h else f"{m}m"


def _route(meta: dict[str, Any] | None) -> str:
    meta = meta or {}
    parts = []
    snr = meta.get("SNR")
    if isinstance(snr, int | float):
        parts.append(f"SNR {snr:g} dB")
    rssi = meta.get("RSSI")
    if isinstance(rssi, int | float):
        parts.append(f"RSSI {rssi:g} dBm")
    hops = meta.get("path_len")
    if hops == DIRECT_ROUTE:
        parts.append("direct route")
    elif isinstance(hops, int) and hops >= 0:
        parts.append("0 hops (heard directly)" if hops == 0 else f"{hops} hop{'s' if hops != 1 else ''}")
    return ", ".join(parts)


async def _info(s, radio: Radio, connected_since: float | None) -> str:
    rf = (radio.self_info or {}).get("radio") or {}
    contacts = (
        await s.execute(
            select(func.count())
            .select_from(Contact)
            .where(Contact.radio_id == radio.id, Contact.on_radio.is_(True))
        )
    ).scalar_one()
    parts = [radio.name or "MeshHome", f"MeshHome {APP_VERSION}"]
    if connected_since:
        parts.append(f"up {_duration(time.time() - connected_since)}")
    if rf.get("freq_mhz"):
        parts.append(f"{rf['freq_mhz']:g} MHz SF{rf.get('sf')} BW{rf.get('bw_khz'):g}")
    parts.append(f"{contacts} contacts")
    return " · ".join(parts)


async def _wait_turn(contact_id: uuid.UUID) -> bool:
    """Wait until a reply is allowed (10 s per contact, 6 a minute overall), then claim it.

    Commands sent in quick succession are answered in turn rather than dropped, but no more than
    MAX_WAITING per contact queue up, and none waits longer than MAX_WAIT_SECONDS.
    """
    if _waiting.get(contact_id, 0) >= MAX_WAITING:
        return False
    _waiting[contact_id] = _waiting.get(contact_id, 0) + 1
    deadline = time.monotonic() + MAX_WAIT_SECONDS
    try:
        while True:
            now = time.monotonic()
            while _recent and now - _recent[0] > 60:
                _recent.popleft()
            wait = _last_reply.get(contact_id, -1e9) + PER_CONTACT_SECONDS - now
            if len(_recent) >= GLOBAL_PER_MINUTE:
                wait = max(wait, _recent[0] + 60 - now)
            if wait <= 0:
                _recent.append(now)
                _last_reply[contact_id] = now
                return True
            if now + wait > deadline:
                return False
            await asyncio.sleep(wait)
    finally:
        _waiting[contact_id] -= 1


async def _app_reply(app: bot_apps.Installed, command: str, arg: str | None) -> str:
    try:
        return await bot_recipes.run(app.recipe, command, app.settings, arg)
    except bot_recipes.SourceError as exc:
        # The details (an address, a status) stay in the log; never sent over the air.
        log.info("bot: /%s (%s) unavailable: %s", command, app.id, exc)
        return app.recipe.command(command).reply.unavailable


def parse(body: str) -> tuple[str, str | None] | None:
    """The command word and its argument: "/info" → ("info", None), "/HA Garage now" → ("ha",
    "garage"), "/ping@node" → ("ping", None); None if not a command. The argument is the next word,
    lowercased, at most 20 characters; only commands that read a group use it."""
    text = (body or "").strip()
    if not text.startswith("/") or len(text) < 2:
        return None
    words = text[1:].split(None, 1)
    word = words[0].split("@", 1)[0].lower()
    if not word:
        return None
    return word, bot_recipes.argument(words[1]) if len(words) > 1 else None


async def handle(message_id: uuid.UUID, *, connected_since: float | None = None) -> str | None:
    """Answer one received message if it is a bot command. Returns the reply text, if any."""
    async with db.session_factory()() as s:
        cfg = await app_settings.get_bot_config(s)
        if not cfg.enabled:
            return None
        msg = await s.get(Message, message_id)
        if msg is None or msg.direction != "in" or msg.suppressed or (msg.txt_type or 0) != 0:
            return None
        parsed = parse(msg.body)
        if parsed is None:
            return None
        command, arg = parsed
        conv = await s.get(Conversation, msg.conversation_id)
        if conv is None or conv.kind != "dm" or conv.contact_id is None:
            return None
        contact = await s.get(Contact, conv.contact_id)
        if contact is None or contact.blocked:
            return None
        if cfg.allow == "favorites" and not contact.favorite:
            log.info("bot: ignoring /%s from a contact that is not a favourite", command)
            return None
        if msg.sender_timestamp and time.time() - msg.sender_timestamp > MAX_AGE_SECONDS:
            log.info("bot: ignoring a /%s sent %d s ago", command, time.time() - msg.sender_timestamp)
            return None
        contact_id, meta = contact.id, dict(msg.meta or {})

    # Outside the database session: this can wait several seconds for its turn.
    if not await _wait_turn(contact_id):
        log.info("bot: too many commands waiting, not answering /%s", command)
        return None

    async with db.session_factory()() as s:
        conv = await s.get(Conversation, conv.id)
        radio = await s.get(Radio, conv.radio_id)
        app = None if command in COMMANDS else await bot_apps.find_command(s, command)
        if command == "help":
            listed = {k: (f"/{k}", v) for k, v in COMMANDS.items()}
            for a in await bot_apps.installed(s):
                if a.enabled:
                    for c in a.recipe.commands:
                        listed[c.name] = (f"/{c.name} <group>" if c.group else f"/{c.name}", c.help)
            reply = "Commands: " + ", ".join(f"{k} ({v})" for k, v in listed.values())
        elif app is not None:
            reply = await _app_reply(app, command, arg)
        elif command == "info":
            reply = await _info(s, radio, connected_since)
        elif command == "ping":
            route = _route(meta)
            reply = f"pong · {route}" if route else "pong"
        else:
            reply = f"Unknown command /{command[:20]}. Try /help"
        reply = _fit(reply)

        # Derived from the command's id: handling the same message twice cannot send twice.
        out, created = await messaging.create_outgoing(
            s,
            conv,
            reply,
            f"bot-{message_id.hex}",
            get_settings().send_expiry_seconds,
            radio.is_simulated,
        )
        if created:
            out.meta = {"bot": command}
        await s.commit()
    if created:
        hub.publish("message-created", conversation_id=str(conv.id), message_id=str(out.id))
    return reply
