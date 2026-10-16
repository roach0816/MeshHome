"""Web push: notifications to browsers and installed web apps, sent by this server itself.

Part of "Install as an app" (HTTPS only; app_settings.AppInstallConfig). Each browser that turns
notifications on gives this server a subscription: its push service's endpoint URL and the keys to
encrypt to. There is no relay and no account with any app store:

- VAPID (RFC 8292): this server signs each request with its own key pair (generated once, kept in
  the database), so the push service knows the requests come from the server the browser subscribed
  to.
- Encryption (RFC 8291, aes128gcm): the notification's sender, text and conversation are encrypted
  to the browser's key, so Google, Mozilla or Apple only see ciphertext, its size and the time.
- The browser's service worker (frontend/public/sw.js) decrypts nothing itself: the browser does,
  and hands the worker the JSON, which it shows as a notification.

Only HTTPS endpoints of the known browser push services are accepted, so the server never posts to
an address a browser makes up.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import struct
import time
import uuid
from typing import Any
from urllib.parse import urlsplit

import httpx
from Crypto.Cipher import AES
from Crypto.Hash import SHA256
from Crypto.Protocol.DH import key_agreement
from Crypto.PublicKey import ECC
from Crypto.Signature import DSS
from sqlalchemy import and_, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.models import Conversation, Message, ReadPosition, Session, WebPushSubscription, utcnow
from app.services import app_settings

log = logging.getLogger(__name__)

# Push services of the browsers people use: Chrome, Edge, Firefox and Safari (macOS and iOS).
PUSH_HOST_SUFFIXES = (
    ".googleapis.com",
    ".push.services.mozilla.com",
    ".push.apple.com",
    ".notify.windows.com",
)
VAPID_SUBJECT = "https://github.com/roach0816/MeshHome"  # contact for push services (RFC 8292 `sub`)
TTL = 24 * 3600  # a phone that is off for longer gets no stale notifications
RECORD_SIZE = 4096
MAX_PLAINTEXT = 3000  # well within the 4096-byte limit after encryption overhead
BODY_LIMIT = 1000  # characters; MeshCore messages are far shorter
QUEUE_SIZE = 500
TIMEOUT = 10.0

_queue: asyncio.Queue[uuid.UUID] | None = None
_task: asyncio.Task | None = None
_key_lock = asyncio.Lock()
transport: httpx.AsyncBaseTransport | None = None  # tests: a fake push service


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def endpoint_allowed(endpoint: str) -> bool:
    try:
        u = urlsplit(endpoint)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return u.scheme == "https" and u.port in (None, 443) and host.endswith(PUSH_HOST_SUFFIXES)


def browser_key(p256dh: str) -> ECC.EccKey:
    """The browser's public key: 65 bytes, an uncompressed P-256 point. ValueError if not."""
    raw = b64url_decode(p256dh)
    if len(raw) != 65 or raw[0] != 4:
        raise ValueError("not an uncompressed P-256 public key")
    key = ECC.import_key(raw, curve_name="P-256")  # ValueError for a point off the curve
    if key.pointQ.is_point_at_infinity():  # (0, 0) is read as the point at infinity
        raise ValueError("not a usable P-256 public key")
    return key


# ---- encryption (RFC 8291) --------------------------------------------------------------------


def _hmac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def encrypt(
    p256dh: str,
    auth: str,
    plaintext: bytes,
    *,
    server_key: ECC.EccKey | None = None,
    salt: bytes | None = None,
) -> bytes:
    """The request body for one push message (aes128gcm: header, then one encrypted record).

    `server_key` and `salt` are random per message; tests pass the RFC's values."""
    ua_public = b64url_decode(p256dh)
    ua_key = browser_key(p256dh)
    auth_secret = b64url_decode(auth)
    as_key = server_key or ECC.generate(curve="P-256")
    as_public = as_key.public_key().export_key(format="raw")
    salt = salt or os.urandom(16)

    ecdh_secret = key_agreement(static_priv=as_key, static_pub=ua_key, kdf=lambda z: z)
    prk_key = _hmac(auth_secret, ecdh_secret)
    ikm = _hmac(prk_key, b"WebPush: info\x00" + ua_public + as_public + b"\x01")
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]

    cipher = AES.new(cek, AES.MODE_GCM, nonce=nonce)
    ct, tag = cipher.encrypt_and_digest(plaintext + b"\x02")  # 0x02: the last (only) record
    header = salt + struct.pack("!IB", RECORD_SIZE, len(as_public)) + as_public
    return header + ct + tag


# ---- VAPID (RFC 8292) --------------------------------------------------------------------------


async def vapid_keys(s: AsyncSession) -> tuple[ECC.EccKey, str]:
    """This server's VAPID key pair, created on first use: (private key, public key for browsers)."""
    async with _key_lock:
        raw = await app_settings.get_web_push_keys(s)
        if raw is None:
            key = ECC.generate(curve="P-256")
            raw = {
                "private_key": b64url(int(key.d).to_bytes(32, "big")),
                "public_key": b64url(key.public_key().export_key(format="raw")),
            }
            await app_settings.put_web_push_keys(s, raw)
            await s.commit()
        d = int.from_bytes(b64url_decode(raw["private_key"]), "big")
        return ECC.construct(curve="P-256", d=d), raw["public_key"]


def vapid_header(key: ECC.EccKey, public_key: str, endpoint: str, now: float | None = None) -> str:
    u = urlsplit(endpoint)
    claims = {
        "aud": f"{u.scheme}://{u.netloc}",
        "exp": int(now or time.time()) + 12 * 3600,
        "sub": VAPID_SUBJECT,
    }
    signing_input = (
        b64url(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
        + "."
        + b64url(json.dumps(claims, separators=(",", ":")).encode())
    )
    signature = DSS.new(key, "fips-186-3", encoding="binary").sign(SHA256.new(signing_input.encode()))
    return f"vapid t={signing_input}.{b64url(signature)}, k={public_key}"


# ---- sending ----------------------------------------------------------------------------------


class Result:
    def __init__(self, ok: bool, gone: bool = False, error: str | None = None) -> None:
        self.ok, self.gone, self.error = ok, gone, error


def topic(sub: WebPushSubscription, conversation_id: uuid.UUID) -> str:
    """Replaces a not-yet-delivered notification for the same conversation, without revealing which."""
    return b64url(_hmac(b64url_decode(sub.auth), str(conversation_id).encode()))[:32]


async def send(
    client: httpx.AsyncClient,
    keys: tuple[ECC.EccKey, str],
    sub: WebPushSubscription,
    content: dict[str, Any],
    *,
    urgency: str = "normal",
    topic_id: str | None = None,
) -> Result:
    plaintext = json.dumps(content, separators=(",", ":"), ensure_ascii=False).encode()
    if len(plaintext) > MAX_PLAINTEXT:
        content = {**content, "b": content.get("b", "")[:200] + "…"}
        plaintext = json.dumps(content, separators=(",", ":"), ensure_ascii=False).encode()
    headers = {
        "Authorization": vapid_header(*keys, sub.endpoint),
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": str(TTL),
        "Urgency": urgency,
    }
    if topic_id:
        headers["Topic"] = topic_id
    try:
        body = encrypt(sub.p256dh, sub.auth, plaintext)
        r = await client.post(sub.endpoint, content=body, headers=headers)
    except ValueError:
        return Result(False, gone=True, error="The browser's push keys are invalid")
    except httpx.HTTPError as exc:
        return Result(False, error=f"Couldn't reach the push service ({type(exc).__name__})")
    if r.status_code in (200, 201, 202):
        return Result(True)
    if r.status_code in (404, 410):
        return Result(False, gone=True, error="The browser is no longer subscribed")
    return Result(False, error=f"The push service answered {r.status_code}: {r.text[:100]}".strip())


async def _record(s: AsyncSession, sub: WebPushSubscription, result: Result) -> None:
    if result.gone:
        await s.execute(delete(WebPushSubscription).where(WebPushSubscription.id == sub.id))
        return
    if result.ok:
        sub.last_sent_at = utcnow()
        sub.last_error = None
    else:
        sub.last_error = (result.error or "failed")[:200]


def wants(sub: WebPushSubscription, conv: Conversation) -> bool:
    # The conversation's own alert setting wins; otherwise the browser's choice by kind.
    if conv.sound == "off":
        return False
    if conv.sound == "on":
        return True
    return sub.channels if conv.kind == "channel" else sub.dms


async def unread_total(s: AsyncSession, user_id: uuid.UUID) -> int:
    q = (
        select(func.count())
        .select_from(Message)
        .outerjoin(
            ReadPosition,
            and_(ReadPosition.conversation_id == Message.conversation_id, ReadPosition.user_id == user_id),
        )
        .where(
            Message.direction == "in",
            Message.position > func.coalesce(ReadPosition.position, 0),
            Message.suppressed.is_(False),
        )
    )
    return int((await s.execute(q)).scalar_one())


async def _live_subscriptions(s: AsyncSession) -> list[tuple[WebPushSubscription, Session]]:
    rows = await s.execute(
        select(WebPushSubscription, Session)
        .join(Session, Session.id == WebPushSubscription.session_id)
        .where(Session.expires_at > utcnow())
    )
    return [(sub, sess) for sub, sess in rows.all()]


async def notify_message(client: httpx.AsyncClient, message_id: uuid.UUID) -> int:
    """Notify every browser that wants this incoming message. Returns how many were sent."""
    async with db.session_factory()() as s:
        if not (await app_settings.get_app_install_config(s)).enabled:
            return 0
        m = await s.get(Message, message_id)
        if m is None or m.direction != "in" or m.suppressed:
            return 0
        conv = await s.get(Conversation, m.conversation_id)
        if conv is None:
            return 0
        targets = [(sub, sess) for sub, sess in await _live_subscriptions(s) if wants(sub, conv)]
        if not targets:
            return 0
        keys = await vapid_keys(s)
        unread: dict[uuid.UUID, int] = {}
        sent = 0
        for sub, sess in targets:
            if sess.user_id not in unread:
                unread[sess.user_id] = await unread_total(s, sess.user_id)
            content = {
                "v": 1,
                "c": str(conv.id),
                "m": str(m.id),
                "k": conv.kind,
                "t": conv.title,
                "s": m.sender_label or "",
                "b": m.body[:BODY_LIMIT],
                "n": unread[sess.user_id],
            }
            urgency = "high" if conv.kind == "dm" else "normal"
            result = await send(client, keys, sub, content, urgency=urgency, topic_id=topic(sub, conv.id))
            await _record(s, sub, result)
            sent += result.ok
        await s.commit()
        return sent


async def send_test(s: AsyncSession, sub: WebPushSubscription) -> Result:
    keys = await vapid_keys(s)
    async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport) as client:
        result = await send(
            client,
            keys,
            sub,
            {"v": 1, "k": "test", "t": "MeshHome", "s": "", "b": "Notifications work on this device."},
        )
    await _record(s, sub, result)
    await s.commit()
    return result


# ---- lifecycle -------------------------------------------------------------------------------


def start() -> None:
    global _queue, _task
    _queue = asyncio.Queue(maxsize=QUEUE_SIZE)
    _task = asyncio.create_task(_worker(), name="web-push")


async def stop() -> None:
    global _task
    if _task:
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        _task = None


def enqueue(message_id: uuid.UUID) -> None:
    """A new incoming message was committed: notify the browsers that want it (never blocks)."""
    if _queue is None:
        return
    try:
        _queue.put_nowait(message_id)
    except asyncio.QueueFull:
        log.warning("web push queue full; a notification was dropped")


async def _worker() -> None:
    assert _queue is not None
    while True:
        message_id = await _queue.get()
        try:
            # A client per message: notifications are rare, and tests can swap `transport` in.
            async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport) as client:
                await notify_message(client, message_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - notifications must never disturb collection
            log.warning("web push failed: %s", type(exc).__name__)
