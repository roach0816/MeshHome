"""Meshtastic nodes and trackers: what MeshHome's Meshtastic radio (radio/meshtastic_radio.py)
hears besides messages: node info, positions, battery and sensor readings.

Every node heard is kept in the node list (MeshNode), for Tracking's "Add a tracker". Meshtastic
trackers, such as a SenseCAP X1 running Meshtastic, send their position on their own, on a timer or
when they've moved ("smart position"), and their battery and sensor readings on other timers. So
nothing is polled: every position heard from a tracked node is recorded, with the hop count and last
relay of the packet. "Ask now" sends a position request, which a tracker answers at most every few
minutes.

The trackers and the radio must share a channel. On the default public channel Meshtastic sends
positions blurred to about 1.5 km and anyone nearby can follow them; a private channel with full
position precision (Settings → Node settings → Channels) is shared with the trackers by QR code.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select

from app import db
from app.meshtastic import proto as P
from app.meshtastic.client import MeshtasticError
from app.models import MeshNode, TrackedDevice, TrackPosition, utcnow
from app.radio.meshtastic_radio import MeshtasticRadio
from app.realtime import hub
from app.services import app_settings

log = logging.getLogger(__name__)

HAT_HOST, HAT_PORT = "127.0.0.1", 4403
ASK_TIMEOUT = 60.0
# Environment readings arrive apart from positions: they're kept with a position this recent.
ENV_WITH_POSITION = timedelta(minutes=15)
NODE_RETENTION = timedelta(days=30)
CHANNEL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,11}$")
NO_ANSWER = (
    "No answer to the position request. It may be out of range, or it answered another request "
    "in the last few minutes (Meshtastic answers at most every 3 minutes); its broadcasts still count."
)


def _clean(d: Any) -> Any:
    if isinstance(d, dict):
        return {k: _clean(v) for k, v in d.items() if k != "_raw"}
    if isinstance(d, list):
        return [_clean(x) for x in d]
    return d


def private_channel(ch: dict[str, Any] | None) -> bool:
    psk = ((ch or {}).get("settings") or {}).get("psk") or b""
    return len(psk) in (16, 32)


def hop_path(pkt: dict[str, Any]) -> list[str] | None:
    """The packet's hops for TrackPosition.hops, tracker's end first: one entry per hop, the last
    one the relay that passed it to this node (its node number's low byte, as Meshtastic gives it),
    "?" for the ones before. [] when heard directly; None when the packet doesn't say."""
    start, left = pkt.get("hop_start"), pkt.get("hop_limit")
    relay = pkt.get("relay_node")
    frm = int(pkt.get("from") or 0)
    if start is None and left is None:
        return None
    hops = max(0, int(start or 0) - int(left or 0)) if start else None
    if hops is None:
        return None
    if hops == 0:
        return []
    last = f"{int(relay):02x}" if relay and int(relay) != (frm & 0xFF) else "?"
    return ["?"] * (hops - 1) + [last]


class MeshtasticSource:
    def __init__(self) -> None:
        self.adapter: MeshtasticRadio | None = None
        self.simulated = False
        self._asking: dict[int, float] = {}  # node num -> monotonic deadline
        self._pending_env: dict[int, dict[str, float]] = {}  # node num -> readings
        self._timers: set[asyncio.Task] = set()
        self._seen: deque[tuple[int, int]] = deque(maxlen=500)  # (from, packet id) recently heard

    # ---- lifecycle -------------------------------------------------------------------

    def start(self) -> None:
        self.__init__()  # type: ignore[misc]  # a fresh start (the tests start the app many times)

    async def stop(self) -> None:
        for t in self._timers:
            t.cancel()
        await asyncio.gather(*self._timers, return_exceptions=True)
        self._timers.clear()
        self.adapter = None

    async def attach(self, adapter: MeshtasticRadio, radio: Any = None) -> None:
        """The radio connected (radio/supervisor.py): record its node list, then its packets."""
        self.adapter = adapter
        self.simulated = adapter.is_simulated
        adapter.on_mesh_packet = self._on_packet
        try:
            await self._on_snapshot(adapter)
        except Exception as exc:  # noqa: BLE001 - tracking must never stop the radio connecting
            log.warning("meshtastic: could not record the node list: %s", exc)
        self._publish()

    def detach(self) -> None:
        if self.adapter is not None:
            self.adapter.on_mesh_packet = None
            self.adapter = None
            self._publish()

    @property
    def connected(self) -> bool:
        return self.adapter is not None and self.adapter.conn is not None

    def _publish(self) -> None:
        hub.publish("tracking-updated")

    # ---- what the node tells us ------------------------------------------------------

    async def _on_snapshot(self, adapter: MeshtasticRadio) -> None:
        recorded = False
        simulated = adapter.is_simulated
        async with db.session_factory()() as s:
            for num, ni in list(adapter.nodes.items()):
                node = await self._node(s, num, simulated)
                self._apply_user(node, ni.get("user") or {})
                if ni.get("last_heard"):
                    node.last_heard_at = datetime.fromtimestamp(int(ni["last_heard"]), UTC)
                if ni.get("snr") is not None:
                    node.snr = round(float(ni["snr"]), 2)
                if ni.get("hops_away") is not None:
                    node.hops_away = int(ni["hops_away"])
                self._apply_metrics(node, ni.get("device_metrics") or {})
                pos = ni.get("position") or {}
                fix = P.position_fix(pos)
                if fix and num != adapter.my_num:
                    at = datetime.fromtimestamp(
                        int(pos.get("time") or ni.get("last_heard") or time.time()), UTC
                    )
                    at = min(at, utcnow())
                    self._apply_position(node, fix, at)
                    # A position from while MeshHome wasn't connected (the node kept it).
                    recorded |= await self._record(s, num, fix, at, None, only_newer=True)
            await s.execute(
                delete(MeshNode).where(
                    MeshNode.updated_at < utcnow() - NODE_RETENTION,
                    MeshNode.num.not_in(
                        select(TrackedDevice.mesh_node_num).where(TrackedDevice.mesh_node_num.is_not(None))
                    ),
                )
            )
            await s.commit()
        if recorded:
            hub.publish("tracking-updated")

    async def _on_packet(self, pkt: dict[str, Any]) -> None:
        dec = pkt.get("decoded")
        frm = int(pkt.get("from") or 0)
        me = self.adapter.my_num if self.adapter else 0
        if not dec or not frm or frm == me:
            return
        key = (frm, int(pkt.get("id") or 0))
        if key[1]:
            # The same packet again (a copy that took another path): count it once.
            if key in self._seen:
                return
            self._seen.append(key)
        port = dec.get("portnum")
        if port not in (P.POSITION_APP, P.NODEINFO_APP, P.TELEMETRY_APP):
            return
        try:
            body = P.decode(
                dec.get("payload") or b"",
                {P.POSITION_APP: P.POSITION, P.NODEINFO_APP: P.USER, P.TELEMETRY_APP: P.TELEMETRY}[port],
            )
        except P.ProtoError:
            return
        now = utcnow()
        simulated = self.simulated
        recorded = False
        async with db.session_factory()() as s:
            node = await self._node(s, frm, simulated)
            node.last_heard_at = now
            if pkt.get("rx_snr"):
                node.snr = round(float(pkt["rx_snr"]), 2)
            hops = hop_path(pkt)
            if hops is not None:
                node.hops_away = len(hops)
            if port == P.NODEINFO_APP:
                self._apply_user(node, body)
            elif port == P.TELEMETRY_APP:
                dm, env = body.get("device_metrics"), body.get("environment_metrics")
                if dm:
                    self._apply_metrics(node, dm)
                    recorded |= await self._battery(s, frm, dm)
                if env:
                    recorded |= await self._environment(s, frm, env)
            elif port == P.POSITION_APP:
                fix = P.position_fix(body)
                if fix is not None:
                    t = body.get("time")
                    # Its own clock (GPS time) when that's plausible; when it was heard otherwise.
                    at = now
                    if t and timedelta(0) <= now - datetime.fromtimestamp(int(t), UTC) <= timedelta(
                        minutes=10
                    ):
                        at = datetime.fromtimestamp(int(t), UTC)
                    self._apply_position(node, fix, at)
                    link = {
                        "snr": round(float(pkt["rx_snr"]), 2) if pkt.get("rx_snr") else None,
                        "rssi": float(pkt["rx_rssi"]) if pkt.get("rx_rssi") else None,
                        "route": "mesh",
                        "hops": hops,
                    }
                    self._asking.pop(frm, None)
                    recorded |= await self._record(s, frm, fix, at, link)
            await s.commit()
        if recorded:
            hub.publish("tracking-updated")

    async def _node(self, s: Any, num: int, simulated: bool) -> MeshNode:
        node = await s.get(MeshNode, num)
        if node is None:
            node = MeshNode(num=num, node_id=P.node_id(num), simulated=simulated)
            s.add(node)
        node.updated_at = utcnow()
        return node

    @staticmethod
    def _apply_user(node: MeshNode, user: dict[str, Any]) -> None:
        if user.get("long_name"):
            node.long_name = str(user["long_name"])[:40]
        if user.get("short_name"):
            node.short_name = str(user["short_name"])[:8]
        if user.get("hw_model") is not None:
            node.hw_model = int(user["hw_model"])
        if "role" in user or user.get("long_name"):
            node.role = P.ROLES.get(int(user.get("role") or 0), str(user.get("role")))
        pk = user.get("public_key")
        if isinstance(pk, bytes) and len(pk) == 32:
            node.public_key = pk.hex()

    @staticmethod
    def _apply_metrics(node: MeshNode, dm: dict[str, Any]) -> None:
        if dm.get("battery_level"):
            node.battery_level = int(dm["battery_level"])
        if dm.get("voltage"):
            node.voltage = round(float(dm["voltage"]), 3)

    @staticmethod
    def _apply_position(node: MeshNode, fix: dict[str, Any], at: datetime) -> None:
        if node.position_at is None or at >= node.position_at:
            node.lat, node.lon, node.alt_m, node.position_at = fix["lat"], fix["lon"], fix["alt_m"], at

    async def _tracked(self, s: Any, num: int) -> TrackedDevice | None:
        if not (await app_settings.get_tracking_config(s)).enabled:
            return None
        return (
            await s.execute(select(TrackedDevice).where(TrackedDevice.mesh_node_num == num).with_for_update())
        ).scalar_one_or_none()

    async def _last_position(self, s: Any, device_id: Any) -> TrackPosition | None:
        return (
            await s.execute(
                select(TrackPosition)
                .where(TrackPosition.device_id == device_id)
                .order_by(TrackPosition.at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def _record(
        self,
        s: Any,
        num: int,
        fix: dict[str, Any],
        at: datetime,
        link: dict[str, Any] | None,
        only_newer: bool = False,
    ) -> bool:
        d = await self._tracked(s, num)
        if d is None:
            return False
        last = await self._last_position(s, d.id)
        if last is not None:
            if at <= last.at:
                return False  # already recorded (e.g. the node list after a reconnect)
            gap = (at - last.at).total_seconds()
            if 10 <= gap <= 1800 and not only_newer:
                # How often it broadcasts, roughly: for fading a stale marker.
                d.interval_s = max(15, round(d.interval_s * 0.6 + gap * 0.4))
        elif only_newer and at < utcnow() - timedelta(hours=1):
            return False
        env = self._pending_env.pop(num, {})
        s.add(
            TrackPosition(
                device_id=d.id,
                at=at,
                source="meshtastic",
                battery_v=d.battery_v,
                **fix,
                **(link or {}),
                **env,
            )
        )
        d.status, d.status_detail = "ok", None
        d.last_reply_at = utcnow()
        d.failures = 0
        return True

    async def _battery(self, s: Any, num: int, dm: dict[str, Any]) -> bool:
        d = await self._tracked(s, num)
        if d is None:
            return False
        level, volts = dm.get("battery_level"), dm.get("voltage")
        if level:
            d.battery_reported_pct = int(level)
        if volts and 2.5 <= float(volts) <= 5.0:
            d.battery_v = round(float(volts), 2)
            d.battery_max_v = max(d.battery_v, d.battery_max_v or 0.0)
        return bool(level or volts)

    async def _environment(self, s: Any, num: int, env: dict[str, Any]) -> bool:
        readings: dict[str, float] = {}
        if env.get("temperature") is not None:
            readings["temp_c"] = round(float(env["temperature"]), 2)
        if env.get("barometric_pressure"):
            readings["pressure_hpa"] = round(float(env["barometric_pressure"]), 2)
        if not readings:
            return False
        d = await self._tracked(s, num)
        if d is None:
            return False
        last = await self._last_position(s, d.id)
        if last is not None and utcnow() - last.at <= ENV_WITH_POSITION:
            for k, v in readings.items():
                setattr(last, k, v)
            return True
        self._pending_env[num] = readings  # kept with its next position
        return False

    # ---- asking and settings ---------------------------------------------------------

    def asking(self, num: int) -> bool:
        deadline = self._asking.get(num)
        return deadline is not None and time.monotonic() < deadline

    async def ask_position(self, num: int) -> None:
        if self.adapter is None or not self.connected:
            raise MeshtasticError("MeshHome isn't connected to its Meshtastic radio")
        await self.adapter.request_position(num)
        self._asking[num] = time.monotonic() + ASK_TIMEOUT
        hub.publish("tracking-updated")
        t = asyncio.create_task(self._ask_timeout(num), name="meshtastic-ask")
        self._timers.add(t)
        t.add_done_callback(self._timers.discard)

    async def _ask_timeout(self, num: int) -> None:
        await asyncio.sleep(ASK_TIMEOUT)
        if self._asking.pop(num, None) is None:
            return  # it answered
        async with db.session_factory()() as s:
            d = await self._tracked(s, num)
            if d is not None:
                d.status, d.status_detail = "no_answer", NO_ANSWER
                await s.commit()
        hub.publish("tracking-updated")

    def base_position(self) -> dict[str, Any] | None:
        """The radio's own position (a fixed position, or its GPS), for the map's base marker."""
        if self.adapter is None:
            return None
        return P.position_fix(self.adapter.nodes.get(self.adapter.my_num, {}).get("position") or {})


source = MeshtasticSource()
