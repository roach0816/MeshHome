"""Deterministic-ish simulated radio for development and for running before hardware exists.

Everything it produces is stored with ``is_simulated = true`` and labelled in the UI.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import time
from collections import deque
from typing import Any

from app.radio import identity, packets, sim_repeater
from app.radio.base import (
    CONTACT_RECORD_FIELDS,
    DeviceSnapshot,
    IncomingMessage,
    NotSupported,
    RadioAdapter,
    RadioChannel,
    RadioContact,
    RadioError,
    RemoteTicket,
    SendResult,
    channel_key_kind,
    hashtag_key,
)

log = logging.getLogger(__name__)


def _key(seed: str) -> str:
    return hashlib.sha256(f"meshcore-home-sim:{seed}".encode()).hexdigest()


# The simulated node's identity: a real key pair, so radio backups can export and import it.
SIM_PRIVATE_KEY = identity.expand_seed(hashlib.sha256(b"meshcore-home-sim:home-identity").digest())
SIM_SELF_KEY = identity.public_key(SIM_PRIVATE_KEY)
# Fictional positions around a public landmark (Boulder, Colorado), not any real installation.
SIM_HOME_POSITION = (40.0150, -105.2705)
SIM_CONTACTS = [
    RadioContact(public_key=_key("tracker"), name="Tracker (sim)", kind=1, lat=40.0274, lon=-105.2519),
    RadioContact(public_key=_key("neighbor"), name="Neighbor (sim)", kind=1, lat=40.0108, lon=-105.2790),
    RadioContact(public_key=_key("roof"), name="Roof Repeater (sim)", kind=2, lat=40.0163, lon=-105.2662),
    RadioContact(
        public_key=_key("hilltop"), name="Hilltop Repeater (sim)", kind=2, lat=39.9990, lon=-105.2930
    ),
    RadioContact(public_key=_key("room"), name="Club Room (sim)", kind=3, lat=40.0201, lon=-105.2829),
    RadioContact(public_key=_key("wx"), name="Weather Station (sim)", kind=4, lat=40.0060, lon=-105.2600),
    # Shares no position, like many real nodes.
    RadioContact(public_key=_key("hiker"), name="Hiker (sim, no location)", kind=1),
]
SIM_CHANNELS = [
    RadioChannel(slot=0, name="Public", secret=b"sim-public-key!!"),
    RadioChannel(slot=1, name="#home-sim", secret=b"sim-home-secret!"),
]
CHANNEL_SENDERS = ["Tracker (sim)", "Neighbor (sim)", "Hilltop", "Kayak 🚣", "BaseCamp"]
PHRASES = [
    "Testing, anyone copy?",
    "Heading out for a walk, back in an hour.",
    "Signal looks good from the ridge today.",
    "Can you hear me through the roof repeater?",
    "Weather turning, bring a jacket 🌧️",
    "All good here.",
    "Ping 👋",
    "Line one\nLine two — newlines survive the trip.",
    "<b>not bold</b> — remote text is always rendered as plain text",
]


SIM_MAX_CHANNELS = 8


def _initial_state() -> dict:
    return {
        "private_key": SIM_PRIVATE_KEY,
        "public_key": SIM_SELF_KEY,
        "key_export": True,  # tests turn it off, like firmware built without key export
        "name": "Home (simulated)",
        "lat": SIM_HOME_POSITION[0],
        "lon": SIM_HOME_POSITION[1],
        "share_location": True,
        "radio": {"freq_mhz": 910.525, "bw_khz": 62.5, "sf": 7, "cr": 5, "tx_power_dbm": 22, "repeat": False},
        "auto_add_contacts": True,
        "multi_acks": 0,
        "path_hash_mode": 0,
        "default_flood_scope": "",
        "telemetry": {"base": 0, "location": 0, "environment": 0},
        "tuning": {"rx_delay": 0.0, "airtime_factor": 1.0},
        "channels": {c.slot: (c.name, c.secret) for c in SIM_CHANNELS},
        "custom_vars": {"gps": "0", "gps_interval": "900"},
        # Per-contact radio state: flags (bit 0 favourite) and routing path.
        "contact_flags": {},
        "contact_paths": {},  # public_key -> (path_len, path_hex)
        "removed_contacts": set(),
        "added_contacts": {},  # public_key -> (name, kind), from contact_add
    }


# Simulated node settings live for the process lifetime, so they survive reconnects like a real
# node's flash would (but reset when the app restarts).
_SIM_STATE: dict | None = None


def sim_state() -> dict:
    global _SIM_STATE
    if _SIM_STATE is None:
        _SIM_STATE = _initial_state()
    return _SIM_STATE


class SimulatedRadio(RadioAdapter):
    is_simulated = True

    def __init__(self, interval_seconds: int = 60, ack_probability: float = 0.8, seed: int | None = None):
        super().__init__()
        self.interval = interval_seconds
        self.ack_probability = ack_probability
        self._rng = random.Random(seed)
        self._queue: deque[IncomingMessage] = deque()
        self._task: asyncio.Task | None = None
        self._pending: set[asyncio.Task] = set()
        self._connected = False

    async def connect(self) -> None:
        await asyncio.sleep(0.2)  # pretend handshake
        self._connected = True
        if self.interval > 0:
            self._task = asyncio.create_task(self._generator(), name="sim-radio-generator")

    async def disconnect(self) -> None:
        self._connected = False
        for t in [self._task, *self._pending]:
            if t:
                t.cancel()
        self._task = None
        self._pending.clear()

    def _require(self) -> None:
        if not self._connected:
            raise RadioError("simulated radio is not connected")

    async def get_device_snapshot(self) -> DeviceSnapshot:
        self._require()
        st = sim_state()
        return DeviceSnapshot(
            public_key=st["public_key"],
            name=st["name"],
            is_simulated=True,
            model="Simulated companion",
            firmware="sim-0.1",
            radio={k: v for k, v in st["radio"].items() if k != "repeat"},
            lat=st["lat"],
            lon=st["lon"],
        )

    async def get_contacts(self) -> list[RadioContact]:
        self._require()
        now = int(time.time())
        st = sim_state()
        out = []
        for i, c in enumerate(SIM_CONTACTS):
            if c.public_key in st["removed_contacts"]:
                continue
            path_len, path_hex = st["contact_paths"].get(c.public_key, (-1, ""))
            out.append(
                RadioContact(
                    c.public_key,
                    c.name,
                    c.kind,
                    now - 600 * (i + 1) - (86400 * 3 if i >= 5 else 0),  # some heard days ago
                    lat=c.lat,
                    lon=c.lon,
                    flags=st["contact_flags"].get(c.public_key, 0),
                    path_len=path_len,
                    path_hex=path_hex,
                )
            )
        for key, (name, kind) in st["added_contacts"].items():
            if key not in st["removed_contacts"]:
                path_len, path_hex = st["contact_paths"].get(key, (-1, ""))
                flags = st["contact_flags"].get(key, 0)
                out.append(
                    RadioContact(key, name, kind, None, flags=flags, path_len=path_len, path_hex=path_hex)
                )
        return out

    def path_hash_size(self) -> int:
        return int(sim_state()["path_hash_mode"]) + 1

    async def get_channels(self) -> list[RadioChannel]:
        self._require()
        return [
            RadioChannel(slot=slot, name=name, secret=secret)
            for slot, (name, secret) in sorted(sim_state()["channels"].items())
        ]

    async def fetch_next_message(self) -> IncomingMessage | None:
        self._require()
        return self._queue.popleft() if self._queue else None

    async def send_channel(
        self, slot: int, text: str, timestamp: int, scope: str | None = None, after_text: bytes = b""
    ) -> SendResult:
        self._require()
        if slot not in sim_state()["channels"]:
            return SendResult(ok=False, error="unknown channel slot")
        sim_state()["last_channel_scope"] = scope
        sim_state()["last_channel_after_text"] = after_text
        await asyncio.sleep(0.15)
        return SendResult(ok=True)

    async def sign(self, data: bytes) -> bytes:
        self._require()
        return identity.sign(sim_state()["private_key"], data)

    async def send_dm(self, public_key: str, text: str, timestamp: int, attempt: int = 0) -> SendResult:
        self._require()
        await asyncio.sleep(0.15)
        st = sim_state()
        flood = st["contact_paths"].get(public_key, (-1, ""))[0] < 0
        st.setdefault("sent_dms", []).append(
            {
                "public_key": public_key,
                "text": text,
                "timestamp": timestamp,
                "attempt": attempt,
                "flood": flood,
            }
        )
        ack = self._rng.randbytes(4).hex()
        if self._rng.random() < self.ack_probability:
            self._spawn(self._deliver_ack(ack, self._rng.uniform(0.8, 3.0)))
        if self._rng.random() < 0.5:
            contact = next((c for c in SIM_CONTACTS if c.public_key == public_key), None)
            if contact and contact.kind == 1:
                self._spawn(self._auto_reply(contact, self._rng.uniform(3.0, 8.0)))
        return SendResult(ok=True, expected_ack=ack, suggested_timeout_ms=8000, flood=flood)

    # ---- node configuration (in memory) ---------------------------------------------

    async def read_config(self) -> dict:
        self._require()
        st = sim_state()
        return {
            "simulated": True,
            "firmware": {
                "version_code": 10,
                "version": "sim-0.1",
                "build": "simulated",
                "model": "Simulated companion",
            },
            "identity": {
                "name": st["name"],
                "lat": st["lat"],
                "lon": st["lon"],
                "share_location": st["share_location"],
            },
            "radio": {**st["radio"], "max_tx_power_dbm": 22},
            "behavior": {
                "auto_add_contacts": st["auto_add_contacts"],
                "multi_acks": st["multi_acks"],
                "path_hash_mode": st["path_hash_mode"],
                "default_flood_scope": st["default_flood_scope"],
            },
            "telemetry": dict(st["telemetry"]),
            "tuning": dict(st["tuning"]),
            "channels": [
                {
                    "slot": i,
                    "name": st["channels"].get(i, ("", bytes(16)))[0],
                    "key": channel_key_kind(*st["channels"].get(i, ("", bytes(16)))),
                }
                for i in range(SIM_MAX_CHANNELS)
            ],
            "max_channels": SIM_MAX_CHANNELS,
            "custom_vars": dict(st["custom_vars"]),
        }

    async def configure(self, op: str, params: dict) -> dict:
        self._require()
        await asyncio.sleep(0.1)
        st = sim_state()
        if op == "identity":
            st.update(
                name=params["name"],
                lat=params.get("lat"),
                lon=params.get("lon"),
                share_location=params["share_location"],
            )
        elif op == "radio":
            st["radio"].update({k: params[k] for k in ("freq_mhz", "bw_khz", "sf", "cr", "tx_power_dbm")})
            if params.get("repeat") is not None:
                st["radio"]["repeat"] = bool(params["repeat"])
        elif op == "behavior":
            st["auto_add_contacts"] = params["auto_add_contacts"]
            st["multi_acks"] = params["multi_acks"]
            for k in ("path_hash_mode", "default_flood_scope"):
                if params.get(k) is not None:
                    st[k] = params[k]
        elif op == "telemetry":
            st["telemetry"] = {k: params[k] for k in ("base", "location", "environment")}
        elif op == "tuning":
            st["tuning"] = {k: params[k] for k in ("rx_delay", "airtime_factor")}
        elif op == "channel":
            name = params["name"]
            secret = (
                hashtag_key(name)
                if name.startswith("#") or params.get("secret") is None
                else params["secret"]
            )
            st["channels"][params["slot"]] = (name, secret)
        elif op == "channel_clear":
            st["channels"].pop(params["slot"], None)
        elif op == "custom_var":
            st["custom_vars"][params["key"]] = params["value"]
        elif op in ("advert", "sync_clock"):
            pass
        elif op == "contact_add":
            key = params["public_key"]
            st["removed_contacts"].discard(key)
            st["added_contacts"][key] = (params["name"], params["kind"])
        elif op.startswith("contact_"):
            key = params["public_key"]
            known = any(c.public_key == key for c in SIM_CONTACTS) or key in st["added_contacts"]
            if key in st["removed_contacts"] or not known:
                raise RadioError("this contact is no longer on the radio")
            if op == "contact_favorite":
                flags = st["contact_flags"].get(key, 0)
                st["contact_flags"][key] = (flags | 1) if params["favorite"] else (flags & ~1)
            elif op == "contact_reset_path":
                st["contact_paths"].pop(key, None)
            elif op == "contact_set_path":
                hex_ = params["path_hex"]
                st["contact_paths"][key] = (len(hex_) // (2 * self.path_hash_size()), hex_)
            elif op == "contact_remove":
                st["removed_contacts"].add(key)
            elif op == "contact_export":
                return {"uri": "meshcore://" + hashlib.sha256(f"sim-card:{key}".encode()).hexdigest() * 3}
            elif op != "contact_share":
                raise NotSupported(f"unknown operation {op!r}")
        elif op == "reboot":
            # Behave like the real thing: the connection drops and the supervisor reconnects.
            self._spawn(self._simulate_reboot())
        else:
            raise NotSupported(f"unknown operation {op!r}")
        return {}

    # ---- radio backups -----------------------------------------------------------------

    async def export_private_key(self) -> bytes:
        self._require()
        if not sim_state()["key_export"]:
            raise NotSupported("exporting the identity key is turned off in this radio's firmware")
        return sim_state()["private_key"]

    async def import_private_key(self, private_key: bytes) -> None:
        self._require()
        st = sim_state()
        st["public_key"] = identity.public_key(private_key)
        st["private_key"] = bytes(private_key)

    async def contact_records(self) -> list[dict]:
        out = []
        for c in await self.get_contacts():
            record = {
                "public_key": c.public_key,
                "type": c.kind,
                "flags": c.flags,
                "out_path_len": c.path_len,
                "out_path": c.path_hex,
                "out_path_hash_mode": -1 if c.path_len < 0 else self.path_hash_size() - 1,
                "adv_name": c.name,
                "last_advert": c.last_advert or 0,
                "adv_lat": c.lat or 0.0,
                "adv_lon": c.lon or 0.0,
            }
            out.append({k: record[k] for k in CONTACT_RECORD_FIELDS})
        return out

    async def add_contact_record(self, record: dict) -> None:
        self._require()
        st = sim_state()
        key = record["public_key"]
        st["removed_contacts"].discard(key)
        if not any(c.public_key == key for c in SIM_CONTACTS):
            st["added_contacts"][key] = (record["adv_name"], record["type"])
        st["contact_flags"][key] = int(record.get("flags") or 0)
        path_len = record.get("out_path_len")
        if isinstance(path_len, int) and path_len >= 0:
            st["contact_paths"][key] = (record["out_path_len"], record["out_path"])

    async def _simulate_reboot(self) -> None:
        await asyncio.sleep(0.3)
        self._connected = False
        if self.on_disconnect:
            await self.on_disconnect("radio rebooted")

    # ---- remote administration (simulated repeaters / room servers) ----------------------

    def _sim_node(self, public_key: str) -> sim_repeater.SimRepeater:
        c = next((c for c in SIM_CONTACTS if c.public_key == public_key and c.kind in (2, 3)), None)
        if c is None:
            raise RadioError("no simulated repeater with that key")
        return sim_repeater.repeater(c.public_key, c.name, c.lat, c.lon)

    async def trace_send(self, path: bytes, flags: int) -> RemoteTicket:
        self._require()
        size = 1 << (flags & 3)
        hops = [path[i : i + size] for i in range(0, len(path), size)]
        repeaters = [c.public_key for c in SIM_CONTACTS if c.kind == 2]
        # The simulated repeaters relay a trace; any other hop means it never comes back.
        ok = bool(hops) and all(any(k.startswith(h.hex()) for k in repeaters) for h in hops)
        return RemoteTicket(kind="trace", public_key="", timeout=5.0, handle=(hops, ok))

    async def trace_wait(self, ticket: RemoteTicket, timeout: float) -> dict[str, Any]:
        hops, ok = ticket.handle
        await asyncio.sleep(min(timeout, 0.25 * len(hops) + 0.2))
        if not ok:
            raise TimeoutError
        snr = lambda: round(self._rng.uniform(-6, 11) * 4) / 4  # noqa: E731 - quarter-dB, like the radio
        return {"path": [{"hash": h.hex(), "snr": snr()} for h in hops] + [{"snr": snr()}]}

    async def remote_send(self, public_key: str, kind: str, arg: str | None = None) -> RemoteTicket:
        self._require()
        node = self._sim_node(public_key)
        await asyncio.sleep(0.05)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        delay = self._rng.uniform(0.6, 1.8)
        me = SIM_SELF_KEY
        if kind == "logout":
            node.sessions.pop(me, None)
            return RemoteTicket(kind=kind, public_key=public_key, timeout=0)
        if kind == "cli":
            # Like the firmware: only an admin gets replies; the reply arrives as a CLI message.
            if node.is_admin(me):
                reply = node.cli(arg or "")
                if reply is not None:
                    self._spawn(self._cli_reply(public_key, reply, delay))
            return RemoteTicket(kind=kind, public_key=public_key, tag=self._rng.randbytes(4).hex(), timeout=6)
        if kind == "login":
            result = node.login(me, arg or "")
        elif kind in ("owner", "regions"):
            result = node.owner() if kind == "owner" else node.region_names()
        elif not node.logged_in(me):
            result = None  # the firmware ignores requests from someone not logged in
        elif kind == "status":
            result = node.status()
        elif kind == "telemetry":
            result = node.telemetry()
        elif kind == "acl":
            result = node.acl_list() if node.is_admin(me) else None
        elif kind == "neighbours":
            result = node.neighbours([c.public_key for c in SIM_CONTACTS])
        else:
            raise NotSupported(f"unknown remote request {kind!r}")
        if result is not None:
            self._spawn(self._resolve_later(fut, result, delay))
        return RemoteTicket(
            kind=kind, public_key=public_key, tag=self._rng.randbytes(4).hex(), timeout=6, handle=fut
        )

    async def remote_wait(self, ticket: RemoteTicket, timeout: float):
        if ticket.handle is None:
            return True
        return await asyncio.wait_for(ticket.handle, timeout)

    async def _resolve_later(self, fut: asyncio.Future, result, delay: float) -> None:
        await asyncio.sleep(delay)
        if not fut.done():
            fut.set_result(result)

    async def _cli_reply(self, public_key: str, text: str, delay: float) -> None:
        await asyncio.sleep(delay)
        if self._connected:
            self.inject(
                IncomingMessage(
                    kind="dm",
                    text=text,
                    txt_type=1,
                    pubkey_prefix=public_key[:12],
                    sender_timestamp=int(time.time()),
                )
            )

    # ---- simulation helpers -------------------------------------------------

    def inject(self, msg: IncomingMessage, after_text: bytes = b"") -> None:
        """Receive a message. `after_text`: bytes the sender put after a channel message's text
        (such as a signature), which the radio's text frame leaves out."""
        if self.on_rx_packet and msg.txt_type == 0 and (msg.kind == "channel" or msg.pubkey_prefix):
            # Like a real radio: the packet is logged as heard first, then the message is handed over.
            self._spawn(self._heard_then_queue(msg, after_text))
            return
        self._queue.append(msg)
        if self.on_messages_waiting:
            self._spawn(self.on_messages_waiting())

    async def _heard_then_queue(self, msg: IncomingMessage, after_text: bytes = b"") -> None:
        roof, hill = bytes.fromhex(_key("roof")[:2]), bytes.fromhex(_key("hilltop")[:2])
        if msg.kind == "channel":
            ch = sim_state()["channels"].get(int(msg.channel_slot or 0))
            ptype = packets.TYPE_GRP_TXT
            if ch and any(ch[1]):
                # The packet a real sender would send: [timestamp][flags]["name: text"], encrypted.
                sent = f"{msg.sender_label}: {msg.text}" if msg.sender_label else msg.text
                ts = (msg.sender_timestamp or int(time.time())).to_bytes(4, "little")
                plain = ts + b"\0" + sent.encode() + (b"\0" + after_text if after_text else b"")
                payload = packets.encrypt_group(ch[1], plain)
            else:
                payload = bytes([packets.channel_hash(ch[1]) if ch else 0]) + self._rng.randbytes(18)
        else:
            ptype = packets.TYPE_TXT_MSG
            payload = bytes(
                [int(SIM_SELF_KEY[:2], 16), int(msg.pubkey_prefix[:2], 16)]
            ) + self._rng.randbytes(18)
        routes = [[roof], [hill, roof], [hill]]
        self._rng.shuffle(routes)
        snr = lambda: round(self._rng.uniform(-6, 11) * 4) / 4  # noqa: E731 - quarter-dB steps, like the radio
        await self.on_rx_packet(packets.build(ptype, payload, routes[0]), snr(), self._rng.randint(-115, -70))
        self._queue.append(msg)
        if self.on_messages_waiting:
            await self.on_messages_waiting()
        if self._rng.random() < 0.6:  # often heard again through another repeater
            await asyncio.sleep(self._rng.uniform(0.3, 1.5))
            if self._connected and self.on_rx_packet:
                await self.on_rx_packet(
                    packets.build(ptype, payload, routes[1]), snr(), self._rng.randint(-115, -70)
                )

    def random_message(self) -> IncomingMessage:
        now = int(time.time())
        if self._rng.random() < 0.6:
            ch = self._rng.choice(SIM_CHANNELS)
            sender = self._rng.choice(CHANNEL_SENDERS)
            return IncomingMessage(
                kind="channel",
                text=self._rng.choice(PHRASES),
                channel_slot=ch.slot,
                sender_label=sender,
                sender_timestamp=now - self._rng.randint(0, 5),
                meta={"snr": round(self._rng.uniform(-8, 12), 1), "path_len": self._rng.randint(0, 3)},
            )
        contact = self._rng.choice([c for c in SIM_CONTACTS if c.kind == 1])
        return IncomingMessage(
            kind="dm",
            text=self._rng.choice(PHRASES),
            pubkey_prefix=contact.public_key[:12],
            sender_timestamp=now - self._rng.randint(0, 5),
            meta={"snr": round(self._rng.uniform(-8, 12), 1)},
        )

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _deliver_ack(self, code: str, delay: float) -> None:
        await asyncio.sleep(delay)
        if self._connected and self.on_ack:
            await self.on_ack(code)

    async def _auto_reply(self, contact: RadioContact, delay: float) -> None:
        await asyncio.sleep(delay)
        if self._connected:
            self.inject(
                IncomingMessage(
                    kind="dm",
                    text=self._rng.choice(["Got it 👍", "Copy that.", "Thanks!", "Roger, talk soon."]),
                    pubkey_prefix=contact.public_key[:12],
                    sender_timestamp=int(time.time()),
                )
            )

    async def _generator(self) -> None:
        try:
            while self._connected:
                await asyncio.sleep(self._rng.uniform(0.5, 1.5) * self.interval)
                self.inject(self.random_message())
        except asyncio.CancelledError:
            pass
