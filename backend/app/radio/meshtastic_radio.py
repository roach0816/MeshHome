"""MeshHome's radio as a Meshtastic node: the adapter (radio/base.py) over a Meshtastic client
API connection (meshtastic/client.py), so messages, contacts, channels and tracking work the same
way as with MeshCore.

Mapping:
- **Contacts** are the nodes in the node's list (all it has heard), keyed by `key_for(node number)`:
  Meshtastic identifies nodes by number, and keys can change. The node's own PKI key is kept in the
  contact's meta.
- **Channels** are the node's primary and secondary channels (slots 0–7).
- **Direct messages** are sent with want_ack; the recipient's acknowledgement (a routing packet
  answering the packet id) is the delivery ACK. The firmware retries on its own, so the app sends
  each message once (`app_retries`); a routing error after its retries is reported as not delivered.
- **Everything else** (positions, telemetry, node info) is passed to services/meshtastic.py for
  Tracking and the node list.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from app.meshtastic import proto as P
from app.meshtastic.client import ROUTING_ERRORS, Connection, Link, MeshtasticError
from app.radio.base import (
    DeviceSnapshot,
    IncomingMessage,
    NotSupported,
    RadioAdapter,
    RadioChannel,
    RadioContact,
    RadioError,
    SendResult,
)

log = logging.getLogger(__name__)

# Meshtastic's apps allow 200 bytes of text (the packet has room for 233 with its overhead).
TEXT_MAX_BYTES = 200
# How long to wait for a direct message's acknowledgement, which comes after the firmware's own
# retries (up to three, longer on slow presets). Late ones still count.
ACK_WAIT_MS = 45_000
CHANNEL_NAME_MAX = 11
SUFFIX = "0" * 56
# The recipient couldn't open a direct message because the two radios hadn't swapped keys yet
# (Meshtastic 2.5+ only accepts direct messages encrypted to the recipient's key).
NO_KEY_ERRORS = {6, 34, 35}
# Errors after which the message may still have arrived (only its acknowledgement didn't).
MAYBE_DELIVERED = {3, 5, 8}


def key_for(num: int) -> str:
    """The contact / radio key MeshHome uses for a Meshtastic node: its node number in hex,
    padded to the 64 characters of a MeshCore key."""
    return f"{num & 0xFFFFFFFF:08x}{SUFFIX}"


def num_from_key(key: str) -> int:
    if len(key) != 64 or not key.endswith(SUFFIX):
        raise RadioError("not a Meshtastic node")
    return int(key[:8], 16)


def is_meshtastic_key(key: str | None) -> bool:
    return bool(key) and len(key) == 64 and key.endswith(SUFFIX)  # type: ignore[union-attr]


def preset_label(lora: dict[str, Any]) -> str:
    """How Meshtastic names the primary channel when it has no name: after the modem preset."""
    if not lora.get("use_preset", True) and "use_preset" in lora:
        return "Custom"
    name = P.PRESETS.get(int(lora.get("modem_preset") or 0), "LONG_FAST")
    return "".join(w.capitalize() for w in name.split("_"))


# Meshtastic's HardwareModel numbers for the devices MeshHome is used with; others by number.
HARDWARE = {9: "RAK4631", 37: "Linux (meshtasticd)", 43: "Heltec V3"}


def hardware_name(hw: object) -> str:
    try:
        n = int(hw)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return "Meshtastic node"
    return f"Meshtastic · {HARDWARE.get(n, f'hardware {n}')}"


def contact_kind(role: str | None) -> int:
    """MeshHome's contact kinds (MeshCore's advert types): 2 relays, 4 sensors, 1 everything else."""
    if role in P.RELAY_ROLES and role != "CLIENT_BASE":
        return 2
    if role == "SENSOR":
        return 4
    return 1


def path_entry(pkt: dict[str, Any]) -> dict[str, Any] | None:
    """How a packet reached the node, in the shape of a message path (services/msg_paths.py)."""
    start, left = pkt.get("hop_start"), pkt.get("hop_limit")
    if not start:
        return None
    hops = max(0, int(start) - int(left or 0))
    relay = pkt.get("relay_node")
    frm = int(pkt.get("from") or 0)
    last = f"{int(relay):02x}" if relay and int(relay) != (frm & 0xFF) else "?"
    return {
        "hops": [] if hops == 0 else ["?"] * (hops - 1) + [last],
        "hash_size": 1,
        "route": "flood",
        "snr": round(float(pkt["rx_snr"]), 2) if pkt.get("rx_snr") else None,
        "rssi": float(pkt["rx_rssi"]) if pkt.get("rx_rssi") else None,
    }


class MeshtasticRadio(RadioAdapter):
    tech = "meshtastic"
    app_retries = False

    def __init__(self, link_factory: Callable[[], Link], *, simulated: bool = False) -> None:
        super().__init__()
        self.is_simulated = simulated
        self._link_factory = link_factory
        self.conn: Connection | None = None
        self.my_num = 0
        self.nodes: dict[int, dict[str, Any]] = {}
        self.channels: dict[int, dict[str, Any]] = {}
        self.lora: dict[str, Any] = {}
        self.device: dict[str, Any] = {}
        self.metadata: dict[str, Any] = {}
        self._inbox: deque[IncomingMessage] = deque()
        self._seen: deque[tuple[int, int]] = deque(maxlen=500)
        self._dm_pending: dict[int, tuple[int, float]] = {}  # packet id -> (to, when sent)
        self._runner: asyncio.Task | None = None
        self._closing = False
        # services/meshtastic.py: positions, telemetry and node info for Tracking and the node list.
        self.on_mesh_packet: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        # A direct message wasn't delivered: (expected ack code, why, whether it might have been
        # received all the same: no acknowledgement isn't proof it wasn't).
        self.on_send_failed: Callable[[str, str, bool], Awaitable[None]] | None = None

    # ---- connection -----------------------------------------------------------------------

    async def connect(self) -> None:
        conn = Connection(self._link_factory())
        try:
            snap = await conn.start()
        except (ConnectionError, OSError) as exc:
            await conn.close()
            raise RadioError(str(exc) or "couldn't connect to the Meshtastic node") from None
        except (MeshtasticError, P.ProtoError) as exc:
            await conn.close()
            raise RadioError(str(exc)) from None
        self.conn = conn
        self.my_num = snap.my_num
        self.nodes = {n: dict(v) for n, v in snap.nodes.items()}
        self.channels = dict(snap.channels)
        self.lora, self.device, self.metadata = snap.lora, snap.device, snap.metadata
        self._runner = asyncio.create_task(self._run(conn), name="meshtastic-radio")

    async def _run(self, conn: Connection) -> None:
        reason = "the node closed the connection"
        try:
            await conn.run(self._on_message)
        except asyncio.CancelledError:
            raise
        except (ConnectionError, MeshtasticError, P.ProtoError, OSError) as exc:
            reason = str(exc) or type(exc).__name__
        except Exception as exc:  # noqa: BLE001 - report it as a lost connection
            log.warning("meshtastic radio: %s", exc, exc_info=True)
            reason = str(exc)
        if not self._closing and self.on_disconnect is not None:
            await self.on_disconnect(reason)

    async def disconnect(self) -> None:
        self._closing = True
        if self._runner is not None:
            self._runner.cancel()
            with contextlib.suppress(BaseException):
                await self._runner
        if self.conn is not None:
            await self.conn.close()
        self.conn = None

    def _require(self) -> Connection:
        if self.conn is None:
            raise RadioError("radio is not connected")
        return self.conn

    # ---- what the node tells us ---------------------------------------------------------------

    async def _on_message(self, msg: dict[str, Any]) -> None:
        if "packet" in msg:
            await self._on_packet(msg["packet"])
        elif "node_info" in msg:
            ni = msg["node_info"]
            if ni.get("num"):
                self.nodes[int(ni["num"])] = {**self.nodes.get(int(ni["num"]), {}), **ni}
                await self._contacts_changed()
        elif "channel" in msg:
            self.channels[int(msg["channel"].get("index") or 0)] = msg["channel"]
        elif "config" in msg:
            if "lora" in msg["config"]:
                self.lora = msg["config"]["lora"]
            elif "device" in msg["config"]:
                self.device = msg["config"]["device"]

    async def _contacts_changed(self) -> None:
        if self.on_contacts_changed is not None:
            await self.on_contacts_changed()

    async def _on_packet(self, pkt: dict[str, Any]) -> None:
        dec = pkt.get("decoded")
        frm = int(pkt.get("from") or 0)
        if not dec or not frm:
            return
        port = dec.get("portnum")
        if port == P.ROUTING_APP:
            await self._on_routing(frm, dec)
            return
        if frm == self.my_num:
            return
        self.last_heard_at = time.time()
        pid = int(pkt.get("id") or 0)
        if pid:
            if (frm, pid) in self._seen:
                return  # another copy of a packet already heard (it took another path)
            self._seen.append((frm, pid))
        node = self.nodes.setdefault(frm, {"num": frm})
        node["last_heard"] = int(pkt.get("rx_time") or time.time())
        if pkt.get("rx_snr") is not None:
            node["snr"] = float(pkt["rx_snr"])
        entry = path_entry(pkt)
        if entry is not None:
            node["hops_away"] = len(entry["hops"])
        ch = pkt.get("channel")
        if ch is not None and 0 <= int(ch) <= 7 and not pkt.get("pki_encrypted"):
            node["channel"] = int(ch)  # the channel it's heard on (a key-encrypted DM says nothing)
        if port == P.TEXT_MESSAGE_APP:
            self._on_text(pkt, dec, frm, entry)
            if self.on_messages_waiting is not None:
                await self.on_messages_waiting()
            return
        try:
            if port == P.NODEINFO_APP:
                node["user"] = {**node.get("user", {}), **P.decode(dec.get("payload") or b"", P.USER)}
                await self._contacts_changed()
            elif port == P.POSITION_APP:
                pos = P.decode(dec.get("payload") or b"", P.POSITION)
                if P.position_fix(pos):
                    node["position"] = pos
                    await self._contacts_changed()
            elif port == P.TELEMETRY_APP:
                tel = P.decode(dec.get("payload") or b"", P.TELEMETRY)
                if tel.get("device_metrics"):
                    node["device_metrics"] = tel["device_metrics"]
        except P.ProtoError:
            return
        if self.on_mesh_packet is not None and port in (P.POSITION_APP, P.NODEINFO_APP, P.TELEMETRY_APP):
            try:
                await self.on_mesh_packet(pkt)
            except Exception as exc:  # noqa: BLE001 - tracking must never disturb messaging
                log.warning("meshtastic: could not record a packet for tracking: %s", exc)

    def _on_text(self, pkt: dict[str, Any], dec: dict[str, Any], frm: int, entry: dict | None) -> None:
        try:
            text = (dec.get("payload") or b"").decode("utf-8")
        except UnicodeDecodeError:
            text = (dec.get("payload") or b"").decode("utf-8", "replace")
        if not text:
            return
        to = int(pkt.get("to") or 0)
        meta: dict[str, Any] = {"packet_id": int(pkt.get("id") or 0)}
        if entry is not None:
            meta["paths"] = [entry]
        if dec.get("reply_id"):
            meta["reply_to_packet"] = int(dec["reply_id"])
        if dec.get("emoji"):
            meta["reaction"] = True
        ts = int(pkt.get("rx_time") or time.time())
        if to == self.my_num:
            msg = IncomingMessage(
                kind="dm", text=text, sender_timestamp=ts, pubkey_prefix=key_for(frm), meta=meta
            )
        elif to == P.BROADCAST:
            user = self.nodes.get(frm, {}).get("user") or {}
            msg = IncomingMessage(
                kind="channel",
                text=text,
                sender_timestamp=ts,
                channel_slot=int(pkt.get("channel") or 0),
                sender_label=user.get("long_name") or user.get("short_name") or P.node_id(frm),
                pubkey_prefix=key_for(frm),
                meta=meta,
            )
        else:
            return  # a message for someone else (not normally passed on)
        self._inbox.append(msg)

    async def _on_routing(self, frm: int, dec: dict[str, Any]) -> None:
        rid = int(dec.get("request_id") or 0)
        pending = self._dm_pending.get(rid)
        if pending is None:
            return
        try:
            err = int(P.decode(dec.get("payload") or b"", P.ROUTING).get("error_reason") or 0)
        except P.ProtoError:
            return
        to, _ = pending
        code = f"{rid:08x}"
        if err == 0:
            if frm != to:
                return  # a relay passing it on, not the recipient's acknowledgement
            self._dm_pending.pop(rid, None)
            if self.on_ack is not None:
                await self.on_ack(code)
        else:
            self._dm_pending.pop(rid, None)
            reason = ROUTING_ERRORS.get(err, f"error {err}")
            if err in NO_KEY_ERRORS and frm == to:
                # Swap keys now (our node info, asking for theirs), so sending again works.
                reason = "the two radios hadn't swapped keys yet (they're swapping them now): send it again"
                with contextlib.suppress(Exception):
                    await self._introduce(to)
            if self.on_send_failed is not None:
                await self.on_send_failed(code, reason, err in MAYBE_DELIVERED)
        cutoff = time.monotonic() - 600
        self._dm_pending = {k: v for k, v in self._dm_pending.items() if v[1] > cutoff}

    # ---- the adapter --------------------------------------------------------------------------

    def _me(self) -> dict[str, Any]:
        return self.nodes.get(self.my_num, {})

    async def _introduce(self, num: int) -> None:
        user = {k: v for k, v in (self._me().get("user") or {}).items() if k != "_raw"}
        if user:
            await self._require().send(num, P.NODEINFO_APP, P.encode(user, P.USER), want_response=True)

    def _dm_channel(self, num: int) -> int:
        """The channel to send a direct message on: the one the node is heard on, if this radio has
        it (with keys swapped, the firmware encrypts it to the recipient's key either way)."""
        ch = int(self.nodes.get(num, {}).get("channel") or 0)
        ok = (self.channels.get(ch) or {}).get("role") in (P.PRIMARY, P.SECONDARY)
        return ch if ok else 0

    async def get_device_snapshot(self) -> DeviceSnapshot:
        me = self._me()
        user = me.get("user") or {}
        fix = P.position_fix(me.get("position") or {}) or {}
        return DeviceSnapshot(
            public_key=key_for(self.my_num),
            name=user.get("long_name") or P.node_id(self.my_num),
            is_simulated=self.is_simulated,
            model=hardware_name(self.metadata.get("hw_model", user.get("hw_model"))),
            firmware=self.metadata.get("firmware_version"),
            radio={
                "region": P.REGIONS.get(int(self.lora.get("region") or 0), "UNSET"),
                "preset": P.PRESETS.get(int(self.lora.get("modem_preset") or 0))
                if self.lora.get("use_preset", True)
                else "CUSTOM",
                "hop_limit": self.lora.get("hop_limit"),
                "tx_power_dbm": self.lora.get("tx_power"),
            },
            raw_device_info={
                "tech": "meshtastic",
                "node_id": P.node_id(self.my_num),
                "short_name": user.get("short_name"),
                "role": P.ROLES.get(int(self.device.get("role") or 0)),
            },
            lat=fix.get("lat"),
            lon=fix.get("lon"),
            tech="meshtastic",
        )

    async def get_contacts(self) -> list[RadioContact]:
        out = []
        for num, ni in self.nodes.items():
            if num == self.my_num or not num:
                continue
            user = ni.get("user") or {}
            role = P.ROLES.get(int(user.get("role") or 0)) if user else None
            fix = P.position_fix(ni.get("position") or {}) or {}
            dm = ni.get("device_metrics") or {}
            pk = user.get("public_key")
            out.append(
                RadioContact(
                    public_key=key_for(num),
                    name=(user.get("long_name") or user.get("short_name") or P.node_id(num))[:64],
                    kind=contact_kind(role),
                    last_advert=int(ni["last_heard"]) if ni.get("last_heard") else None,
                    lat=fix.get("lat"),
                    lon=fix.get("lon"),
                    flags=1 if ni.get("is_favorite") else 0,
                    meta={
                        "tech": "meshtastic",
                        "node_num": num,
                        "node_id": P.node_id(num),
                        "short_name": user.get("short_name"),
                        "hw_model": user.get("hw_model"),
                        "role": role,
                        "battery_level": dm.get("battery_level"),
                        "voltage": dm.get("voltage"),
                        "snr": ni.get("snr"),
                        "hops_away": ni.get("hops_away"),
                        "via_mqtt": bool(ni.get("via_mqtt")),
                        "pki_key": pk.hex() if isinstance(pk, bytes) and len(pk) == 32 else None,
                        "unmessagable": bool(user.get("is_unmessagable")),
                    },
                )
            )
        return out

    def channel_name(self, index: int) -> str:
        ch = self.channels.get(index) or {}
        name = (ch.get("settings") or {}).get("name") or ""
        if not name and index == 0:
            return preset_label(self.lora)
        return name or f"Channel {index}"

    async def get_channels(self) -> list[RadioChannel]:
        out = []
        for idx, ch in sorted(self.channels.items()):
            if ch.get("role") not in (P.PRIMARY, P.SECONDARY):
                continue
            psk = (ch.get("settings") or {}).get("psk") or b""
            out.append(RadioChannel(slot=idx, name=self.channel_name(idx), secret=bytes(psk) or b"\x00"))
        return out

    async def fetch_next_message(self) -> IncomingMessage | None:
        return self._inbox.popleft() if self._inbox else None

    async def send_channel(
        self, slot: int, text: str, timestamp: int, scope: str | None = None, after_text: bytes = b""
    ) -> SendResult:
        data = text.encode("utf-8")
        if len(data) > TEXT_MAX_BYTES:
            return SendResult(ok=False, error=f"longer than {TEXT_MAX_BYTES} bytes")
        if (self.channels.get(slot) or {}).get("role") not in (P.PRIMARY, P.SECONDARY):
            return SendResult(ok=False, error="that channel isn't on the radio")
        await self._require().send(P.BROADCAST, P.TEXT_MESSAGE_APP, data, channel=slot, want_ack=True)
        return SendResult(ok=True)

    async def send_dm(self, public_key: str, text: str, timestamp: int, attempt: int = 0) -> SendResult:
        num = num_from_key(public_key)
        data = text.encode("utf-8")
        if len(data) > TEXT_MAX_BYTES:
            return SendResult(ok=False, error=f"longer than {TEXT_MAX_BYTES} bytes")
        pid = await self._require().send(
            num, P.TEXT_MESSAGE_APP, data, channel=self._dm_channel(num), want_ack=True
        )
        self._dm_pending[pid] = (num, time.monotonic())
        return SendResult(ok=True, expected_ack=f"{pid:08x}", suggested_timeout_ms=ACK_WAIT_MS)

    # ---- settings -----------------------------------------------------------------------------

    def settings(self) -> dict[str, Any]:
        """The node's settings, for MeshHome's node settings page (no channel keys)."""
        me = self._me()
        user = me.get("user") or {}
        fix = P.position_fix(me.get("position") or {}) or {}
        channels = []
        for idx in range(8):
            ch = self.channels.get(idx) or {}
            if ch.get("role") not in (P.PRIMARY, P.SECONDARY):
                continue
            s = ch.get("settings") or {}
            psk = s.get("psk") or b""
            channels.append(
                {
                    "index": idx,
                    "name": s.get("name") or "",
                    "display_name": self.channel_name(idx),
                    "primary": ch.get("role") == P.PRIMARY,
                    "key": "none" if not psk else "default" if len(psk) == 1 else "private",
                    "position_precision": int(
                        (s.get("module_settings") or {}).get("position_precision") or 0
                    ),
                }
            )
        return {
            "node": {
                "num": self.my_num,
                "node_id": P.node_id(self.my_num),
                "long_name": user.get("long_name") or "",
                "short_name": user.get("short_name") or "",
                "firmware": self.metadata.get("firmware_version"),
                "role": P.ROLES.get(int(self.device.get("role") or 0), "CLIENT"),
                "lat": fix.get("lat"),
                "lon": fix.get("lon"),
            },
            "lora": {
                "region": P.REGIONS.get(int(self.lora.get("region") or 0), "UNSET"),
                "preset": P.PRESETS.get(int(self.lora.get("modem_preset") or 0), "LONG_FAST")
                if self.lora.get("use_preset", True)
                else "CUSTOM",
                "hop_limit": int(self.lora.get("hop_limit") or 3),
                "tx_power_dbm": int(self.lora.get("tx_power") or 0),
                "tx_enabled": bool(self.lora.get("tx_enabled")),
            },
            "channels": channels,
        }

    async def read_config(self) -> dict[str, Any]:
        s = self.settings()
        node = s["node"]
        # "identity" is what services/sensors.py reads to keep the location current.
        return {
            "identity": {
                "name": node["long_name"],
                "lat": node["lat"],
                "lon": node["lon"],
                "share_location": True,
            },
            "meshtastic": s,
        }

    def channel_url(self, indices: list[int] | None = None) -> str | None:
        """Channels and LoRa settings as a Meshtastic share link (shown as its QR code)."""
        if not self.lora:
            return None
        picked = [
            i
            for i in sorted(self.channels)
            if self.channels[i].get("role") in (P.PRIMARY, P.SECONDARY) and (indices is None or i in indices)
        ]
        if not picked:
            return None
        settings = [(self.channels[i].get("settings") or {}).get("_raw") or b"" for i in picked]
        return P.channel_url(settings, self.lora.get("_raw") or b"")

    async def _edit(self, *msgs: dict[str, Any]) -> None:
        conn = self._require()
        try:
            await conn.admin({"begin_edit_settings": True})
            for m in msgs:
                await conn.admin(m)
            await conn.admin({"commit_edit_settings": True})
        except (MeshtasticError, ConnectionError) as exc:
            raise RadioError(str(exc)) from None

    async def _admin(self, msg: dict[str, Any]) -> None:
        try:
            await self._require().admin(msg)
        except (MeshtasticError, ConnectionError) as exc:
            raise RadioError(str(exc)) from None

    async def configure(self, op: str, params: dict[str, Any]) -> dict[str, Any]:
        if op == "identity":
            return await self._identity(params)
        if op == "owner":
            return await self._owner(params.get("long_name"), params.get("short_name"))
        if op == "radio":
            return await self._lora(params)
        if op == "role":
            role = next((k for k, v in P.ROLES.items() if v == params.get("role")), None)
            if role is None:
                raise RadioError("unknown role")
            raw = P.patch(self.device.get("_raw") or b"", P.DEVICE_CONFIG, {"role": role})
            await self._edit({"set_config": {"device": raw}})
            self.device = P.decode(raw, P.DEVICE_CONFIG)
            return {}
        if op == "channel":
            return await self._channel(params)
        if op == "channel_clear":
            idx = int(params["index"])
            if idx == 0:
                raise RadioError("the primary channel can't be removed")
            ch = {"index": idx, "role": P.DISABLED, "settings": {}}
            await self._edit({"set_channel": ch})
            self.channels[idx] = P.decode(P.encode(ch, P.CHANNEL), P.CHANNEL)
            return {}
        if op == "channels_import":
            return await self._import(params)
        if op == "contact_favorite":
            num = num_from_key(params["public_key"])
            await self._admin(
                {"set_favorite_node" if params.get("favorite") else "remove_favorite_node": num}
            )
            self.nodes.setdefault(num, {"num": num})["is_favorite"] = bool(params.get("favorite"))
            return {}
        if op == "contact_remove":
            num = num_from_key(params["public_key"])
            await self._admin({"remove_by_nodenum": num})
            self.nodes.pop(num, None)
            return {}
        raise NotSupported("Meshtastic nodes don't have this setting")

    async def _identity(self, params: dict[str, Any]) -> dict[str, Any]:
        name = (params.get("name") or "").strip()
        user = self._me().get("user") or {}
        if name and name != user.get("long_name"):
            await self._owner(name, None)
        lat, lon = params.get("lat"), params.get("lon")
        me = self.nodes.setdefault(self.my_num, {"num": self.my_num})
        if lat is not None and lon is not None:
            pos = {"latitude_i": round(float(lat) * 1e7), "longitude_i": round(float(lon) * 1e7)}
            await self._admin({"set_fixed_position": pos})
            me["position"] = {**pos, "time": int(time.time())}
        elif me.get("position"):
            await self._admin({"remove_fixed_position": True})
            me.pop("position", None)
        return {}

    async def _owner(self, long_name: str | None, short_name: str | None) -> dict[str, Any]:
        user = dict(self._me().get("user") or {})
        user.pop("_raw", None)
        if long_name is not None:
            long_name = long_name.strip()
            if not long_name or len(long_name.encode()) > 24:
                raise RadioError("the name must be 1 to 24 bytes")
            user["long_name"] = long_name
        if short_name is not None:
            short_name = short_name.strip()
            if not short_name or len(short_name.encode()) > 4:
                raise RadioError("the short name must be 1 to 4 characters")
            user["short_name"] = short_name
        user.setdefault("id", P.node_id(self.my_num))
        user.pop("public_key", None)  # the node keeps its own key
        await self._admin({"set_owner": user})
        self.nodes.setdefault(self.my_num, {"num": self.my_num})["user"] = {
            **(self._me().get("user") or {}),
            **user,
        }
        return {}

    async def _lora(self, params: dict[str, Any]) -> dict[str, Any]:
        changes: dict[str, Any] = {"tx_enabled": True}
        if params.get("region") is not None:
            region = next((k for k, v in P.REGIONS.items() if v == params["region"]), None)
            if not region:
                raise RadioError("choose the region you're in")
            changes["region"] = region
        if params.get("preset") is not None:
            preset = next((k for k, v in P.PRESETS.items() if v == params["preset"]), None)
            if preset is None:
                raise RadioError("unknown modem preset")
            changes["modem_preset"], changes["use_preset"] = preset, True
        if params.get("hop_limit") is not None:
            changes["hop_limit"] = max(1, min(7, int(params["hop_limit"])))
        if params.get("tx_power_dbm") is not None:
            changes["tx_power"] = max(0, min(30, int(params["tx_power_dbm"])))
        raw = P.patch(self.lora.get("_raw") or b"", P.LORA_CONFIG, changes)
        restart = any(
            changes.get(k) is not None and changes[k] != (self.lora.get(k) or 0)
            for k in ("region", "modem_preset")
        )
        await self._edit({"set_config": {"lora": raw}})
        self.lora = P.decode(raw, P.LORA_CONFIG)
        return {"restarts": restart}

    async def _channel(self, params: dict[str, Any]) -> dict[str, Any]:
        idx = params.get("index")
        if idx is None:  # a new channel: the first free slot
            idx = next(
                (
                    i
                    for i in range(1, 8)
                    if (self.channels.get(i) or {}).get("role") not in (P.PRIMARY, P.SECONDARY)
                ),
                None,
            )
            if idx is None:
                raise RadioError("all 8 channels are in use")
        idx = int(idx)
        current = self.channels.get(idx) or {}
        settings = {k: v for k, v in (current.get("settings") or {}).items() if k != "_raw"}
        name = params.get("name")
        if name is not None:
            name = name.strip()
            if len(name.encode()) > CHANNEL_NAME_MAX:
                raise RadioError(f"a channel name is at most {CHANNEL_NAME_MAX} bytes")
            if not name and idx != 0:
                raise RadioError("give the channel a name")
            settings["name"] = name
        key = params.get("key")
        if key == "random":
            settings["psk"] = os.urandom(32)
        elif key == "default":
            settings["psk"] = P.DEFAULT_PSK
        elif key is None and not settings.get("psk"):
            settings["psk"] = os.urandom(32)
        if params.get("position_precision") is not None:
            ms = {k: v for k, v in (settings.get("module_settings") or {}).items() if k != "_raw"}
            ms["position_precision"] = max(0, min(32, int(params["position_precision"])))
            settings["module_settings"] = ms
        ch = {"index": idx, "role": P.PRIMARY if idx == 0 else P.SECONDARY, "settings": settings}
        await self._edit({"set_channel": ch})
        self.channels[idx] = P.decode(P.encode(ch, P.CHANNEL), P.CHANNEL)
        return {"index": idx}

    async def _import(self, params: dict[str, Any]) -> dict[str, Any]:
        """Channels from a Meshtastic share link: added in free slots, or replacing all of them
        (and then also taking its LoRa settings, as the Meshtastic apps do)."""
        cs: dict[str, Any] = params["channel_set"]
        incoming = [{k: v for k, v in s.items() if k != "_raw"} for s in cs.get("settings") or []]
        if not incoming:
            raise RadioError("the link has no channels")
        msgs: list[dict[str, Any]] = []
        if params.get("replace"):
            for i in range(8):
                if i < len(incoming):
                    msgs.append(
                        {
                            "set_channel": {
                                "index": i,
                                "role": P.PRIMARY if i == 0 else P.SECONDARY,
                                "settings": incoming[i],
                            }
                        }
                    )
                elif (self.channels.get(i) or {}).get("role") in (P.PRIMARY, P.SECONDARY):
                    msgs.append({"set_channel": {"index": i, "role": P.DISABLED, "settings": {}}})
            lora = cs.get("lora_config")
            restart = False
            if lora:
                raw = P.patch(lora.get("_raw") or b"", P.LORA_CONFIG, {"tx_enabled": True})
                restart = any(lora.get(k, 0) != self.lora.get(k, 0) for k in ("region", "modem_preset"))
                msgs.append({"set_config": {"lora": raw}})
        else:
            free = [
                i
                for i in range(1, 8)
                if (self.channels.get(i) or {}).get("role") not in (P.PRIMARY, P.SECONDARY)
            ]
            if len(free) < len(incoming):
                raise RadioError(f"only {len(free)} free channel slots")
            for i, s in zip(free, incoming, strict=False):
                msgs.append({"set_channel": {"index": i, "role": P.SECONDARY, "settings": s}})
            restart = False
        await self._edit(*msgs)
        for m in msgs:
            if "set_channel" in m:
                ch = m["set_channel"]
                self.channels[ch["index"]] = P.decode(P.encode(ch, P.CHANNEL), P.CHANNEL)
            elif "set_config" in m:
                self.lora = P.decode(m["set_config"]["lora"], P.LORA_CONFIG)
        return {"added": len(incoming), "restarts": restart}

    # ---- tracking -----------------------------------------------------------------------------

    async def request_position(self, num: int) -> None:
        await self._require().send(
            num,
            P.POSITION_APP,
            b"",
            channel=self._dm_channel(num),
            want_response=True,
            hop_limit=int(self.lora.get("hop_limit") or 3),
        )

    def simulate_incoming(self) -> bool:
        link = self.conn.link if self.conn else None
        inject = getattr(link, "inject_text", None)
        if inject is None:
            return False
        inject()
        return True
