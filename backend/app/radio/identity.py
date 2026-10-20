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


_L = 2**252 + 27742317777372353535851937790883648493  # the Ed25519 group order


def _encode(point) -> bytes:
    out = bytearray(int(point.y).to_bytes(32, "little"))
    out[31] |= (int(point.x) & 1) << 7
    return bytes(out)


def sign(private_key: bytes, message: bytes) -> bytes:
    """An Ed25519 signature made with a firmware private key (RFC 8032 with the key already
    expanded, as MeshCore's ed25519_sign does). Used by the simulated radio."""
    if len(private_key) != PRIVATE_KEY_BYTES:
        raise ValueError("A MeshCore private key is 64 bytes")
    a = int.from_bytes(private_key[:32], "little")
    g = ECC._curves["ed25519"].G
    pub = _encode(g * a)
    r = int.from_bytes(hashlib.sha512(private_key[32:] + message).digest(), "little") % _L
    big_r = _encode(g * r)
    k = int.from_bytes(hashlib.sha512(big_r + pub + message).digest(), "little") % _L
    return big_r + ((r + k * a) % _L).to_bytes(32, "little")
