"""The one component that owns the radio connection.

Responsibilities: load the saved radio config, take the PostgreSQL ownership lock, connect
with exponential backoff, drain received messages into the database one at a time, run the
outgoing queue, correlate ACKs, and record collection gaps. One supervisor per process; the
app runs with a single worker.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import timedelta

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection

from app import db
from app.config import get_settings
from app.models import Channel, CollectionGap, Contact, Conversation, Message, Radio, SendAttempt, utcnow
from app.radio import packets
from app.radio.base import IncomingMessage, RadioAdapter, RadioChannel, RadioError, RemoteTicket
from app.radio.meshcore_tcp import MeshCoreTcpRadio
from app.radio.simulated import SimulatedRadio
from app.realtime import hub
from app.services import app_settings, bot, messaging, radio_hat, web_push
from app.services.messaging import States
from app.services.msg_paths import PathTracker

log = logging.getLogger(__name__)

OWNERSHIP_LOCK_ID = 0x4D43_5241  # "MCRA" — one radio owner per database
FALLBACK_FETCH_SECONDS = 60
CONTACT_REFRESH_SECONDS = 30 * 60
# After the radio reports an advert or path change: wait for a burst to settle, and re-read the
# contact table (over the local link, not the air) at most this often.
CONTACT_PUSH_SETTLE_SECONDS = 3.0
CONTACT_PUSH_MIN_INTERVAL = 30.0
HEARTBEAT_SECONDS = 15
BACKOFF_MAX = 30.0
SEND_SPACING_SECONDS = 0.5
COMMAND_TIMEOUT = 20.0
TXT_CLI_DATA = 1  # MeshCore TxtType.CLI_DATA: a remote node's reply to a CLI command


@dataclass
class RadioStatus:
    state: str = (
        "starting"  # disabled|not_configured|paused|connecting|connected|backoff|lock_unavailable|starting
    )
    detail: str = ""
    mode: str = "none"
    is_simulated: bool = False
    radio_id: str | None = None
    radio_name: str | None = None
    connected_since: float | None = None
    last_interaction_at: float | None = None
    last_error: str | None = None
    next_retry_at: float | None = None
    reconnects: int = 0
    received: int = 0
    sent: int = 0
    storage_warning: str | None = None
    extra: dict = field(default_factory=dict)


class RadioSupervisor:
    def __init__(self) -> None:
        self.status = RadioStatus()
        self._task: asyncio.Task | None = None
        self._reload = asyncio.Event()
        self._recv_wake = asyncio.Event()
        self._send_wake = asyncio.Event()
        self._contacts_dirty = asyncio.Event()
        self._cmd_lock = asyncio.Lock()
        self._adapter: RadioAdapter | None = None
        self._radio: Radio | None = None
        self._early_acks: dict[str, float] = {}
        # Remote administration: CLI replies (txt_type 1) go here instead of the chat archive.
        self.cli_reply_hook: Callable[[str, str], Awaitable[None]] | None = None
        self._bot_tasks: set[asyncio.Task] = set()
        # Message paths from the radio's raw packet log (see services/msg_paths.py).
        self._paths = PathTracker()
        self._channel_hashes: dict[int, int] = {}  # slot -> 1-byte channel hash (never the key)
        self._stopping = False

    # ---- lifecycle -------------------------------------------------------------------

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="radio-supervisor")

    async def stop(self) -> None:
        self._stopping = True
        self._reload.set()
        if self._task:
            self._task.cancel()
            try:
                await asyncio.wait_for(self._task, timeout=15)
            except (asyncio.CancelledError, TimeoutError):
                pass
        await self._open_gap("application stopped")

    def reload(self) -> None:
        """Re-read saved settings and reconnect (called after config/pause changes)."""
        self._reload.set()

    @property
    def connected(self) -> bool:
        return self.status.state == "connected" and self._adapter is not None

    @property
    def adapter(self) -> RadioAdapter | None:
        return self._adapter

    @property
    def radio(self) -> Radio | None:
        return self._radio

    def _set(self, state: str, detail: str = "", **kw) -> None:
        changed = state != self.status.state or detail != self.status.detail
        self.status.state = state
        self.status.detail = detail
        for k, v in kw.items():
            setattr(self.status, k, v)
        if changed:
            log.info("radio state: %s %s", state, f"({detail})" if detail else "")
            hub.publish("radio-status-changed", state=state)

    def snapshot(self) -> dict:
        return asdict(self.status)

    async def _wait_reload(self, timeout: float | None = None) -> bool:
        try:
            await asyncio.wait_for(self._reload.wait(), timeout)
            return True
        except TimeoutError:
            return False
        finally:
            self._reload.clear()

    # ---- main loop -------------------------------------------------------------------

    async def _run(self) -> None:
        settings = get_settings()
        backoff = 1.0
        async with db.session_factory()() as s:
            n = await messaging.mark_interrupted_sends_uncertain(s)
            await s.commit()
            if n:
                log.warning("%d interrupted send(s) marked uncertain", n)
        while not self._stopping:
            async with db.session_factory()() as s:
                cfg = await app_settings.get_radio_config(s)
            self.status.mode = cfg.mode
            if not settings.radio_enabled:
                self._set("disabled", "RADIO_ENABLED=false for this deployment")
                await self._wait_reload()
                continue
            if cfg.mode == "none" or (cfg.mode == "tcp" and not cfg.host):
                self._set("not_configured", "No radio configured yet — set one up in Settings")
                await self._wait_reload()
                continue
            if cfg.paused:
                self._set("paused", "Maintenance pause: radio connection released")
                await self._wait_reload()
                continue

            outcome = await self._session(cfg)
            if self._stopping:
                break
            if outcome == "reload":
                backoff = 1.0
                continue
            if outcome == "was_connected":
                backoff = 1.0
            delay = min(BACKOFF_MAX, backoff) * random.uniform(0.7, 1.3)
            backoff = min(BACKOFF_MAX, backoff * 2)
            self.status.reconnects += 1
            self._set(
                "backoff" if self.status.state != "lock_unavailable" else "lock_unavailable",
                self.status.last_error or "retrying",
                next_retry_at=time.time() + delay,
            )
            if await self._wait_reload(delay):
                backoff = 1.0
            self.status.next_retry_at = None

    async def _session(self, cfg: app_settings.RadioConfig) -> str:
        """One connection lifetime. Returns 'reload', 'was_connected' or 'failed'."""
        lock_conn: AsyncConnection | None = None
        was_connected = False
        try:
            lock_conn = await db.engine().connect()
            got = (
                await lock_conn.execute(text("SELECT pg_try_advisory_lock(:id)"), {"id": OWNERSHIP_LOCK_ID})
            ).scalar_one()
            await lock_conn.commit()
            if not got:
                self.status.last_error = "Another app instance owns the radio; serving history only"
                self._set("lock_unavailable", self.status.last_error)
                return "failed"

            adapter = self._build_adapter(cfg)
            adapter.on_ack = self._on_ack
            adapter.on_messages_waiting = self._on_waiting
            adapter.on_contacts_changed = self._on_contacts_changed
            adapter.on_rx_packet = self._on_rx_packet
            disconnected = asyncio.Event()

            async def _on_disconnect(reason: str) -> None:
                self.status.last_error = f"connection lost: {reason}"
                disconnected.set()

            adapter.on_disconnect = _on_disconnect
            target = {
                "simulated": "simulated radio",
                "hat": f"the radio HAT ({radio_hat.HOST}:{radio_hat.PORT})",
            }.get(cfg.mode, f"{cfg.host}:{cfg.port}")
            self._set("connecting", f"Connecting to {target}", is_simulated=cfg.mode == "simulated")
            await asyncio.wait_for(adapter.connect(), COMMAND_TIMEOUT)
            self._adapter = adapter
            await self._initial_sync(adapter)
            was_connected = True
            self._set(
                "connected",
                f"Connected to {target}",
                connected_since=time.time(),
                last_error=None,
                radio_id=str(self._radio.id) if self._radio else None,
                radio_name=self._radio.name if self._radio else None,
            )
            await self._close_gaps()
            hub.publish("contacts-updated")
            hub.publish("conversations-updated")
            self._recv_wake.set()
            self._send_wake.set()

            tasks = [
                asyncio.create_task(self._receive_loop(), name="radio-receive"),
                asyncio.create_task(self._send_loop(), name="radio-send"),
                asyncio.create_task(self._heartbeat(lock_conn), name="radio-heartbeat"),
                asyncio.create_task(self._contact_refresher(), name="radio-contacts"),
                asyncio.create_task(disconnected.wait(), name="radio-disconnect-watch"),
                asyncio.create_task(self._reload.wait(), name="radio-reload-watch"),
            ]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for t in done:
                if t.get_name() == "radio-reload-watch":
                    self._reload.clear()
                    await self._open_gap("radio settings changed")
                    return "reload"
                exc = t.exception() if not t.cancelled() else None
                if exc:
                    self.status.last_error = _safe_error(exc)
            await self._open_gap(self.status.last_error or "connection lost")
            return "was_connected"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - supervisor must survive anything
            self.status.last_error = _safe_error(exc)
            log.warning("radio session failed: %s", self.status.last_error)
            await self._open_gap(self.status.last_error)
            return "was_connected" if was_connected else "failed"
        finally:
            adapter, self._adapter = self._adapter, None
            if adapter:
                try:
                    await adapter.disconnect()
                except Exception:  # noqa: BLE001
                    pass
            if lock_conn is not None:
                try:
                    await lock_conn.execute(text("SELECT pg_advisory_unlock_all()"))
                    await lock_conn.close()
                except Exception:  # noqa: BLE001
                    pass
            self.status.connected_since = None

    def _build_adapter(self, cfg: app_settings.RadioConfig) -> RadioAdapter:
        if cfg.mode == "simulated":
            return SimulatedRadio(interval_seconds=cfg.sim_interval_seconds)
        if cfg.mode == "hat":
            return MeshCoreTcpRadio(radio_hat.HOST, radio_hat.PORT)
        return MeshCoreTcpRadio(cfg.host, cfg.port)

    async def _cmd(self, coro_fn, *args, timeout: float = COMMAND_TIMEOUT):
        async with self._cmd_lock:
            result = await asyncio.wait_for(coro_fn(*args), timeout)
        self.status.last_interaction_at = time.time()
        return result

    async def _initial_sync(self, adapter: RadioAdapter) -> None:
        snap = await self._cmd(adapter.get_device_snapshot)
        await self._cmd(adapter.sync_clock, int(time.time()))
        contacts = await self._cmd(adapter.get_contacts)
        channels = await self._cmd(adapter.get_channels)
        self._remember_channels(channels)
        async with db.session_factory()() as s:
            radio = await messaging.upsert_radio(s, snap)
            await messaging.sync_contacts(s, radio, contacts)
            await messaging.sync_channels(s, radio, channels, await app_settings.get_fingerprint_key(s))
            await s.commit()
            self._radio = radio

    async def refresh_contacts(self) -> int:
        adapter = self._adapter
        if not adapter or not self._radio:
            raise RadioError("radio is not connected")
        contacts = await self._cmd(adapter.get_contacts)
        async with db.session_factory()() as s:
            radio = await s.get(Radio, self._radio.id)
            n = await messaging.sync_contacts(s, radio, contacts)
            await s.commit()
        hub.publish("contacts-updated")
        return n

    # ---- node configuration ----------------------------------------------------------

    async def read_node_config(self) -> dict:
        adapter = self._adapter
        if adapter is None or not self.connected:
            raise RadioError("radio is not connected")
        # Reads every channel slot, so allow longer than a single command.
        cfg = await self._cmd(adapter.read_config, timeout=60)
        # Region scopes are app-side; report them alongside the radio's channels.
        if self._radio is not None and cfg.get("channels"):
            async with db.session_factory()() as s:
                scopes = dict(
                    (
                        await s.execute(
                            select(Channel.slot, Channel.flood_scope).where(
                                Channel.radio_id == self._radio.id, Channel.active.is_(True)
                            )
                        )
                    ).all()
                )
            for ch in cfg["channels"]:
                ch["flood_scope"] = scopes.get(ch["slot"]) if ch.get("name") else None
        return cfg

    def path_hash_size(self) -> int:
        return self._adapter.path_hash_size() if self._adapter else 1

    async def radio_channels(self) -> list[RadioChannel]:
        """The radio's channels including keys. Keys never leave the server."""
        adapter = self._adapter
        if adapter is None or not self.connected:
            raise RadioError("radio is not connected")
        return await self._cmd(adapter.get_channels)

    async def current_channel_secret(self, slot: int) -> bytes | None:
        """Used only to rename a channel while keeping its key; never leaves the server."""
        for ch in await self.radio_channels():
            if ch.slot == slot and any(ch.secret):
                return ch.secret
        return None

    async def set_channel_scope(self, slot: int, scope: str | None) -> str | None:
        """Set the region scope of the active channel in `slot`; returns its conversation id."""
        if self._radio is None:
            return None
        async with db.session_factory()() as s:
            row = (
                await s.execute(
                    select(Channel, Conversation.id)
                    .join(Conversation, Conversation.channel_id == Channel.id)
                    .where(Channel.radio_id == self._radio.id, Channel.slot == slot, Channel.active.is_(True))
                )
            ).first()
            if row is None:
                return None
            ch, conv_id = row
            ch.flood_scope = scope
            await s.commit()
        hub.publish("conversations-updated")
        return str(conv_id)

    async def call(self, method: str, *args, timeout: float = COMMAND_TIMEOUT):
        """One adapter call under the command lock (radio backups use the adapter directly)."""
        adapter = self._adapter
        if adapter is None or not self.connected:
            raise RadioError("radio is not connected")
        return await self._cmd(getattr(adapter, method), *args, timeout=timeout)

    async def configure_node(self, op: str, params: dict) -> dict:
        adapter = self._adapter
        if adapter is None or not self.connected:
            raise RadioError("radio is not connected")
        result = await self._cmd(adapter.configure, op, params, timeout=30)
        if op in (
            "contact_add",
            "contact_favorite",
            "contact_reset_path",
            "contact_set_path",
            "contact_remove",
        ):
            await self.refresh_contacts()
        if op in ("identity", "radio", "channel", "channel_clear"):
            # Refresh what the archive knows: device name/location/RF and channel generations.
            snap = await self._cmd(adapter.get_device_snapshot)
            channels = await self._cmd(adapter.get_channels)
            self._remember_channels(channels)
            async with db.session_factory()() as s:
                radio = await messaging.upsert_radio(s, snap)
                await messaging.sync_channels(s, radio, channels, await app_settings.get_fingerprint_key(s))
                await s.commit()
                self._radio = radio
            self.status.radio_name = snap.name
            hub.publish("conversations-updated")
            hub.publish("contacts-updated")  # refreshes device info and the map
            hub.publish("radio-status-changed", state=self.status.state)
        return result

    # ---- remote administration ------------------------------------------------------------
    # Sending holds the command lock briefly; the reply crosses the mesh and is awaited outside
    # it, so normal sending and receiving carry on meanwhile.

    async def remote_send(self, public_key: str, kind: str, arg: str | None = None) -> RemoteTicket:
        adapter = self._adapter
        if adapter is None or not self.connected:
            raise RadioError("radio is not connected")
        return await self._cmd(adapter.remote_send, public_key, kind, arg, timeout=20)

    async def remote_wait(self, ticket: RemoteTicket, timeout: float):
        adapter = self._adapter
        if adapter is None:
            raise RadioError("radio is not connected")
        return await adapter.remote_wait(ticket, timeout)

    # ---- receive ---------------------------------------------------------------------

    async def _on_waiting(self) -> None:
        self._recv_wake.set()

    async def _receive_loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._recv_wake.wait(), FALLBACK_FETCH_SECONDS)
            except TimeoutError:
                pass
            self._recv_wake.clear()
            while True:
                adapter = self._adapter
                if adapter is None:
                    return
                msg = await self._cmd(adapter.fetch_next_message)
                if msg is None:
                    break
                # Do not fetch the next message until this one is committed.
                await self._persist_incoming(msg)

    def _remember_channels(self, channels: list[RadioChannel]) -> None:
        self._channel_hashes = {c.slot: packets.channel_hash(c.secret) for c in channels if c.secret}

    async def _on_rx_packet(self, raw: bytes, snr: float | None, rssi: float | None) -> None:
        packet = packets.parse(raw)
        if packet is None:
            return
        copy = self._paths.heard(packet, snr, rssi)
        if copy is None:
            return
        message_id, entry = copy
        try:
            async with db.session_factory()() as s:
                m = await s.get(Message, message_id, with_for_update=True)
                if m is None:
                    return
                m.meta = {**(m.meta or {}), "paths": [*(m.meta or {}).get("paths", []), entry]}
                await s.commit()
                conv_id = m.conversation_id
            hub.publish("delivery-updated", message_id=str(message_id), conversation_id=str(conv_id))
        except Exception as exc:  # noqa: BLE001 - paths are a nicety; never disturb collection
            log.info("could not record a message path: %s", _safe_error(exc))

    def _claim_paths(self, msg: IncomingMessage) -> str | None:
        """Attach the routes this message was heard on (msg.meta["paths"]); returns the packet key."""
        own = self._radio.public_key if self._radio else ""
        if msg.kind == "channel" and msg.channel_slot is not None:
            key, entries = self._paths.claim(channel_hash=self._channel_hashes.get(int(msg.channel_slot)))
        elif msg.kind == "dm" and msg.pubkey_prefix:
            key, entries = self._paths.claim(
                sender_hash=int(msg.pubkey_prefix[:2], 16), self_hash=int(own[:2], 16) if own else None
            )
        else:
            return None
        if entries:
            msg.meta = {**(msg.meta or {}), "paths": entries}
        return key

    async def _persist_incoming(self, msg: IncomingMessage) -> None:
        if msg.kind == "dm" and msg.txt_type == TXT_CLI_DATA and msg.pubkey_prefix and self.cli_reply_hook:
            # A repeater / room server answering a remote CLI command: not a chat message.
            try:
                await self.cli_reply_hook(msg.pubkey_prefix, msg.text)
            except Exception as exc:  # noqa: BLE001
                log.warning("could not handle a remote CLI reply: %s", _safe_error(exc))
            return
        path_key = self._claim_paths(msg)
        delay = 1.0
        while True:
            try:
                async with db.session_factory()() as s:
                    radio = await s.get(Radio, self._radio.id)
                    m, created = await messaging.ingest_incoming(s, radio, msg)
                    await s.commit()
                if path_key and created:
                    self._paths.follow(path_key, m.id, len((msg.meta or {}).get("paths", [])))
                if self.status.storage_warning:
                    self.status.storage_warning = None
                    hub.publish("radio-status-changed", state=self.status.state)
                if created:
                    self.status.received += 1
                    hub.publish(
                        "message-created",
                        conversation_id=str(m.conversation_id),
                        message_id=str(m.id),
                        direction="in",
                        kind=msg.kind,
                        suppressed=m.suppressed,
                    )
                    if not m.suppressed:
                        web_push.enqueue(m.id)
                    if msg.kind == "dm" and not m.suppressed and bot.parse(msg.text):
                        task = asyncio.create_task(self._run_bot(m.id), name="bot")
                        self._bot_tasks.add(task)
                        task.add_done_callback(self._bot_tasks.discard)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                # Keep the already-fetched message in memory and stop dequeuing until storage recovers.
                self.status.storage_warning = f"Database unavailable; collection paused ({_safe_error(exc)})"
                log.error("failed to persist received message: %s", _safe_error(exc))
                hub.publish("radio-status-changed", state=self.status.state)
                await asyncio.sleep(delay)
                delay = min(30.0, delay * 2)

    async def _run_bot(self, message_id) -> None:
        try:
            if await bot.handle(message_id, connected_since=self.status.connected_since):
                self.wake_sender()
        except Exception as exc:  # noqa: BLE001 - the bot must never disturb collection
            log.warning("bot could not answer: %s", _safe_error(exc))

    # ---- send ------------------------------------------------------------------------

    def wake_sender(self) -> None:
        self._send_wake.set()

    async def _send_loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._send_wake.wait(), 1.0)
            except TimeoutError:
                pass
            self._send_wake.clear()
            await self._sweep()
            while self._adapter is not None:
                claimed = await self._claim_next()
                if claimed is None:
                    break
                await self._transmit(*claimed)
                await asyncio.sleep(SEND_SPACING_SECONDS)

    async def _sweep(self) -> None:
        async with db.session_factory()() as s:
            changed = await messaging.expire_and_timeout(s)
            await s.commit()
        for mid in changed:
            hub.publish("delivery-updated", message_id=str(mid))
        cutoff = time.time() - 120
        self._early_acks = {k: v for k, v in self._early_acks.items() if v > cutoff}

    async def _claim_next(self):
        """Atomically move the oldest unexpired queued message to 'sending' with an attempt record."""
        async with db.session_factory()() as s:
            m = (
                await s.execute(
                    select(Message)
                    .where(Message.state == States.QUEUED, Message.expires_at > utcnow())
                    .order_by(Message.position)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if m is None:
                return None
            conv = await s.get(Conversation, m.conversation_id)
            # ("channel", slot, region scope) or ("dm", public key)
            target: tuple | None = None
            if conv.kind == "channel" and conv.channel_id:
                ch = await s.get(Channel, conv.channel_id)
                if ch and ch.active:
                    target = ("channel", ch.slot, ch.flood_scope)
            elif conv.contact_id:
                contact = await s.get(Contact, conv.contact_id)
                if contact:
                    target = ("dm", contact.public_key)
            if target is None:
                m.state = States.FAILED
                m.error = "This conversation is no longer available on the radio."
                await s.commit()
                hub.publish("delivery-updated", message_id=str(m.id), conversation_id=str(m.conversation_id))
                return None
            attempts = (
                await s.execute(select(SendAttempt.attempt_no).where(SendAttempt.message_id == m.id))
            ).all()
            attempt = SendAttempt(message_id=m.id, attempt_no=len(attempts) + 1)
            s.add(attempt)
            m.state = States.SENDING
            m.error = None
            await s.commit()
            hub.publish("delivery-updated", message_id=str(m.id), conversation_id=str(m.conversation_id))
            return m.id, m.conversation_id, attempt.id, target, m.body

    async def _transmit(self, message_id, conversation_id, attempt_id, target, body) -> None:
        adapter = self._adapter
        state, error, ack, deadline = States.UNCERTAIN, None, None, None
        try:
            if adapter is None:
                raise RadioError("radio disconnected before transmission")
            ts = int(time.time())
            if target[0] == "channel":
                result = await self._cmd(adapter.send_channel, target[1], body, ts, target[2])
            else:
                result = await self._cmd(adapter.send_dm, target[1], body, ts)
            if result.ok:
                self.status.sent += 1
                state = States.ACCEPTED
                if target[0] == "dm" and result.expected_ack:
                    ack = result.expected_ack.lower()
                    wait = (result.suggested_timeout_ms or 0) / 1000 * 1.5
                    deadline = utcnow() + timedelta(seconds=min(60.0, max(10.0, wait)))
                    if ack in self._early_acks:  # ACK beat the send result
                        self._early_acks.pop(ack, None)
                        state, deadline = States.ACKNOWLEDGED, None
            else:
                state, error = States.FAILED, f"Radio rejected the message ({result.error})"
        except asyncio.CancelledError:
            state, error = (
                States.UNCERTAIN,
                "Interrupted while sending; it may or may not have been transmitted.",
            )
            await self._finish_send(message_id, attempt_id, state, error, ack, deadline)
            raise
        except Exception as exc:  # noqa: BLE001
            state = States.UNCERTAIN
            error = f"Outcome unknown: {_safe_error(exc)}. It may or may not have been transmitted."
        await self._finish_send(message_id, attempt_id, state, error, ack, deadline)
        hub.publish("delivery-updated", message_id=str(message_id), conversation_id=str(conversation_id))

    async def _finish_send(self, message_id, attempt_id, state, error, ack, deadline) -> None:
        async with db.session_factory()() as s:
            await s.execute(
                update(Message)
                .where(Message.id == message_id)
                .values(state=state, error=error, expected_ack=ack, ack_deadline=deadline)
            )
            await s.execute(
                update(SendAttempt)
                .where(SendAttempt.id == attempt_id)
                .values(finished_at=utcnow(), result=state, detail=error)
            )
            await s.commit()

    async def _on_ack(self, code: str) -> None:
        code = code.lower()
        async with db.session_factory()() as s:
            row = (
                await s.execute(
                    update(Message)
                    .where(
                        Message.expected_ack == code,
                        Message.state.in_([States.ACCEPTED, States.NO_ACK]),
                    )
                    .values(state=States.ACKNOWLEDGED, ack_deadline=None)
                    .returning(Message.id, Message.conversation_id)
                )
            ).first()
            await s.commit()
        if row:
            hub.publish("delivery-updated", message_id=str(row[0]), conversation_id=str(row[1]))
        else:
            self._early_acks[code] = time.time()

    # ---- health / gaps ---------------------------------------------------------------

    async def _heartbeat(self, lock_conn: AsyncConnection) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            try:
                await asyncio.wait_for(lock_conn.execute(text("SELECT 1")), 5)
                await lock_conn.commit()
            except Exception as exc:
                raise RadioError("lost database ownership connection; releasing radio") from exc

    async def _on_contacts_changed(self) -> None:
        self._contacts_dirty.set()

    async def _contact_refresher(self) -> None:
        """Re-read the contact table soon after the radio reports a change, and every 30 min."""
        loop = asyncio.get_running_loop()
        last = loop.time()
        while True:
            try:
                await asyncio.wait_for(self._contacts_dirty.wait(), CONTACT_REFRESH_SECONDS)
                await asyncio.sleep(
                    max(CONTACT_PUSH_SETTLE_SECONDS, last + CONTACT_PUSH_MIN_INTERVAL - loop.time())
                )
            except TimeoutError:
                pass
            self._contacts_dirty.clear()
            try:
                await self.refresh_contacts()
            except (RadioError, TimeoutError) as exc:
                log.info("contact refresh failed: %s", exc)
            last = loop.time()

    async def _open_gap(self, reason: str) -> None:
        try:
            async with db.session_factory()() as s:
                open_gap = (
                    await s.execute(select(CollectionGap).where(CollectionGap.ended_at.is_(None)).limit(1))
                ).scalar_one_or_none()
                if open_gap is None:
                    s.add(
                        CollectionGap(
                            radio_id=self._radio.id if self._radio else None,
                            reason=(reason or "unknown")[:128],
                        )
                    )
                    await s.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning("could not record collection gap: %s", _safe_error(exc))

    async def _close_gaps(self) -> None:
        async with db.session_factory()() as s:
            await s.execute(
                update(CollectionGap).where(CollectionGap.ended_at.is_(None)).values(ended_at=utcnow())
            )
            await s.commit()

    # ---- simulation ------------------------------------------------------------------

    def simulate_incoming(self) -> bool:
        if isinstance(self._adapter, SimulatedRadio):
            self._adapter.inject(self._adapter.random_message())
            return True
        return False


def _safe_error(exc: BaseException) -> str:
    msg = str(exc) or type(exc).__name__
    return msg[:200]


supervisor = RadioSupervisor()
