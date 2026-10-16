"""Remote administration of repeaters and room servers over the mesh.

Every request costs airtime, so nothing is fetched automatically: the UI asks for one section
at a time and shows what was last fetched (cached per node in an app setting) with its age,
like the MeshCore app's refresh icons. One remote operation runs at a time.

Logins live in memory only and the password is never stored. Commands that carry a password
or key are redacted before they reach the console log, the cache or the audit log.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import UTC, datetime
from typing import Any

from Crypto.PublicKey import ECC

from app import db
from app.radio import identity
from app.radio.base import RemoteTicket
from app.radio.supervisor import supervisor
from app.realtime import hub
from app.services import app_settings

log = logging.getLogger(__name__)

REQUEST_KINDS = ("status", "telemetry", "acl", "neighbours", "owner", "regions")
CONSOLE_MAX = 100
MIN_WAIT = 8.0
MAX_WAIT = 45.0
QUEUE_WAIT = 60.0
SENSITIVE = re.compile(r"^\s*(password|set\s+guest\.password|set\s+prv\.key|get\s+guest\.password)\b", re.I)


class RemoteTimeout(Exception):
    """The node did not answer in time (out of range, busy, or not logged in)."""


class RemoteBusy(Exception):
    """Another remote operation is still waiting for its reply."""


_lock = asyncio.Lock()
_sessions: dict[str, dict[str, Any]] = {}  # public key -> {"admin", "permissions", "at"}
_cli_waiters: dict[str, asyncio.Future[str]] = {}  # 12-hex key prefix -> pending CLI reply
_prefix_to_key: dict[str, str] = {}  # for replies that arrive after we stopped waiting
_cache_lock = asyncio.Lock()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _cache_key(public_key: str) -> str:
    return f"remote:{public_key[:32]}"  # app_settings keys are at most 64 characters


def _wait_for(ticket: RemoteTicket) -> float:
    return min(max(ticket.timeout, MIN_WAIT), MAX_WAIT)


def session(public_key: str) -> dict[str, Any] | None:
    return _sessions.get(public_key)


def busy() -> bool:
    return _lock.locked()


def redact(command: str) -> str:
    m = SENSITIVE.match(command)
    return f"{m.group(1)} ••••" if m else command


# ---- cache ---------------------------------------------------------------------------------


async def load(public_key: str) -> dict[str, Any]:
    async with db.session_factory()() as s:
        data = await app_settings._get(s, _cache_key(public_key))
    data = data or {}
    return {
        "sections": data.get("sections") or {},
        "values": data.get("values") or {},
        "console": data.get("console") or [],
    }


async def _update(public_key: str, fn) -> None:
    async with _cache_lock, db.session_factory()() as s:
        data = await app_settings._get(s, _cache_key(public_key)) or {}
        data = {k: data.get(k) or ({} if k != "console" else []) for k in ("sections", "values", "console")}
        fn(data)
        data["console"] = data["console"][-CONSOLE_MAX:]
        await app_settings._put(s, _cache_key(public_key), data)
        await s.commit()
    hub.publish("remote-updated", public_key=public_key)


def _log(data: dict, direction: str, text: str) -> None:
    data["console"].append({"at": _now(), "dir": direction, "text": text})


async def clear_console(public_key: str) -> None:
    await _update(public_key, lambda d: d.__setitem__("console", []))


# ---- operations ----------------------------------------------------------------------------


async def _exclusive():
    try:
        await asyncio.wait_for(_lock.acquire(), QUEUE_WAIT)
    except TimeoutError:
        raise RemoteBusy("Another remote request is still waiting for its reply.") from None


async def login(public_key: str, password: str) -> dict[str, Any]:
    await _exclusive()
    try:
        ticket = await supervisor.remote_send(public_key, "login", password)
        try:
            result = await supervisor.remote_wait(ticket, _wait_for(ticket))
        except TimeoutError:
            raise RemoteTimeout("No answer to the login. The node may be out of range.") from None
    finally:
        _lock.release()
    if result.get("ok"):
        _sessions[public_key] = {
            "admin": bool(result.get("admin")),
            "permissions": result.get("permissions"),
            "at": _now(),
        }
        role = "admin" if result.get("admin") else "guest"
        await _update(public_key, lambda d: _log(d, "note", f"Logged in ({role})"))
    else:
        _sessions.pop(public_key, None)
        await _update(public_key, lambda d: _log(d, "note", "Login failed: wrong password"))
    return result


async def logout(public_key: str) -> None:
    await _exclusive()
    try:
        ticket = await supervisor.remote_send(public_key, "logout")
        await supervisor.remote_wait(ticket, 1)
    finally:
        _lock.release()
        _sessions.pop(public_key, None)
    await _update(public_key, lambda d: _log(d, "note", "Logged out"))


async def request(public_key: str, kind: str, arg: str | None = None) -> dict[str, Any]:
    if kind not in REQUEST_KINDS:
        raise ValueError(kind)
    await _exclusive()
    try:
        ticket = await supervisor.remote_send(public_key, kind, arg)
        try:
            result = await supervisor.remote_wait(ticket, _wait_for(ticket))
        except TimeoutError:
            hint = "" if kind in ("owner", "regions") else " Log in again if the node was restarted."
            raise RemoteTimeout(f"No answer from the node.{hint}") from None
    finally:
        _lock.release()
    entry = {"data": result, "at": _now()}
    await _update(public_key, lambda d: d["sections"].__setitem__(kind, entry))
    return entry


def _apply_reply(data: dict, command: str, reply: str | None) -> None:
    """Remember values read or successfully written, so the settings forms can show them."""
    if reply is None or SENSITIVE.match(command):
        return
    parts = command.split(None, 2)
    if len(parts) >= 2 and parts[0] == "get":
        value = reply[2:] if reply.startswith("> ") else reply
        data["values"][parts[1]] = {"value": value.strip(), "at": _now()}
    elif len(parts) == 3 and parts[0] == "set" and reply.strip().upper().startswith("OK"):
        data["values"][parts[1]] = {"value": parts[2].strip(), "at": _now()}
    elif parts and parts[0] in ("ver", "board", "clock"):
        data["values"][parts[0]] = {"value": reply.strip(), "at": _now()}


async def cli(public_key: str, command: str, *, log_as: str | None = None) -> str | None:
    """Send one CLI command and wait for its reply (None when the node sends none, like reboot)."""
    shown = log_as or redact(command)
    prefix = public_key[:12]
    await _exclusive()
    try:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        _cli_waiters[prefix] = fut
        supervisor.cli_reply_hook = on_cli_reply  # (again: the supervisor may have been reset)
        _prefix_to_key[prefix] = public_key
        try:
            ticket = await supervisor.remote_send(public_key, "cli", command)
            await _update(public_key, lambda d: _log(d, "out", shown))
            try:
                reply = await asyncio.wait_for(fut, _wait_for(ticket))
            except TimeoutError:
                reply = None
        finally:
            _cli_waiters.pop(prefix, None)
    finally:
        _lock.release()

    def _record(d: dict) -> None:
        if reply is None:
            word = command.split(None, 1)[0] if command.strip() else ""
            _log(d, "note", "No reply (expected after a restart)" if word == "reboot" else "No reply")
            return
        _log(d, "in", "(hidden: contains a password)" if SENSITIVE.match(command) else reply)
        _apply_reply(d, command, reply)

    rebooting = command.split(None, 1)[0].lower() == "reboot"
    if rebooting:
        _sessions.pop(public_key, None)  # the node forgets logins when it restarts
    await _update(public_key, _record)
    if reply is None and not rebooting:
        raise RemoteTimeout(
            "No reply from the node. Check that you are logged in as admin and it is in range."
        )
    return reply


async def on_cli_reply(prefix: str, text: str) -> None:
    """Supervisor hook for CLI replies (txt_type 1) received from any node."""
    fut = _cli_waiters.get(prefix)
    if fut is not None and not fut.done():
        fut.set_result(text)
        return
    public_key = _prefix_to_key.get(prefix)
    if public_key is None:
        log.info("dropping a CLI reply from an unknown node")
        return
    await _update(public_key, lambda d: _log(d, "in", f"{text} (late reply)"))


supervisor.cli_reply_hook = on_cli_reply


# ---- identity key --------------------------------------------------------------------------


def generate_identity(prefix: str, *, deadline: float = 60.0) -> tuple[str, str]:
    """A MeshCore identity whose public key starts with `prefix` (0-4 hex digits).

    Returns (private key hex, public key hex) in the firmware's format: the private key is the
    64-byte expanded Ed25519 key (SHA-512 of the seed, clamped), as `set prv.key` expects. Keys
    starting with 00 or ff are reserved by the firmware and skipped.
    """
    prefix = prefix.lower()
    if not re.fullmatch(r"[0-9a-f]{0,4}", prefix) or prefix[:2] in ("00", "ff"):
        raise ValueError("Use up to 4 hex digits that do not start with 00 or ff.")
    stop = time.monotonic() + deadline
    while True:
        seed = os.urandom(32)
        pub = ECC.construct(curve="Ed25519", seed=seed).public_key().export_key(format="raw").hex()
        if pub.startswith(prefix) and pub[:2] not in ("00", "ff"):
            return identity.expand_seed(seed).hex(), pub
        if time.monotonic() > stop:
            raise ValueError("No key with that prefix was found in time; try a shorter prefix.")


async def change_identity(public_key: str, prefix: str) -> dict[str, Any]:
    prv, pub = await asyncio.to_thread(generate_identity, prefix)
    reply = await cli(public_key, f"set prv.key {prv}", log_as=f"set prv.key •••• (new key {pub[:8]}…)")
    return {"reply": reply, "new_public_key": pub}
