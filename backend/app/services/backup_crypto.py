"""Passphrase encryption for backup files.

File layout:

    b"MHBACKUP1\\n"   (b"MCHBACKUP1\\n" in files made before the rename to MeshHome)
    header line: JSON {"kdf": "scrypt", "n", "r", "p", "salt", "nonce", "chunk"} + "\\n"
    chunks:      [4-byte big-endian length][AES-256-GCM ciphertext + 16-byte tag] ...

The key comes from the passphrase with scrypt (deliberately slow, so guessing passphrases is
expensive). The plaintext is cut into chunks of ``chunk`` bytes, each sealed with its own nonce
(random 8-byte prefix + chunk number) and authenticated together with the header and a
"last chunk" flag, so chunks cannot be reordered, swapped between files, or cut off unnoticed.
Nothing but the KDF parameters is readable without the passphrase.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
from typing import BinaryIO

from Crypto.Cipher import AES

MAGIC = b"MHBACKUP1\n"
LEGACY_MAGIC = b"MCHBACKUP1\n"
CHUNK = 1024 * 1024
TAG = 16
SCRYPT = {"n": 2**15, "r": 8, "p": 1}
MIN_PASSPHRASE = 10
MAX_HEADER = 4096


class BackupError(Exception):
    """The backup cannot be read; the message is safe to show."""


def _key(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(passphrase.encode(), salt=salt, n=n, r=r, p=p, maxmem=256 * 1024 * 1024, dklen=32)


def _nonce(prefix: bytes, index: int) -> bytes:
    return prefix + struct.pack(">I", index)


def encrypt(src: BinaryIO, dst: BinaryIO, passphrase: str) -> None:
    if len(passphrase) < MIN_PASSPHRASE:
        raise BackupError(f"Use a passphrase of at least {MIN_PASSPHRASE} characters.")
    salt, prefix = os.urandom(16), os.urandom(8)
    header = json.dumps(
        {
            "kdf": "scrypt",
            **SCRYPT,
            "salt": base64.b64encode(salt).decode(),
            "nonce": base64.b64encode(prefix).decode(),
            "chunk": CHUNK,
        },
        separators=(",", ":"),
    ).encode()
    key = _key(passphrase, salt, **SCRYPT)
    dst.write(MAGIC + header + b"\n")
    index = 0
    block = src.read(CHUNK)
    while True:
        nxt = src.read(CHUNK)
        last = not nxt
        cipher = AES.new(key, AES.MODE_GCM, nonce=_nonce(prefix, index))
        cipher.update(header + (b"\x01" if last else b"\x00"))
        ct, tag = cipher.encrypt_and_digest(block)
        dst.write(struct.pack(">I", len(ct) + TAG) + ct + tag)
        if last:
            return
        block, index = nxt, index + 1


def decrypt(src: BinaryIO, dst: BinaryIO, passphrase: str) -> None:
    if src.readline(len(LEGACY_MAGIC)) not in (MAGIC, LEGACY_MAGIC):
        raise BackupError("This is not a MeshHome backup file.")
    header = src.readline(MAX_HEADER).rstrip(b"\n")
    try:
        h = json.loads(header)
        if h.get("kdf") != "scrypt":
            raise ValueError
        n, r, p = int(h["n"]), int(h["r"]), int(h["p"])
        if not (2**10 <= n <= 2**20 and 1 <= r <= 32 and 1 <= p <= 16):
            raise ValueError
        salt, prefix = base64.b64decode(h["salt"]), base64.b64decode(h["nonce"])
        if len(prefix) != 8:
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise BackupError("The backup file's header is damaged.") from None
    key = _key(passphrase, salt, n, r, p)
    index = 0
    while True:
        raw_len = src.read(4)
        if len(raw_len) < 4:
            raise BackupError("The backup file is incomplete (it may have been cut off while copying).")
        (size,) = struct.unpack(">I", raw_len)
        if size < TAG or size > CHUNK + TAG:
            raise BackupError("The backup file is damaged.")
        sealed = src.read(size)
        if len(sealed) < size:
            raise BackupError("The backup file is incomplete (it may have been cut off while copying).")
        ct, tag = sealed[:-TAG], sealed[-TAG:]
        last = src.peek(1)[:1] == b"" if hasattr(src, "peek") else _at_end(src)
        cipher = AES.new(key, AES.MODE_GCM, nonce=_nonce(prefix, index))
        cipher.update(header + (b"\x01" if last else b"\x00"))
        try:
            plain = cipher.decrypt_and_verify(ct, tag)
        except ValueError:
            if index == 0:
                raise BackupError("Wrong passphrase, or the file is not a valid backup.") from None
            raise BackupError("The backup file is damaged or was changed.") from None
        dst.write(plain)
        if last:
            return
        index += 1


def _at_end(f: BinaryIO) -> bool:
    pos = f.tell()
    more = f.read(1)
    f.seek(pos)
    return not more
