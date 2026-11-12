"""A simulated Meshtastic node, to rehearse tracking without hardware (and for the tests).

It speaks the same protobuf messages as meshtasticd over the API, so the real connection code
runs against it. Its mesh: this node ("MeshHome base"), a router at the car, and two trackers
running the simulated course (the first visible course, as for the simulated MeshCore tracker),
one a few minutes behind the other. The trackers broadcast their position every 30 seconds and
their battery and environment readings every 2 minutes, heard directly or through the car.
Settings changed through admin messages are kept until the app restarts; a change of region
"reboots" the node, which closes the connection as meshtasticd does.
"""

from __future__ import annotations

import asyncio
import math
import os
import random
import time
from typing import Any

from app.meshtastic import proto as P
from app.meshtastic.client import Link

BASE_NUM = 0x4D480001
CAR_NUM = 0x0CA12A0C  # its low byte names it as a relay (0 would mean "unknown")
TRACKERS = ((0xE1A20001, "X1 Tracker 1", "X1A", 0.0), (0xE1A20002, "X1 Tracker 2", "X1B", 420.0))
POSITION_EVERY = 30.0
TELEMETRY_EVERY = 120.0

# Kept across reconnects (like the node's flash), until the app restarts.
_state: dict[str, Any] = {}


def _defaults() -> dict[str, Any]:
    return {
        "lora": {
            "use_preset": True,
            "modem_preset": 0,
            "region": 1,
            "hop_limit": 3,
            "tx_enabled": True,
            "tx_power": 22,
        },
        "channels": {
            0: {
                "index": 0,
                "role": P.PRIMARY,
                "settings": {"psk": P.DEFAULT_PSK, "module_settings": {"position_precision": 13}},
            }
        },
        "owner": {"id": P.node_id(BASE_NUM), "long_name": "MeshHome base", "short_name": "MHB"},
    }


def reset() -> None:
    _state.clear()


def _user(num: int, long: str, short: str, role: int) -> dict[str, Any]:
    return {"id": P.node_id(num), "long_name": long, "short_name": short, "hw_model": 0, "role": role}


class SimLink(Link):
    def __init__(self, *, fast: bool = False) -> None:
        if not _state:
            _state.update(_defaults())
        self._q: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._rng = random.Random()
        self._open = False
        # Tests: broadcast at once and quickly.
        self._fast = fast or bool(os.environ.get("MESHHOME_SIM_MESHTASTIC_FAST"))
        self._edit: list[dict[str, Any]] = []
        self._editing = False
        self._passkey = os.urandom(8)

    async def open(self) -> None:
        self._open = True

    async def read(self) -> bytes:
        item = await self._q.get()
        if item is None:
            self._open = False
            raise ConnectionError("the simulated node restarted")
        return item

    async def close(self) -> None:
        self._open = False
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()

    def _emit(self, msg: dict[str, Any]) -> None:
        if self._open:
            self._q.put_nowait(P.encode(msg, P.FROM_RADIO))

    # ---- what it's asked -----------------------------------------------------------------

    async def write(self, payload: bytes) -> None:
        if not self._open:
            raise ConnectionError("not connected")
        msg = P.decode(payload, P.TO_RADIO)
        if "want_config_id" in msg:
            self._send_config(int(msg["want_config_id"]))
            if not self._tasks:
                self._tasks.append(asyncio.create_task(self._broadcasts(), name="sim-meshtastic"))
        elif "packet" in msg:
            self._on_packet(msg["packet"])

    def _send_config(self, nonce: int) -> None:
        self._emit({"my_info": {"my_node_num": BASE_NUM, "pio_env": "native", "nodedb_count": 4}})
        now = int(time.time())
        self._emit(
            {"node_info": {"num": BASE_NUM, "user": {**_state["owner"], "hw_model": 37}, "is_favorite": True}}
        )
        self._emit(
            {
                "node_info": {
                    "num": CAR_NUM,
                    "user": _user(CAR_NUM, "Car Router", "CAR", 2),
                    "last_heard": now - 40,
                    "snr": 7.25,
                    "hops_away": 0,
                    "device_metrics": {"battery_level": 101, "voltage": 4.9},
                }
            }
        )
        for num, long, short, _ in TRACKERS:
            self._emit(
                {
                    "node_info": {
                        "num": num,
                        "user": _user(num, long, short, 5),
                        "last_heard": now - 300,
                        "hops_away": 1,
                    }
                }
            )
        self._emit({"metadata": {"firmware_version": "2.7.26.sim", "hw_model": 37, "hasPKC": True}})
        for i in range(8):
            ch = _state["channels"].get(i) or {"index": i, "role": P.DISABLED, "settings": {}}
            self._emit({"channel": ch})
        self._emit({"config": {"device": {"role": 0}}})
        self._emit({"config": {"lora": _state["lora"]}})
        self._emit({"config_complete_id": nonce})

    def _reply(self, to_pid: int, portnum: int, payload: bytes) -> None:
        self._emit(
            {
                "packet": {
                    "from": BASE_NUM,
                    "to": BASE_NUM,
                    "id": self._rng.randint(1, 0xFFFFFFFF),
                    "decoded": {"portnum": portnum, "payload": payload, "request_id": to_pid},
                }
            }
        )

    def _ack(self, pid: int, error: int = 0) -> None:
        self._reply(
            pid,
            P.ROUTING_APP,
            P.encode({"error_reason": error}, {3: ("error_reason", "enum")}) if error else b"",
        )

    def _on_packet(self, pkt: dict[str, Any]) -> None:
        dec = pkt.get("decoded") or {}
        pid = int(pkt.get("id") or 0)
        to = int(pkt.get("to") or 0)
        if dec.get("portnum") == P.ADMIN_APP and to == BASE_NUM:
            self._on_admin(pid, P.decode(dec.get("payload") or b"", P.ADMIN))
        elif dec.get("portnum") == P.POSITION_APP and dec.get("want_response"):
            tracker = next((t for t in TRACKERS if t[0] == to), None)
            if tracker is not None:
                asyncio.get_running_loop().call_later(
                    0.2 if self._fast else self._rng.uniform(1.5, 4.0), self._position, tracker, pid
                )

    def _on_admin(self, pid: int, msg: dict[str, Any]) -> None:
        if msg.get("get_config_request") == P.SESSIONKEY_CONFIG_TYPE:
            self._ack(pid)
            self._reply(
                pid,
                P.ADMIN_APP,
                P.encode({"get_config_response": {}, "session_passkey": self._passkey}, P.ADMIN),
            )
            return
        if msg.get("session_passkey") != self._passkey:
            self._ack(pid, 36)
            return
        self._ack(pid)
        if msg.get("begin_edit_settings"):
            self._editing, self._edit = True, []
        elif msg.get("commit_edit_settings"):
            changes, self._editing, self._edit = self._edit, False, []
            self._apply(changes)
        elif self._editing:
            self._edit.append(msg)
        else:
            self._apply([msg])

    def _apply(self, changes: list[dict[str, Any]]) -> None:
        reboot = False
        for m in changes:
            if "set_channel" in m:
                ch = {k: v for k, v in m["set_channel"].items() if k != "_raw"}
                _state["channels"][int(ch.get("index") or 0)] = _clean(ch)
            if "set_config" in m and "lora" in m["set_config"]:
                lora = _clean(m["set_config"]["lora"])
                reboot = reboot or lora.get("region") != _state["lora"].get("region")
                reboot = reboot or lora.get("modem_preset") != _state["lora"].get("modem_preset")
                _state["lora"] = lora
            if "set_owner" in m:
                _state["owner"] = {**_state["owner"], **_clean(m["set_owner"])}
        if reboot:
            # Like meshtasticd: it restarts to apply LoRa changes, closing the connection.
            asyncio.get_running_loop().call_later(0.3 if self._fast else 2.0, self._q.put_nowait, None)

    # ---- the mesh ------------------------------------------------------------------------

    def _shares_channel(self) -> bool:
        """The trackers are on the team channel set up from MeshHome (any private primary
        channel). On the default public channel their positions arrive blurred."""
        return _state["channels"][0]["settings"].get("psk", P.DEFAULT_PSK) != P.DEFAULT_PSK

    def _packet(self, frm: int, portnum: int, payload: bytes, *, request_id: int | None = None) -> None:
        via_car = self._rng.random() < 0.6
        hop_start = int(_state["lora"].get("hop_limit") or 3)
        data: dict[str, Any] = {"portnum": portnum, "payload": payload}
        if request_id:
            data["request_id"] = request_id
        self._emit(
            {
                "packet": {
                    "from": frm,
                    "to": BASE_NUM if request_id else P.BROADCAST,
                    "id": self._rng.randint(1, 0xFFFFFFFF),
                    "channel": 0,
                    "rx_time": int(time.time()),
                    "rx_snr": round(self._rng.uniform(-6, 9) * 4) / 4,
                    "rx_rssi": self._rng.randint(-118, -72),
                    "hop_start": hop_start,
                    "hop_limit": hop_start - (1 if via_car else 0),
                    "relay_node": (CAR_NUM if via_car else frm) & 0xFF,
                    "decoded": data,
                }
            }
        )

    def _position(self, tracker: tuple[int, str, str, float], request_id: int | None = None) -> None:
        from app.radio.simulated import sim_tracker_position

        num, _, _, behind = tracker
        lat, lon = sim_tracker_position(time.time() - behind)
        bits = 32 if self._shares_channel() else 13
        if bits < 32:  # what Meshtastic sends on a public channel: about 1.5 km off
            lat, lon = round(lat, 2), round(lon, 2)
        t = time.time()
        pos = {
            "latitude_i": int(lat * 1e7),
            "longitude_i": int(lon * 1e7),
            "altitude": int(512 + 18 * math.sin(t / 540)),
            "time": int(t),
            "location_source": 2,
            "sats_in_view": self._rng.randint(9, 16),
            "precision_bits": bits,
        }
        self._packet(num, P.POSITION_APP, P.encode(pos, P.POSITION), request_id=request_id)

    def _telemetry(self, tracker: tuple[int, str, str, float], n: int) -> None:
        num = tracker[0]
        volts = max(3.62, 4.33 - 0.004 * n - (0.03 if num == TRACKERS[1][0] else 0))
        from app.services.tracking import battery_percent

        level = battery_percent(volts, 4.33) or 0
        dev = {
            "device_metrics": {
                "battery_level": level,
                "voltage": round(volts, 3),
                "uptime_seconds": int(n * TELEMETRY_EVERY),
            }
        }
        self._packet(num, P.TELEMETRY_APP, P.encode({"time": int(time.time()), **dev}, P.TELEMETRY))
        env = {
            "environment_metrics": {
                "temperature": round(27.5 + self._rng.uniform(-0.6, 0.6), 1),
                "barometric_pressure": round(955.0 + self._rng.uniform(-0.5, 0.5), 1),
                "relative_humidity": round(self._rng.uniform(40, 60), 1),
            }
        }
        self._packet(num, P.TELEMETRY_APP, P.encode({"time": int(time.time()), **env}, P.TELEMETRY))

    async def _broadcasts(self) -> None:
        n = 0
        last_tel = -1e9
        step = 0.5 if self._fast else POSITION_EVERY
        if not self._fast:
            await asyncio.sleep(3)
        while True:
            for t in TRACKERS:
                self._position(t)
            if time.monotonic() - last_tel >= (1.0 if self._fast else TELEMETRY_EVERY):
                last_tel = time.monotonic()
                for t in TRACKERS:
                    self._telemetry(t, n)
                n += 1
            await asyncio.sleep(step)


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    return {k: (_clean(v) if isinstance(v, dict) else v) for k, v in d.items() if k != "_raw"}
