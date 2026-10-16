"""MeshCore identity keys.

The firmware stores a node's private key as the 64-byte *expanded* Ed25519 key: SHA-512 of the
32-byte seed, with the first half clamped. Its first half is the secret scalar, so the public key
can be computed from it, which lets a backup check that an exported key belongs to the radio.
"""

from __future__ import annotations

import hashlib

from Crypto.PublicKey import ECC

PRIVATE_KEY_BYTES = 64


def expand_seed(seed: bytes) -> bytes:
    """The firmware's private key for a 32-byte Ed25519 seed."""
    h = bytearray(hashlib.sha512(seed).digest())
    h[0] &= 248
    h[31] &= 63
    h[31] |= 64
    return bytes(h)


def public_key(private_key: bytes) -> str:
    """The public key (64 hex characters) for a firmware private key. ValueError if malformed."""
    if len(private_key) != PRIVATE_KEY_BYTES:
        raise ValueError("A MeshCore private key is 64 bytes")
    scalar = int.from_bytes(private_key[:32], "little")
    if scalar == 0:
        raise ValueError("Not a valid MeshCore private key")
    point = ECC._curves["ed25519"].G * scalar
    out = bytearray(int(point.y).to_bytes(32, "little"))
    out[31] |= (int(point.x) & 1) << 7
    return out.hex()
