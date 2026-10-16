"""MeshCore over-the-air packets, as the companion radio logs them (PUSH_CODE_LOG_RX_DATA).

Layout (MeshCore src/Packet.cpp, Packet::readFrom):

    header (1)            bits 0-1 route type, bits 2-5 payload type, bits 6-7 version
    transport codes (4)   only for the TRANSPORT_FLOOD and TRANSPORT_DIRECT route types
    path_len (1)          bits 0-5 number of hops, bits 6-7 hash size - 1
    path (hops x size)    one hash prefix of each repeater's public key, in the order travelled
    payload (rest)        GRP_TXT: channel hash (1), MAC (2), ciphertext
                          TXT_MSG: destination hash (1), source hash (1), MAC (2), ciphertext

Copies of one packet heard through different repeaters share payload type and payload, which is
what MeshCore's packet hash covers (Packet::calculatePacketHash), so ``key`` groups them.
Only headers and routing are read here; nothing is decrypted.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

ROUTE_TRANSPORT_FLOOD, ROUTE_FLOOD, ROUTE_DIRECT, ROUTE_TRANSPORT_DIRECT = 0, 1, 2, 3
TYPE_TXT_MSG = 0x02
TYPE_GRP_TXT = 0x05
TYPE_TRACE = 0x09
MAX_HASH_SIZE = 8


@dataclass(frozen=True)
class Packet:
    route: int
    payload_type: int
    hash_size: int
    hops: tuple[str, ...]  # hex hash prefixes, first repeater first
    payload: bytes
    key: str  # MeshCore packet hash (hex): the same for every copy of this packet

    @property
    def flood(self) -> bool:
        return self.route in (ROUTE_FLOOD, ROUTE_TRANSPORT_FLOOD)


def packet_key(payload_type: int, payload: bytes, path_len: int = 0) -> str:
    h = hashlib.sha256(bytes([payload_type]))
    if payload_type == TYPE_TRACE:
        h.update(bytes([path_len]))
    h.update(payload)
    return h.digest()[:MAX_HASH_SIZE].hex()


def parse(raw: bytes) -> Packet | None:
    """The routing information of a raw packet, or None if it is malformed."""
    if len(raw) < 2:
        return None
    header = raw[0]
    route, ptype = header & 0x03, (header >> 2) & 0x0F
    i = 1
    if route in (ROUTE_TRANSPORT_FLOOD, ROUTE_TRANSPORT_DIRECT):
        i += 4
    if i >= len(raw):
        return None
    path_len = raw[i]
    i += 1
    size, count = (path_len >> 6) + 1, path_len & 0x3F
    if size > 3:  # MeshCore's isValidPathLen: hash sizes are 1-3 bytes
        return None
    end = i + size * count
    if end >= len(raw):
        return None
    hops = tuple(raw[j : j + size].hex() for j in range(i, end, size))
    payload = raw[end:]
    return Packet(route, ptype, size, hops, payload, packet_key(ptype, payload, path_len))


def build(payload_type: int, payload: bytes, hops: list[bytes] = (), route: int = ROUTE_FLOOD) -> bytes:
    """A raw packet (used by the simulated radio and tests)."""
    size = len(hops[0]) if hops else 1
    header = bytes([(payload_type << 2) | route])
    transport = b"\0\0\0\0" if route in (ROUTE_TRANSPORT_FLOOD, ROUTE_TRANSPORT_DIRECT) else b""
    path_len = ((size - 1) << 6) | len(hops)
    return header + transport + bytes([path_len]) + b"".join(hops) + payload


def channel_hash(secret: bytes) -> int:
    """The 1-byte channel identifier sent in the clear: SHA-256 of the key (MeshCore
    BaseChatMesh: 16-byte keys hash their 16 bytes, 32-byte keys all 32)."""
    key = secret[:16] if len(secret) <= 16 or not any(secret[16:32]) else secret[:32]
    return hashlib.sha256(key).digest()[0]
