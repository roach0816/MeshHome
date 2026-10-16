"""Simulated repeaters / room servers for remote administration without hardware.

Answers like the MeshCore repeater firmware does (docs/cli_commands.md, CommonCLI.cpp): text
replies to CLI commands, and decoded STATUS / TELEMETRY / ACL / NEIGHBOURS / OWNER / REGIONS
responses. The reply texts are close to, but not guaranteed to match, the real firmware's.
State lives for the process lifetime, like the simulated companion's settings.
"""

from __future__ import annotations

import time
from typing import Any

SIM_ADMIN_PASSWORD = "password"  # MeshCore's factory default admin password
SIM_GUEST_PASSWORD = "hello"
SIM_FIRMWARE = "v1.17.1-sim (Build: simulated)"

_REPEATERS: dict[str, SimRepeater] = {}


def repeater(public_key: str, name: str, lat: float | None, lon: float | None) -> SimRepeater:
    r = _REPEATERS.get(public_key)
    if r is None:
        r = _REPEATERS[public_key] = SimRepeater(public_key, name, lat, lon)
    return r


class SimRepeater:
    def __init__(self, public_key: str, name: str, lat: float | None, lon: float | None):
        self.public_key = public_key
        self.started = time.time() - 3 * 86400 - 7380
        self.settings: dict[str, str] = {
            "name": name,
            "radio": "910.525,62.5,7,5",
            "tx": "22",
            "lat": f"{lat or 0:.6f}",
            "lon": f"{lon or 0:.6f}",
            "owner.info": "Simulated node|Not a real installation",
            "guest.password": SIM_GUEST_PASSWORD,
            "advert.interval": "120",
            "flood.advert.interval": "12",
            "repeat": "on",
            "path.hash.mode": "0",
            "flood.max": "64",
            "flood.max.unscoped": "64",
            "flood.max.advert": "64",
            "loop.detect": "minimal",
            "txdelay": "0.5",
            "direct.txdelay": "0.3",
            "af": "1.0",
            "dutycycle": "100",
            "multi.acks": "0",
            "int.thresh": "0",
        }
        self.admin_password = SIM_ADMIN_PASSWORD
        self.acl: dict[str, int] = {}
        self.regions: dict[str, dict[str, Any]] = {"*": {"parent": None, "flood": True}}
        self.home: str | None = None
        self.default: str | None = None
        self.sessions: dict[str, bool] = {}  # requester key -> is admin

    # ---- authentication ----------------------------------------------------------------

    def login(self, requester: str, password: str) -> dict[str, Any]:
        if password == self.admin_password:
            self.sessions[requester] = True
            self.acl[requester[:12]] = 3
            return {"ok": True, "admin": True, "permissions": 3}
        if password == self.settings["guest.password"] and password:
            self.sessions[requester] = False
            return {"ok": True, "admin": False, "permissions": 0}
        if not password and requester[:12] in self.acl:
            # As the firmware does: a node on the access list logs in with a blank password and
            # gets its listed rights.
            perm = self.acl[requester[:12]]
            self.sessions[requester] = perm == 3
            return {"ok": True, "admin": perm == 3, "permissions": perm}
        return {"ok": False, "admin": False, "permissions": None}

    def logged_in(self, requester: str) -> bool:
        return requester in self.sessions

    def is_admin(self, requester: str) -> bool:
        return bool(self.sessions.get(requester))

    # ---- binary / anonymous requests -----------------------------------------------------

    def status(self) -> dict[str, Any]:
        up = int(time.time() - self.started)
        return {
            "bat": 4087,
            "tx_queue_len": 0,
            "noise_floor": -112,
            "last_rssi": -78,
            "nb_recv": 18342 + up // 40,
            "nb_sent": 9211 + up // 90,
            "airtime": 4123 + up // 300,
            "uptime": up,
            "sent_flood": 6120,
            "sent_direct": 3091,
            "recv_flood": 14210,
            "recv_direct": 4132,
            "full_evts": 0,
            "last_snr": 7.25,
            "direct_dups": 41,
            "flood_dups": 2210,
            "rx_airtime": 20877 + up // 120,
            "recv_errors": 12,
        }

    def telemetry(self) -> dict[str, Any]:
        return {
            "lpp": [
                {"channel": 1, "type": "voltage", "value": 4.09},
                {"channel": 1, "type": "temperature", "value": 21.4},
                {
                    "channel": 2,
                    "type": "gps",
                    "value": {
                        "latitude": float(self.settings["lat"]),
                        "longitude": float(self.settings["lon"]),
                        "altitude": 1655.0,
                    },
                },
            ]
        }

    def acl_list(self) -> dict[str, Any]:
        return {"acl": [{"key": k, "perm": v} for k, v in self.acl.items()]}

    def neighbours(self, others: list[str]) -> dict[str, Any]:
        items = [
            {"pubkey": k[:8], "secs_ago": 60 * (i * 17 + 3), "snr": round(9.5 - i * 3.25, 2)}
            for i, k in enumerate(o for o in others if o != self.public_key)
        ]
        return {"neighbours_count": len(items), "results_count": len(items), "neighbours": items}

    def owner(self) -> dict[str, Any]:
        return {"text": f"{self.settings['name']}\n{self.settings['owner.info'].replace('|', chr(10))}"}

    def region_names(self) -> dict[str, Any]:
        return {"text": ",".join(r for r in self.regions if r != "*")}

    # ---- CLI -------------------------------------------------------------------------

    def cli(self, cmd: str) -> str | None:
        """Reply text for one command, or None when the firmware stays silent (reboot)."""
        cmd = cmd.strip()
        word, _, rest = cmd.partition(" ")
        rest = rest.strip()
        if word == "get":
            if rest == "acl":
                return "\n".join(f"{k} {v}" for k, v in self.acl.items()) or "(empty)"
            if rest in self.settings:
                return f"> {self.settings[rest]}"
            if rest == "public.key":
                return f"> {self.public_key}"
            return "??: " + rest
        if word == "set":
            key, _, value = rest.partition(" ")
            value = value.strip()
            if key == "prv.key":
                if len(value) != 128 or any(c not in "0123456789abcdefABCDEF" for c in value):
                    return "Error: invalid key"
                return "OK, reboot to apply! New pubkey: (simulated)"
            if key not in self.settings or not value:
                return "unknown config: " + key
            if key == "radio":
                parts = value.split(",")
                if len(parts) != 4:
                    return "Error: expected freq,bw,sf,cr"
                self.settings["radio"] = value
                return "OK - reboot to apply"
            self.settings[key] = value
            return "OK"
        if word == "password":
            if not rest:
                return "Error: password required"
            self.admin_password = rest
            return f"password now: {rest}"
        if word == "setperm":
            key, _, perm = rest.partition(" ")
            if not key:
                return "Err - bad params"
            if perm.strip() == "":
                self.acl.pop(key[:12].lower(), None)
                return "OK - permissions removed"
            if perm.strip() not in ("0", "1", "2", "3"):
                return "Err - bad params"
            self.acl[key[:12].lower()] = int(perm)
            return "OK"
        if word in ("advert", "advert.zerohop"):
            return "OK - Advert sent"
        if word == "clock":
            if rest == "sync":
                return "OK - clock set: " + time.strftime("%H:%M - %d/%m/%Y UTC", time.gmtime())
            return time.strftime("%H:%M - %d/%m/%Y UTC", time.gmtime())
        if word == "time" and rest.isdigit():
            return "OK - clock set"
        if word == "ver":
            return SIM_FIRMWARE
        if word == "board":
            return "Simulated LoRa board"
        if word == "reboot":
            self.sessions.clear()
            return None
        if word == "neighbors":
            return "(use the Neighbours request)"
        if word == "region":
            return self._region(rest)
        return "Unknown command"

    def _region(self, rest: str) -> str:
        sub, _, arg = rest.partition(" ")
        args = arg.split()
        if not sub:
            lines = []
            for name, r in self.regions.items():
                flags = ("F" if r["flood"] else "-") + (" home" if name == self.home else "")
                lines.append(f"{name}{' (' + r['parent'] + ')' if r['parent'] else ''} {flags}")
            return "\n".join(lines)
        if sub == "put" and args:
            self.regions[args[0]] = {"parent": args[1] if len(args) > 1 else None, "flood": True}
            return "OK"
        if sub == "remove" and args:
            if args[0] == "*" or args[0] not in self.regions:
                return "Err - not found"
            del self.regions[args[0]]
            return "OK"
        if sub in ("allowf", "denyf") and args:
            if args[0] not in self.regions:
                return "Err - not found"
            self.regions[args[0]]["flood"] = sub == "allowf"
            return "OK"
        if sub == "home":
            if args:
                self.home = args[0]
                return "OK"
            return f"> {self.home or '(none)'}"
        if sub == "default":
            if args:
                self.default = None if args[0] == "<null>" else args[0]
                return "OK"
            return f"> {self.default or '(none)'}"
        if sub == "save":
            return "OK - saved"
        if sub == "list":
            want = args[0] if args else "allowed"
            return ",".join(n for n, r in self.regions.items() if r["flood"] == (want == "allowed"))
        return "Unknown command"
