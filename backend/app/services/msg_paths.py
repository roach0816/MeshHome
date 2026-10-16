"""Which routes a received message travelled, from the radio's raw packet log.

The companion radio reports every packet it hears (with SNR and RSSI) before it drops repeats, and
hands over the decrypted message moments later. So: keep the last few seconds of text packets;
when a message arrives, pick the packet that carried it (same channel, or same sender and us as
the destination) and record its path. Copies of that packet heard later through other repeaters
(same MeshCore packet hash) are added as further paths for a minute.

Matching is by channel or sender plus timing, so two messages on one channel within the same
second could be confused; that is rare, and the paths are labelled as heard by this radio.
Messages the radio stored while MeshHome was not connected have no paths.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from app.radio.packets import TYPE_GRP_TXT, TYPE_TXT_MSG, Packet

BEFORE_SECONDS = 10.0  # how long before the message its packet may have been heard
FOLLOW_SECONDS = 60.0  # how long copies are still attached to the message
MAX_PATHS = 20


@dataclass
class _Heard:
    at: float
    packet: Packet
    snr: float | None
    rssi: float | None


def path_entry(h: _Heard) -> dict[str, Any]:
    return {
        "hops": list(h.packet.hops),
        "hash_size": h.packet.hash_size,
        "route": "flood" if h.packet.flood else "direct",
        "snr": h.snr,
        "rssi": h.rssi,
    }


@dataclass
class PathTracker:
    clock: Any = time.monotonic
    _recent: deque[_Heard] = field(default_factory=lambda: deque(maxlen=200))
    _following: dict[str, tuple[Any, float, int]] = field(
        default_factory=dict
    )  # key -> (message id, until, count)

    def heard(
        self, packet: Packet, snr: float | None, rssi: float | None
    ) -> tuple[Any, dict[str, Any]] | None:
        """Note a packet. If it is a further copy of a known message: (message id, path entry)."""
        if packet.payload_type not in (TYPE_TXT_MSG, TYPE_GRP_TXT):
            return None
        now = self.clock()
        self._expire(now)
        h = _Heard(now, packet, snr, rssi)
        follow = self._following.get(packet.key)
        if follow is not None:
            message_id, until, count = follow
            if count >= MAX_PATHS:
                return None
            self._following[packet.key] = (message_id, until, count + 1)
            return message_id, path_entry(h)
        self._recent.append(h)
        return None

    def claim(
        self, *, channel_hash: int | None = None, sender_hash: int | None = None, self_hash: int | None = None
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Paths for a message that just arrived: (packet key, path entries). Call follow() with
        the key once the message is stored, so later copies are attached to it."""
        now = self.clock()
        self._expire(now)

        def matches(h: _Heard) -> bool:
            p = h.packet
            if now - h.at > BEFORE_SECONDS or len(p.payload) < 2:
                return False
            if channel_hash is not None:
                return p.payload_type == TYPE_GRP_TXT and p.payload[0] == channel_hash
            if sender_hash is not None:
                return (
                    p.payload_type == TYPE_TXT_MSG
                    and p.payload[1] == sender_hash
                    and (self_hash is None or p.payload[0] == self_hash)
                )
            return False

        found = next((h for h in reversed(self._recent) if matches(h)), None)
        if found is None:
            return None, []
        copies = [h for h in self._recent if h.packet.key == found.packet.key]
        for h in copies:
            self._recent.remove(h)
        return found.packet.key, [path_entry(h) for h in copies[:MAX_PATHS]]

    def follow(self, key: str, message_id: Any, count: int) -> None:
        self._following[key] = (message_id, self.clock() + FOLLOW_SECONDS, count)

    def _expire(self, now: float) -> None:
        while self._recent and now - self._recent[0].at > BEFORE_SECONDS * 3:
            self._recent.popleft()
        for key in [k for k, (_, until, _) in self._following.items() if until < now]:
            del self._following[key]
