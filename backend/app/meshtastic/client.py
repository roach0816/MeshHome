"""A connection to a Meshtastic node's client API (what the phone apps use), over TCP.

meshtasticd (Meshtastic on Linux, e.g. on the Pi's radio HAT) and Wi-Fi nodes serve it on port
4403. The client asks for the node's configuration (`want_config_id`); the node answers with its
own node info, its node list, channels and settings, then `config_complete_id`, and from then on
passes on every packet it receives, already decrypted with its channel keys.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import struct
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.meshtastic import proto as P

log = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 60.0
HANDSHAKE_TIMEOUT = 20.0
# The node only accepts admin messages carrying the session key it handed out in the last 300 s.
SESSION_KEY_SECONDS = 240.0
# Routing.error_reason values worth naming.
ROUTING_ERRORS = {
    1: "no route",
    2: "not acknowledged",
    3: "timed out",
    5: "no acknowledgement after retries",
    6: "not on a shared channel",
    7: "too large",
    8: "no response",
    9: "over its airtime limit",
    32: "bad request",
    33: "not authorized",
    36: "the admin session key was rejected",
    38: "rate limited",
}


class MeshtasticError(Exception):
    pass


class Link(ABC):
    """Where FromRadio messages come from: a TCP connection, or the simulated node."""

    @abstractmethod
    async def open(self) -> None: ...

    @abstractmethod
    async def read(self) -> bytes:
        """The next FromRadio protobuf. Raises ConnectionError when the node goes away."""

    @abstractmethod
    async def write(self, payload: bytes) -> None:
        """Send one ToRadio protobuf."""

    @abstractmethod
    async def close(self) -> None: ...


class TcpLink(Link):
    def __init__(self, host: str, port: int = 4403) -> None:
        self.host, self.port = host, port
        self._r: asyncio.StreamReader | None = None
        self._w: asyncio.StreamWriter | None = None

    async def open(self) -> None:
        try:
            self._r, self._w = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), 10)
        except (OSError, TimeoutError) as exc:
            raise ConnectionError(
                f"couldn't connect to {self.host}:{self.port} ({exc or 'timed out'})"
            ) from None

    async def read(self) -> bytes:
        if self._r is None:
            raise ConnectionError("not connected")
        try:
            while True:
                # Anything outside a frame (debug text on serial links) is skipped.
                if await self._r.readexactly(1) != b"\x94":
                    continue
                if await self._r.readexactly(1) != b"\xc3":
                    continue
                (n,) = struct.unpack(">H", await self._r.readexactly(2))
                if n > 512:
                    continue  # not a real frame: resynchronise
                return await self._r.readexactly(n)
        except asyncio.IncompleteReadError:
            raise ConnectionError("the node closed the connection") from None

    async def write(self, payload: bytes) -> None:
        if self._w is None:
            raise ConnectionError("not connected")
        try:
            self._w.write(P.frame(payload))
            await self._w.drain()
        except OSError as exc:
            raise ConnectionError(str(exc)) from None

    async def close(self) -> None:
        if self._w is not None:
            self._w.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._w.wait_closed(), 2)
        self._r = self._w = None


@dataclass
class Snapshot:
    """What the node told us when we connected."""

    my_num: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    nodes: dict[int, dict[str, Any]] = field(default_factory=dict)
    channels: dict[int, dict[str, Any]] = field(default_factory=dict)
    lora: dict[str, Any] = field(default_factory=dict)
    device: dict[str, Any] = field(default_factory=dict)


class Connection:
    """One session with a node: the handshake, then its packets until it goes away."""

    def __init__(self, link: Link) -> None:
        self.link = link
        self.snapshot = Snapshot()
        self._pending: dict[int, asyncio.Future] = {}  # packet id -> future of its reply
        self._session_key: tuple[bytes, float] | None = None
        self._buffered: list[dict[str, Any]] = []  # packets that arrived during the handshake

    async def start(self) -> Snapshot:
        await self.link.open()
        nonce = random.randint(1, 0x7FFFFFFF)
        await self.link.write(P.encode({"want_config_id": nonce}, P.TO_RADIO))
        snap = Snapshot()
        deadline = time.monotonic() + HANDSHAKE_TIMEOUT
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise MeshtasticError("the node didn't send its configuration")
            try:
                msg = P.decode(await asyncio.wait_for(self.link.read(), left), P.FROM_RADIO)
            except TimeoutError:
                raise MeshtasticError("the node didn't send its configuration") from None
            if "my_info" in msg:
                snap.my_num = int(msg["my_info"].get("my_node_num") or 0)
            elif "node_info" in msg:
                ni = msg["node_info"]
                if ni.get("num"):
                    snap.nodes[int(ni["num"])] = ni
            elif "channel" in msg:
                snap.channels[int(msg["channel"].get("index") or 0)] = msg["channel"]
            elif "config" in msg:
                cfg = msg["config"]
                if "lora" in cfg:
                    snap.lora = cfg["lora"]
                elif "device" in cfg:
                    snap.device = cfg["device"]
            elif "metadata" in msg:
                snap.metadata = msg["metadata"]
            elif "packet" in msg:
                self._buffered.append(msg)
            elif msg.get("config_complete_id") == nonce:
                break
        if not snap.my_num:
            raise MeshtasticError("the node didn't say who it is")
        self.snapshot = snap
        return snap

    async def run(self, on_message: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        """Pass every message on (replies to our requests are also matched to them) until the
        node goes away (ConnectionError) or this task is cancelled."""
        beat = asyncio.create_task(self._heartbeat(), name="meshtastic-heartbeat")
        try:
            for msg in self._buffered:
                await self._dispatch(msg, on_message)
            self._buffered.clear()
            while True:
                if beat.done():
                    beat.result()  # its error ends the session
                raw = await self.link.read()
                try:
                    msg = P.decode(raw, P.FROM_RADIO)
                except P.ProtoError as exc:
                    log.info("meshtastic: skipped a message it couldn't decode (%s)", exc)
                    continue
                await self._dispatch(msg, on_message)
        finally:
            beat.cancel()
            with contextlib.suppress(BaseException):
                await beat
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("the connection to the node closed"))
            self._pending.clear()

    async def _dispatch(
        self, msg: dict[str, Any], on_message: Callable[[dict[str, Any]], Awaitable[None]]
    ) -> None:
        pkt = msg.get("packet")
        dec = pkt.get("decoded") if pkt else None
        if dec and dec.get("request_id") and dec.get("portnum") in (P.ROUTING_APP, P.ADMIN_APP):
            fut = self._pending.get(int(dec["request_id"]))
            if fut is not None and not fut.done():
                if dec["portnum"] == P.ADMIN_APP:
                    fut.set_result(P.decode(dec.get("payload") or b"", P.ADMIN))
                else:
                    err = (P.decode(dec.get("payload") or b"", {3: ("error_reason", "enum")})).get(
                        "error_reason", 0
                    )
                    # A plain acknowledgement only settles a request that expects no answer.
                    if err:
                        fut.set_exception(MeshtasticError(ROUTING_ERRORS.get(err, f"error {err}")))
                    elif getattr(fut, "ack_only", False):
                        fut.set_result({})
        await on_message(msg)

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            await self.link.write(P.encode({"heartbeat": {}}, P.TO_RADIO))

    # ---- sending -----------------------------------------------------------------------------

    async def send(
        self,
        to: int,
        portnum: int,
        payload: bytes,
        *,
        channel: int = 0,
        want_response: bool = False,
        want_ack: bool = False,
        hop_limit: int | None = None,
    ) -> int:
        pid = random.randint(1, 0xFFFFFFFF)
        pkt: dict[str, Any] = {
            "to": to,
            "id": pid,
            "channel": channel,
            "want_ack": want_ack,
            "decoded": {"portnum": portnum, "payload": payload, "want_response": want_response},
        }
        if hop_limit:
            pkt["hop_limit"] = hop_limit
        await self.link.write(P.encode({"packet": pkt}, P.TO_RADIO))
        return pid

    async def admin(
        self, msg: dict[str, Any], *, response: bool = False, timeout: float = 10.0
    ) -> dict[str, Any]:
        """An admin message to the connected node itself. With `response`, its answer; otherwise
        an empty dict once it's acknowledged."""
        msg = dict(msg)
        if not any(k.startswith("get_") for k in msg):
            msg["session_passkey"] = await self._passkey()
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        fut.ack_only = not response  # type: ignore[attr-defined]
        pid = random.randint(1, 0xFFFFFFFF)
        self._pending[pid] = fut
        pkt = {
            "to": self.snapshot.my_num,
            "id": pid,
            "want_ack": True,
            "decoded": {"portnum": P.ADMIN_APP, "payload": P.encode(msg, P.ADMIN), "want_response": response},
        }
        try:
            await self.link.write(P.encode({"packet": pkt}, P.TO_RADIO))
            result = await asyncio.wait_for(fut, timeout)
        except TimeoutError:
            raise MeshtasticError("the node didn't answer the settings request") from None
        finally:
            self._pending.pop(pid, None)
        if isinstance(result, dict) and result.get("session_passkey"):
            self._session_key = (result["session_passkey"], time.monotonic())
        return result

    async def _passkey(self) -> bytes:
        if self._session_key and time.monotonic() - self._session_key[1] < SESSION_KEY_SECONDS:
            return self._session_key[0]
        r = await self.admin({"get_config_request": P.SESSIONKEY_CONFIG_TYPE}, response=True)
        key = r.get("session_passkey")
        if not key:
            raise MeshtasticError("the node didn't hand out an admin session key")
        return key

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.link.write(P.encode({"disconnect": True}, P.TO_RADIO))
        await self.link.close()
