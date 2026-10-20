"""Signed channel messages: a signature carried after the text of a channel message.

MeshOS (companion app 1.0.26+) signs channel messages with the sender's key and puts the signature
inside the encrypted payload, after the text's terminating zero byte, so other MeshCore apps and
radios show the text as usual. The decrypted GRP_TXT plaintext is then:

    [timestamp 4, LE][flags 1]["<name>: <text>"][0x00]
    [0x53 0x01][signer key id 8][Ed25519 signature 64][zero padding up to the 16-byte block]

(0x53 0x01 is "S" + version 1; the key id is taken to be the first 8 bytes of the signer's public
key.) The layout is as observed on air (October 2026); see the MeshCore UK blog post "Signed
messages". The radio's text frame stops at the zero byte, so MeshHome reads the signature by
decrypting the packet it heard (radio/packets.py decrypt_group).
"""

from __future__ import annotations

from dataclasses import dataclass

MARKER = b"\x53\x01"
KEY_ID_SIZE = 8
SIG_SIZE = 64
ENDING_SIZE = len(MARKER) + KEY_ID_SIZE + SIG_SIZE  # 74, after the text's zero byte


@dataclass(frozen=True)
class SignedEnding:
    timestamp: int
    flags: int
    text: bytes  # "<name>: <text>" as sent, UTF-8
    key_id: bytes
    signature: bytes


def parse(plaintext: bytes) -> SignedEnding | None:
    """The signature of a decrypted channel message, or None if it carries none."""
    if len(plaintext) < 5:
        return None
    end = plaintext.find(b"\0", 5)
    if end < 0:
        return None
    ext = end + 1
    tail = plaintext[ext + ENDING_SIZE :]
    if plaintext[ext : ext + len(MARKER)] != MARKER or len(plaintext) < ext + ENDING_SIZE:
        return None
    if len(tail) > 15 or any(tail):  # only the block's zero padding may follow
        return None
    k = ext + len(MARKER)
    return SignedEnding(
        timestamp=int.from_bytes(plaintext[:4], "little"),
        flags=plaintext[4],
        text=plaintext[5:end],
        key_id=plaintext[k : k + KEY_ID_SIZE],
        signature=plaintext[k + KEY_ID_SIZE : k + KEY_ID_SIZE + SIG_SIZE],
    )
