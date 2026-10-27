"""MeshCore Ethernet/TCP companion adapter, built on the upstream ``meshcore`` client.

Written against the source of ``meshcore`` 2.3.14 (pinned in requirements.txt).
NOT YET VERIFIED AGAINST REAL HARDWARE: response shapes are read defensively and any
unexpected shape degrades to a RadioError with redacted detail instead of crashing.
Capture redacted real responses as test fixtures once the RAK companion is commissioned.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from typing import Any

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
    advert_position,
    channel_key_kind,
    split_channel_text,
)

log = logging.getLogger(__name__)

COMMAND_TIMEOUT = 8.0
CONNECT_TIMEOUT = 10.0
MAX_CHANNEL_SLOTS_FALLBACK = 8  # only used if the device does not report max_channels
NEIGHBOUR_PREFIX_BYTES = 4


def _payload(event: Any) -> dict[str, Any]:
    p = getattr(event, "payload", None)
    return p if isinstance(p, dict) else {}


def _describe(event: Any) -> str:
    """Error description safe for logs/UI: event type and a reason field only, never payload data."""
    if event is None:
        return "no response"
    p = _payload(event)
    reason = p.get("reason") or p.get("error") or p.get("error_code")
    etype = getattr(getattr(event, "type", None), "value", "unknown")
    return f"{etype}: {reason}" if reason else str(etype)


class MeshCoreTcpRadio(RadioAdapter):
    is_simulated = False

    def __init__(self, host: str, port: int):
        super().__init__()
        self.host = host
        self.port = port
        self._mc = None
        self._subs: list[Any] = []
        self._self_info: dict[str, Any] = {}
        self._device_info: dict[str, Any] = {}

    async def connect(self) -> None:
        from meshcore import EventType, MeshCore

        try:
            # auto_reconnect=False: our supervisor is the single reconnect mechanism.
            mc = await asyncio.wait_for(
                MeshCore.create_tcp(
                    self.host, self.port, default_timeout=COMMAND_TIMEOUT, auto_reconnect=False
                ),
                timeout=CONNECT_TIMEOUT,
            )
        except (TimeoutError, OSError, ConnectionError) as exc:
            raise RadioError(
                f"cannot reach companion at {self.host}:{self.port}: {type(exc).__name__}"
            ) from exc
        if mc is None:
            raise RadioError("companion did not answer the handshake")
        self._mc = mc
        self._self_info = dict(mc.self_info or {})

        # Subscribe to pushes before any message retrieval starts.
        async def _on_waiting(_event):
            if self.on_messages_waiting:
                await self.on_messages_waiting()

        async def _on_ack(event):
            code = _payload(event).get("code")
            if code and self.on_ack:
                await self.on_ack(code)

        async def _on_contacts_changed(_event):
            if self.on_contacts_changed:
                await self.on_contacts_changed()

        async def _on_rx_log(event):
            p = _payload(event)
            if self.on_rx_packet and isinstance(p.get("payload"), str):
                try:
                    raw = bytes.fromhex(p["payload"])
                except ValueError:
                    return
                await self.on_rx_packet(raw, p.get("snr"), p.get("rssi"))

        async def _on_disconnected(event):
            if self.on_disconnect:
                await self.on_disconnect(str(_payload(event).get("reason", "disconnected")))

        self._subs = [
            mc.subscribe(EventType.MESSAGES_WAITING, _on_waiting),
            mc.subscribe(EventType.ACK, _on_ack),
            # Adverts from contacts on the radio (including ones it just auto-added) and new paths.
            mc.subscribe(EventType.ADVERTISEMENT, _on_contacts_changed),
            mc.subscribe(EventType.PATH_UPDATE, _on_contacts_changed),
            # Raw packets heard (before repeats are dropped): the routes messages took.
            mc.subscribe(EventType.RX_LOG_DATA, _on_rx_log),
            mc.subscribe(EventType.DISCONNECTED, _on_disconnected),
        ]

        res = await mc.commands.send_device_query()
        if res is not None and not res.is_error():
            self._device_info = _payload(res)

    async def disconnect(self) -> None:
        mc, self._mc = self._mc, None
        if mc is None:
            return
        for s in self._subs:
            mc.unsubscribe(s)
        self._subs = []
        try:
            await asyncio.wait_for(mc.disconnect(), timeout=5)
        except Exception as exc:  # noqa: BLE001 - best effort close
            log.warning("error while closing companion connection: %s", type(exc).__name__)

    def _require(self):
        if self._mc is None or not self._mc.is_connected:
            raise RadioError("not connected")
        return self._mc

    async def get_device_snapshot(self) -> DeviceSnapshot:
        self._require()
        si, di = self._self_info, self._device_info
        key = si.get("public_key")
        if not isinstance(key, str) or len(key) != 64:
            raise RadioError("device did not report a full public key")
        lat, lon = advert_position(si.get("adv_lat"), si.get("adv_lon"))
        return DeviceSnapshot(
            public_key=key,
            lat=lat,
            lon=lon,
            name=str(si.get("name") or ""),
            is_simulated=False,
            model=di.get("model"),
            firmware=" ".join(str(x) for x in (di.get("ver"), di.get("fw_build")) if x) or None,
            radio={
                "freq_mhz": si.get("radio_freq"),
                "bw_khz": si.get("radio_bw"),
                "sf": si.get("radio_sf"),
                "cr": si.get("radio_cr"),
                "tx_power_dbm": si.get("tx_power"),
            },
            # Explicit allow-list: never forward unknown fields (e.g. BLE PIN) to the UI.
            raw_device_info={
                k: di[k]
                for k in ("fw ver", "max_contacts", "max_channels", "model", "ver", "fw_build")
                if k in di
            },
        )

    async def get_contacts(self) -> list[RadioContact]:
        mc = self._require()
        res = await mc.commands.get_contacts(timeout=COMMAND_TIMEOUT)
        if res is None or res.is_error():
            raise RadioError(f"contact refresh failed ({_describe(res)})")
        out = []
        for c in _payload(res).values():
            key = c.get("public_key") if isinstance(c, dict) else None
            if not isinstance(key, str) or len(key) != 64:
                continue
            lat, lon = advert_position(c.get("adv_lat"), c.get("adv_lon"))
            out.append(
                RadioContact(
                    public_key=key,
                    name=str(c.get("adv_name") or ""),
                    kind=int(c.get("type") or 0),
                    last_advert=c.get("last_advert") or None,
                    meta={"out_path_len": c.get("out_path_len")},
                    lat=lat,
                    lon=lon,
                    flags=int(c.get("flags") or 0),
                    path_len=int(c.get("out_path_len") if c.get("out_path_len") is not None else -1),
                    path_hex=str(c.get("out_path") or ""),
                )
            )
        return out

    async def get_channels(self) -> list[RadioChannel]:
        mc = self._require()
        max_channels = self._device_info.get("max_channels") or MAX_CHANNEL_SLOTS_FALLBACK
        out = []
        for idx in range(int(max_channels)):
            res = await mc.commands.get_channel(idx)
            if res is None or res.is_error():
                continue
            p = _payload(res)
            name = str(p.get("channel_name") or "").strip()
            secret = p.get("channel_secret") or b""
            if not name and not any(secret):
                continue  # empty slot
            out.append(RadioChannel(slot=idx, name=name or f"Channel {idx}", secret=bytes(secret)))
        return out

    async def fetch_next_message(self) -> IncomingMessage | None:
        from meshcore import EventType

        mc = self._require()
        res = await mc.commands.get_msg(timeout=COMMAND_TIMEOUT)
        if res is None:
            raise RadioError("no response to message fetch")
        if res.type == EventType.NO_MORE_MSGS:
            return None
        if res.is_error():
            raise RadioError(f"message fetch failed ({_describe(res)})")
        p = _payload(res)
        meta = {k: p[k] for k in ("SNR", "path_len", "path_hash_mode", "RSSI") if k in p}
        text = str(p.get("text") or "")
        if res.type == EventType.CONTACT_MSG_RECV:
            return IncomingMessage(
                kind="dm",
                text=text,
                txt_type=int(p.get("txt_type") or 0),
                sender_timestamp=p.get("sender_timestamp"),
                pubkey_prefix=p.get("pubkey_prefix"),
                meta=meta,
            )
        if res.type == EventType.CHANNEL_MSG_RECV:
            label, body = split_channel_text(text)
            return IncomingMessage(
                kind="channel",
                text=body,
                txt_type=int(p.get("txt_type") or 0),
                sender_timestamp=p.get("sender_timestamp"),
                channel_slot=p.get("channel_idx"),
                sender_label=label,
                meta=meta,
            )
        raise RadioError(f"unexpected response to message fetch ({_describe(res)})")

    async def send_channel(
        self, slot: int, text: str, timestamp: int, scope: str | None = None, after_text: bytes = b""
    ) -> SendResult:
        mc = self._require()
        if scope:
            # Like the MeshCore app: scope this one send, then revert to the radio's default.
            # Untested on hardware.
            res = await mc.commands.set_flood_scope(scope)
            if res is None or res.is_error():
                return SendResult(ok=False, error=f"could not set region scope ({_describe(res)})")
        try:
            if after_text:
                from meshcore import EventType

                # CMD_SEND_CHANNEL_TXT_MSG with raw bytes: the library only sends UTF-8 text, and
                # the firmware passes the text bytes on as given, zero byte and all.
                frame = b"\x03\x00" + bytes([slot]) + timestamp.to_bytes(4, "little")
                res = await mc.commands.send(
                    frame + text.encode() + b"\0" + after_text, [EventType.OK, EventType.ERROR]
                )
            else:
                res = await mc.commands.send_chan_msg(slot, text, timestamp)
        finally:
            if scope:
                reset = await mc.commands.set_flood_scope(None)
                if reset is None or reset.is_error():
                    log.warning("could not revert the flood scope (%s)", _describe(reset))
        if res is None or res.is_error():
            return SendResult(ok=False, error=_describe(res))
        return SendResult(ok=True)

    async def sign(self, data: bytes) -> bytes:
        mc = self._require()
        res = await mc.commands.sign(data)
        sig = _payload(res).get("signature") if res is not None and not res.is_error() else None
        if not isinstance(sig, bytes | bytearray) or len(sig) != 64:
            raise RadioError(f"the radio did not sign ({_describe(res)})")
        return bytes(sig)

    async def send_dm(self, public_key: str, text: str, timestamp: int, attempt: int = 0) -> SendResult:
        mc = self._require()
        # One transmission. Retries (and the switch to flood) are run by the supervisor, so they
        # survive restarts and show in the app, rather than inside the library's send_msg_with_retry.
        res = await mc.commands.send_msg(public_key, text, timestamp, attempt=attempt)
        if res is None or res.is_error():
            return SendResult(ok=False, error=_describe(res))
        p = _payload(res)
        ack = p.get("expected_ack")
        ack_hex = ack.hex() if isinstance(ack, bytes | bytearray) else (str(ack) if ack else None)
        return SendResult(
            ok=True,
            expected_ack=ack_hex,
            suggested_timeout_ms=p.get("suggested_timeout"),
            flood=p.get("type") == 1 if p.get("type") is not None else None,
        )

    async def sync_clock(self, epoch_seconds: int) -> None:
        mc = self._require()
        res = await mc.commands.set_time(epoch_seconds)
        if res is None or res.is_error():
            log.info("radio clock sync not accepted (%s)", _describe(res))

    # ---- node configuration -------------------------------------------------------------
    # Mirrors the MeshCore companion firmware (examples/companion_radio/MyMesh.cpp). Untested on
    # hardware: every call checks for an error event and reports it rather than assuming success.

    async def _ok(self, coro, what: str):
        res = await coro
        if res is None or res.is_error():
            raise RadioError(f"{what} rejected by radio ({_describe(res)})")
        return res

    async def _fresh_self_info(self) -> dict[str, Any]:
        mc = self._require()
        res = await self._ok(mc.commands.send_appstart(), "reading settings")
        self._self_info = dict(_payload(res))
        return self._self_info

    async def read_config(self) -> dict[str, Any]:
        from meshcore import EventType

        mc = self._require()
        si = await self._fresh_self_info()
        dres = await mc.commands.send_device_query()
        if dres is not None and not dres.is_error():
            self._device_info = _payload(dres)
        di = self._device_info
        fw_ver = int(di.get("fw ver") or 0)

        tuning = None
        tres = await mc.commands.get_tuning()
        if tres is not None and tres.type == EventType.TUNING_PARAMS:
            p = _payload(tres)
            tuning = {
                "rx_delay": p.get("rx_delay", 0) / 1000,
                "airtime_factor": p.get("airtime_factor", 0) / 1000,
            }

        custom_vars = None
        cres = await mc.commands.get_custom_vars()
        if cres is not None and cres.type == EventType.CUSTOM_VARS:
            custom_vars = {str(k): str(v) for k, v in _payload(cres).items()}

        flood_scope = None
        try:
            fres = await mc.commands.get_default_flood_scope()
            if fres is not None and fres.type == EventType.DEFAULT_FLOOD_SCOPE:
                flood_scope = str(_payload(fres).get("scope_name") or "")
        except Exception:  # noqa: BLE001 - older firmware/library combinations
            flood_scope = None

        channels = []
        max_channels = int(di.get("max_channels") or MAX_CHANNEL_SLOTS_FALLBACK)
        for idx in range(max_channels):
            res = await mc.commands.get_channel(idx)
            if res is None or res.is_error():
                continue
            p = _payload(res)
            name = str(p.get("channel_name") or "").strip()
            secret = bytes(p.get("channel_secret") or b"")
            channels.append({"slot": idx, "name": name, "key": channel_key_kind(name, secret)})

        lat, lon = advert_position(si.get("adv_lat"), si.get("adv_lon"))
        return {
            "simulated": False,
            "firmware": {
                "version_code": fw_ver,
                "version": di.get("ver"),
                "build": di.get("fw_build"),
                "model": di.get("model"),
            },
            "identity": {
                "name": si.get("name", ""),
                "lat": lat,
                "lon": lon,
                "share_location": si.get("adv_loc_policy") == 1,
            },
            "radio": {
                "freq_mhz": si.get("radio_freq"),
                "bw_khz": si.get("radio_bw"),
                "sf": si.get("radio_sf"),
                "cr": si.get("radio_cr"),
                "tx_power_dbm": si.get("tx_power"),
                "max_tx_power_dbm": si.get("max_tx_power"),
                # Client repeat exists from firmware version code 9.
                "repeat": di.get("repeat") if fw_ver >= 9 else None,
            },
            "behavior": {
                "auto_add_contacts": not si.get("manual_add_contacts", False),
                "multi_acks": int(si.get("multi_acks") or 0),
                "path_hash_mode": di.get("path_hash_mode") if fw_ver >= 10 else None,
                "default_flood_scope": flood_scope,
            },
            "telemetry": {
                "base": si.get("telemetry_mode_base", 0),
                "location": si.get("telemetry_mode_loc", 0),
                "environment": si.get("telemetry_mode_env", 0),
            },
            "tuning": tuning,
            "channels": channels,
            "max_channels": max_channels,
            "custom_vars": custom_vars,
        }

    async def _set_other(self, **changes) -> None:
        mc = self._require()
        infos = dict(await self._fresh_self_info())
        infos.update(changes)
        infos["manual_add_contacts"] = bool(infos.get("manual_add_contacts"))
        for k in (
            "telemetry_mode_base",
            "telemetry_mode_loc",
            "telemetry_mode_env",
            "adv_loc_policy",
            "multi_acks",
        ):
            infos[k] = int(infos.get(k) or 0)
        await self._ok(mc.commands.set_other_params_from_infos(infos), "updating preferences")

    async def configure(self, op: str, params: dict[str, Any]) -> dict[str, Any]:
        mc = self._require()
        c = mc.commands
        if op == "identity":
            await self._ok(c.set_name(params["name"]), "setting the name")
            if params.get("lat") is not None and params.get("lon") is not None:
                await self._ok(c.set_coords(params["lat"], params["lon"]), "setting the location")
            else:
                await self._ok(c.set_coords(0.0, 0.0), "clearing the location")
            await self._set_other(adv_loc_policy=1 if params["share_location"] else 0)
        elif op == "radio":
            repeat = params.get("repeat")
            await self._ok(
                c.set_radio(
                    params["freq_mhz"],
                    params["bw_khz"],
                    params["sf"],
                    params["cr"],
                    None if repeat is None else int(repeat),
                ),
                "setting LoRa parameters",
            )
            await self._ok(c.set_tx_power(params["tx_power_dbm"]), "setting TX power")
        elif op == "behavior":
            await self._set_other(
                manual_add_contacts=not params["auto_add_contacts"], multi_acks=params["multi_acks"]
            )
            if params.get("path_hash_mode") is not None:
                await self._ok(c.set_path_hash_mode(params["path_hash_mode"]), "setting path hash mode")
            if params.get("default_flood_scope") is not None:
                await self._ok(
                    c.set_default_flood_scope(params["default_flood_scope"] or None), "setting flood scope"
                )
        elif op == "telemetry":
            await self._set_other(
                telemetry_mode_base=params["base"],
                telemetry_mode_loc=params["location"],
                telemetry_mode_env=params["environment"],
            )
        elif op == "tuning":
            await self._ok(
                c.set_tuning(round(params["rx_delay"] * 1000), round(params["airtime_factor"] * 1000)),
                "setting tuning",
            )
        elif op == "channel":
            secret = params.get("secret")  # bytes; None means "derive from #name"
            if params["name"].startswith("#"):
                secret = None
            await self._ok(c.set_channel(params["slot"], params["name"], secret), "saving the channel")
        elif op == "channel_clear":
            await self._ok(c.set_channel(params["slot"], "", bytes(16)), "clearing the channel")
        elif op == "custom_var":
            await self._ok(c.set_custom_var(params["key"], params["value"]), "setting the variable")
        elif op == "advert":
            await self._ok(c.send_advert(flood=bool(params.get("flood"))), "sending the advert")
        elif op == "sync_clock":
            await self._ok(c.set_time(int(params["epoch"])), "setting the clock")
        elif op == "reboot":
            await c.reboot()  # the firmware reboots without replying
        elif op.startswith("contact_"):
            return await self._contact_op(op, params)
        else:
            raise NotSupported(f"unknown operation {op!r}")
        if op in ("identity", "radio", "behavior", "telemetry"):
            await self._fresh_self_info()  # so the next device snapshot reflects the change
        return {}

    # ---- contact operations -----------------------------------------------------------

    def path_hash_size(self) -> int:
        return int(self._device_info.get("path_hash_mode") or 0) + 1

    async def _radio_contact(self, public_key: str) -> dict[str, Any]:
        """update_contact needs the library's full contact record; refresh the cache if missing."""
        mc = self._require()
        contact = (mc.contacts or {}).get(public_key)
        if contact is None:
            await self._ok(mc.commands.get_contacts(timeout=COMMAND_TIMEOUT), "reading contacts")
            contact = (mc.contacts or {}).get(public_key)
        if contact is None:
            raise RadioError("this contact is no longer on the radio")
        return contact

    async def _contact_op(self, op: str, params: dict[str, Any]) -> dict[str, Any]:
        mc = self._require()
        c = mc.commands
        key = params["public_key"]
        if op == "contact_favorite":
            contact = await self._radio_contact(key)
            flags = int(contact.get("flags") or 0)
            flags = (flags | 0x01) if params["favorite"] else (flags & ~0x01)
            await self._ok(c.change_contact_flags(contact, flags), "updating the favourite flag")
        elif op == "contact_reset_path":
            await self._ok(c.reset_path(key), "resetting the path")
        elif op == "contact_set_path":
            contact = await self._radio_contact(key)
            mode = self.path_hash_size() - 1
            await self._ok(
                c.change_contact_path(contact, params["path_hex"], path_hash_mode=mode), "setting the path"
            )
        elif op == "contact_remove":
            await self._ok(c.remove_contact(key), "removing the contact")
        elif op == "contact_share":
            await self._ok(c.share_contact(key), "sharing the contact")
        elif op == "contact_add":
            # A contact from a QR code or typed in: no advert heard yet, so no path (flood) and no
            # position. The radio fills in the rest when it hears the node's advert.
            contact = {
                "public_key": params["public_key"],
                "type": params["kind"],
                "flags": 0,
                "out_path_len": -1,
                "out_path": "",
                "out_path_hash_mode": 0,
                "adv_name": params["name"],
                "last_advert": 0,
                "adv_lat": 0.0,
                "adv_lon": 0.0,
            }
            await self._ok(c.add_contact(contact), "adding the contact")
        elif op == "contact_export":
            res = await self._ok(c.export_contact(key), "exporting the contact")
            return {"uri": _payload(res).get("uri")}
        else:
            raise NotSupported(f"unknown operation {op!r}")
        return {}

    # ---- radio backups ------------------------------------------------------------------

    async def export_private_key(self) -> bytes:
        from meshcore import EventType

        mc = self._require()
        res = await mc.commands.export_private_key()
        if res is not None and res.type == EventType.PRIVATE_KEY:
            key = bytes(_payload(res).get("private_key") or b"")
            if len(key) != 64:
                raise RadioError("the radio sent a malformed identity key")
            return key
        if res is not None and res.type == EventType.DISABLED:
            raise NotSupported("exporting the identity key is turned off in this radio's firmware")
        raise RadioError(f"exporting the identity key failed ({_describe(res)})")

    async def import_private_key(self, private_key: bytes) -> None:
        mc = self._require()
        await self._ok(mc.commands.import_private_key(bytes(private_key)), "importing the identity key")

    async def contact_records(self) -> list[dict[str, Any]]:
        mc = self._require()
        res = await self._ok(mc.commands.get_contacts(timeout=COMMAND_TIMEOUT), "reading contacts")
        return [
            {k: c.get(k) for k in CONTACT_RECORD_FIELDS}
            for c in _payload(res).values()
            if isinstance(c, dict) and isinstance(c.get("public_key"), str) and len(c["public_key"]) == 64
        ]

    async def add_contact_record(self, record: dict[str, Any]) -> None:
        mc = self._require()
        await self._ok(mc.commands.add_contact(dict(record)), "adding the contact")

    # ---- remote administration ----------------------------------------------------------
    # Mirrors the library's *_sync helpers (commands/binary.py, messaging.py), split into send and
    # wait so the command lock is not held while the reply crosses the mesh. Reply events are
    # subscribed to before sending, then matched by tag (or, for login, by key prefix).
    # Untested on hardware: written against the meshcore 2.3.14 source.

    async def remote_send(self, public_key: str, kind: str, arg: str | None = None) -> RemoteTicket:
        from meshcore import EventType
        from meshcore.packets import AnonReqType, BinaryReqType

        mc = self._require()
        c = mc.commands
        if kind == "logout":
            await self._ok(c.send_logout(public_key), "logging out")
            return RemoteTicket(kind=kind, public_key=public_key, timeout=0)
        if kind == "cli":
            # The reply is a CLI text message, fetched by the supervisor's receive loop.
            res = await self._ok(c.send_cmd(public_key, arg or "", dst_type=2), "sending the command")
            return self._ticket(kind, public_key, res, None)

        events = {
            "login": [EventType.LOGIN_SUCCESS, EventType.LOGIN_FAILED],
            "status": [EventType.STATUS_RESPONSE],
            "telemetry": [EventType.TELEMETRY_RESPONSE],
            "acl": [EventType.ACL_RESPONSE],
            "neighbours": [EventType.NEIGHBOURS_RESPONSE],
            "owner": [EventType.BINARY_RESPONSE],
            "regions": [EventType.BINARY_RESPONSE],
        }.get(kind)
        if events is None:
            raise NotSupported(f"unknown remote request {kind!r}")
        received: list[Any] = []
        arrived = asyncio.Event()

        async def _collect(event):
            received.append(event)
            arrived.set()

        subs = [mc.subscribe(e, _collect) for e in events]
        try:
            if kind == "login":
                res = await self._ok(c.send_login(public_key, arg or ""), "sending the login")
            elif kind in ("owner", "regions"):
                req = AnonReqType.OWNER if kind == "owner" else AnonReqType.REGIONS
                res = await self._ok(c.send_anon_req(public_key, req), "sending the request")
            elif kind == "neighbours":
                offset = int(arg or 0)
                data = (
                    b"\x00"  # request version
                    + (255).to_bytes(1, "little")  # as many as fit
                    + offset.to_bytes(2, "little")
                    + b"\x00"  # newest first
                    + NEIGHBOUR_PREFIX_BYTES.to_bytes(1, "little")
                    + os.urandom(4)
                )
                res = await self._ok(
                    c.send_binary_req(
                        public_key,
                        BinaryReqType.NEIGHBOURS,
                        data=data,
                        context={"pubkey_prefix_length": NEIGHBOUR_PREFIX_BYTES},
                    ),
                    "sending the request",
                )
            else:
                req = {
                    "status": BinaryReqType.STATUS,
                    "telemetry": BinaryReqType.TELEMETRY,
                    "acl": BinaryReqType.ACL,
                }[kind]
                res = await self._ok(
                    c.send_binary_req(public_key, req, data=b"\0\0" if kind == "acl" else None),
                    "sending the request",
                )
        except BaseException:
            for s in subs:
                mc.unsubscribe(s)
            raise
        return self._ticket(kind, public_key, res, (subs, received, arrived))

    @staticmethod
    def _ticket(kind: str, public_key: str, res: Any, handle: Any) -> RemoteTicket:
        p = _payload(res)
        ack = p.get("expected_ack")
        tag = ack.hex() if isinstance(ack, bytes | bytearray) else None
        suggested = p.get("suggested_timeout")
        timeout = float(suggested) / 800 if isinstance(suggested, int | float) and suggested > 0 else 15.0
        return RemoteTicket(kind=kind, public_key=public_key, tag=tag, timeout=timeout, handle=handle)

    def _match(self, ticket: RemoteTicket, event: Any) -> Any:
        from meshcore import EventType

        p = _payload(event)
        if ticket.kind == "login":
            if str(p.get("pubkey_prefix") or "") != ticket.public_key[:12]:
                return None
            if event.type == EventType.LOGIN_FAILED:
                return {"ok": False, "admin": False, "permissions": None}
            perms = p.get("permissions")
            return {"ok": True, "admin": bool(p.get("is_admin")), "permissions": perms}
        # The library puts the tag in the event attributes (STATUS_RESPONSE has it nowhere else) and
        # copies it into most payloads. Older firmware pushes status untagged: match its key prefix.
        attrs = getattr(event, "attributes", None) or {}
        tag = attrs.get("tag") or p.get("tag")
        if tag is None and ticket.kind == "status":
            if str(p.get("pubkey_pre") or attrs.get("pubkey_prefix") or "") != ticket.public_key[:12]:
                return None
        elif tag != ticket.tag:
            return None
        if ticket.kind in ("owner", "regions"):
            # Anonymous replies: 4-byte remote timestamp, then UTF-8 text.
            try:
                raw = bytes.fromhex(str(p.get("data") or ""))
            except ValueError:
                raise RadioError("unreadable reply from the node") from None
            text = raw[4:].decode("utf-8", errors="replace").rstrip("\0")
            return {"text": text}
        if ticket.kind == "acl":
            return {"acl": p.get("acl_data") or []}
        if ticket.kind == "telemetry":
            return {"lpp": p.get("lpp") or []}
        return {k: v for k, v in p.items() if k not in ("tag", "pubkey_prefix", "pubkey_pre")}

    async def trace_send(self, path: bytes, flags: int) -> RemoteTicket:
        from meshcore import EventType

        mc = self._require()
        tag = random.randint(1, 0xFFFFFFFF)
        received: list[dict[str, Any]] = []
        arrived = asyncio.Event()

        async def _collect(event):
            p = _payload(event)
            if p.get("tag") == tag:
                received.append(p)
                arrived.set()

        sub = mc.subscribe(EventType.TRACE_DATA, _collect)  # before sending: the reply can be quick
        try:
            res = await self._ok(
                mc.commands.send_trace(auth_code=0, tag=tag, flags=flags, path=path), "sending the trace"
            )
        except BaseException:
            mc.unsubscribe(sub)
            raise
        est_ms = _payload(res).get("suggested_timeout") or 0
        return RemoteTicket(
            kind="trace",
            public_key="",
            tag=str(tag),
            timeout=max(5.0, est_ms / 1000 * 1.5),
            handle=(sub, received, arrived),
        )

    async def trace_wait(self, ticket: RemoteTicket, timeout: float) -> dict[str, Any]:
        sub, received, arrived = ticket.handle
        try:
            await asyncio.wait_for(arrived.wait(), timeout)
            return received[0]
        finally:
            ticket.handle = None
            if self._mc is not None:
                self._mc.unsubscribe(sub)

    async def remote_wait(self, ticket: RemoteTicket, timeout: float) -> Any:
        if ticket.handle is None:
            return True
        subs, received, arrived = ticket.handle
        mc = self._mc
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        try:
            while True:
                while received:
                    found = self._match(ticket, received.pop(0))
                    if found is not None:
                        return found
                arrived.clear()
                left = deadline - loop.time()
                if left <= 0:
                    raise TimeoutError
                await asyncio.wait_for(arrived.wait(), left)
        finally:
            ticket.handle = None
            if mc is not None:
                for s in subs:
                    mc.unsubscribe(s)
