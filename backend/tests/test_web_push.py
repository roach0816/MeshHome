"""Web push: RFC 8291 encryption, VAPID, subscriptions, and who gets notified.

A fake push service stands in for Google's, Mozilla's and Apple's: it decrypts each notification
with the browser's private key, as a browser does, so the tests see exactly what a phone would.
"""

import asyncio
import hashlib
import hmac
import json
import struct
import time

import httpx
from Crypto.Cipher import AES
from Crypto.Hash import SHA256
from Crypto.Protocol.DH import key_agreement
from Crypto.PublicKey import ECC
from Crypto.Signature import DSS
from sqlalchemy import select, update

from app import db
from app.models import Conversation, Message, WebPushSubscription
from app.radio.base import IncomingMessage
from app.services import web_push
from app.services.web_push import b64url, b64url_decode
from tests.conftest import csrf, do_setup, radio_adapter, wait_for

HTTPS = {"X-Forwarded-Proto": "https"}
ENDPOINT = "https://fcm.googleapis.com/fcm/send/test-endpoint"


# ---- the browser side ----------------------------------------------------------------------------


class Browser:
    """A browser's push keys, and decrypting what it receives (RFC 8291, receiver side)."""

    def __init__(self) -> None:
        self.key = ECC.generate(curve="P-256")
        self.public = self.key.public_key().export_key(format="raw")
        self.auth = b"0123456789abcdef"

    def subscription(self, endpoint: str = ENDPOINT, **prefs) -> dict:
        return {
            "endpoint": endpoint,
            "keys": {"p256dh": b64url(self.public), "auth": b64url(self.auth)},
            **prefs,
        }

    def decrypt(self, body: bytes) -> dict:
        salt, (rs, idlen) = body[:16], struct.unpack("!IB", body[16:21])
        as_public, record = body[21 : 21 + idlen], body[21 + idlen :]
        assert rs == 4096 and len(as_public) == 65
        as_key = ECC.import_key(as_public, curve_name="P-256")
        secret = key_agreement(static_priv=self.key, static_pub=as_key, kdf=lambda z: z)

        def h(k: bytes, d: bytes) -> bytes:
            return hmac.new(k, d, hashlib.sha256).digest()

        ikm = h(h(self.auth, secret), b"WebPush: info\x00" + self.public + as_public + b"\x01")
        prk = h(salt, ikm)
        cek, nonce = (
            h(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16],
            h(prk, b"Content-Encoding: nonce\x00\x01")[:12],
        )
        plain = AES.new(cek, AES.MODE_GCM, nonce=nonce).decrypt_and_verify(record[:-16], record[-16:])
        assert plain.endswith(b"\x02")
        return json.loads(plain[:-1])


class PushService:
    """Records requests; answers `status` (201 = accepted)."""

    def __init__(self, browser: Browser, status: int = 201) -> None:
        self.browser, self.status, self.received, self.headers = browser, status, [], []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.headers.append(request.headers)
        self.received.append(self.browser.decrypt(request.content))
        return httpx.Response(self.status)


# ---- primitives ----------------------------------------------------------------------------------


def test_rfc8291_example():
    """RFC 8291 Appendix A, byte for byte."""
    server = ECC.construct(
        curve="P-256", d=int.from_bytes(b64url_decode("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big")
    )
    body = web_push.encrypt(
        "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
        "BTBZMqHH6r4Tts7J_aSIgg",
        b"When I grow up, I want to be a watermelon",
        server_key=server,
        salt=b64url_decode("DGv6ra1nlYgDCS1FRnbzlw"),
    )
    assert b64url(body) == (
        "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6Tlz"
        "AC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN"
    )


def test_vapid_header_is_a_valid_es256_token():
    key = ECC.generate(curve="P-256")
    public = b64url(key.public_key().export_key(format="raw"))
    header = web_push.vapid_header(key, public, ENDPOINT, now=1_000_000)
    token, k = header.removeprefix("vapid t=").split(", k=")
    assert k == public
    head, claims, sig = token.split(".")
    assert json.loads(b64url_decode(claims)) == {
        "aud": "https://fcm.googleapis.com",
        "exp": 1_000_000 + 12 * 3600,
        "sub": web_push.VAPID_SUBJECT,
    }
    assert json.loads(b64url_decode(head)) == {"typ": "JWT", "alg": "ES256"}
    DSS.new(key.public_key(), "fips-186-3", encoding="binary").verify(
        SHA256.new(f"{head}.{claims}".encode()), b64url_decode(sig)
    )


def test_only_known_push_services():
    ok = [
        "https://fcm.googleapis.com/fcm/send/abc",
        "https://updates.push.services.mozilla.com/wpush/v2/abc",
        "https://web.push.apple.com/QF4x",
        "https://wns2-par02p.notify.windows.com/w/?token=abc",
    ]
    bad = [
        "http://fcm.googleapis.com/x",
        "https://evil.example.com/x",
        "https://googleapis.com.evil.example/x",
        "https://fcm.googleapis.com:8443/x",
        "https://127.0.0.1/x",
        "not a url",
    ]
    assert all(web_push.endpoint_allowed(u) for u in ok)
    assert not any(web_push.endpoint_allowed(u) for u in bad)


# ---- the API -------------------------------------------------------------------------------------


async def _enable_install(client) -> None:
    r = await client.put(
        "/api/settings/app-install", headers={**csrf(client), **HTTPS}, json={"enabled": True}
    )
    assert r.status_code == 200


async def test_subscribing_needs_install_as_an_app_over_https(client):
    await do_setup(client, mode="none")
    b = Browser()
    st = (await client.get("/api/web-push", headers=HTTPS)).json()
    assert st == {"available": False, "public_key": None, "subscription": None, "devices": 0}
    r = await client.put(
        "/api/web-push/subscription", headers={**csrf(client), **HTTPS}, json=b.subscription()
    )
    assert r.status_code == 409

    await _enable_install(client)
    assert (await client.get("/api/web-push")).json()["available"] is False  # plain HTTP
    st = (await client.get("/api/web-push", headers=HTTPS)).json()
    assert st["available"] is True and len(b64url_decode(st["public_key"])) == 65
    # The key pair is made once and kept.
    assert (await client.get("/api/web-push", headers=HTTPS)).json()["public_key"] == st["public_key"]

    put = lambda body: client.put("/api/web-push/subscription", headers={**csrf(client), **HTTPS}, json=body)  # noqa: E731
    assert (await put(b.subscription("https://evil.example.com/x"))).status_code == 422
    bad_key = b.subscription()
    bad_key["keys"]["p256dh"] = b64url(b"\x04" + bytes(64))
    assert (await put(bad_key)).status_code == 422
    r = await put(b.subscription(dms=True, channels=True))
    assert r.status_code == 200 and r.json()["channels"] is True
    # Subscribing again replaces this browser's subscription.
    assert (await put(b.subscription(dms=True, channels=False))).status_code == 200
    st = (await client.get("/api/web-push", headers=HTTPS)).json()
    assert st["subscription"]["endpoint"] == ENDPOINT and st["subscription"]["channels"] is False
    assert st["devices"] == 1

    # API keys can't use any of it.
    key = (
        await client.post("/api/api-keys", headers=csrf(client), json={"name": "x", "scope": "write"})
    ).json()["key"]
    r = await client.get("/api/web-push", headers={"Authorization": f"Bearer {key}", **HTTPS})
    assert r.status_code == 403

    assert (await client.delete("/api/web-push/subscription", headers=csrf(client))).status_code == 204
    assert (await client.get("/api/web-push", headers=HTTPS)).json()["subscription"] is None


async def test_test_notification_and_a_gone_browser(client, monkeypatch):
    await do_setup(client, mode="none")
    await _enable_install(client)
    b = Browser()
    service = PushService(b)
    monkeypatch.setattr(web_push, "transport", httpx.MockTransport(service.handler))
    r = await client.put(
        "/api/web-push/subscription", headers={**csrf(client), **HTTPS}, json=b.subscription()
    )
    assert r.status_code == 200

    r = await client.post("/api/web-push/test", headers={**csrf(client), **HTTPS})
    assert r.json() == {"ok": True, "error": None}
    assert service.received[-1]["k"] == "test"
    h = service.headers[-1]
    assert h["content-encoding"] == "aes128gcm" and h["ttl"] == str(24 * 3600)
    assert h["authorization"].startswith("vapid t=")
    sub = (await client.get("/api/web-push", headers=HTTPS)).json()["subscription"]
    assert sub["last_sent_at"] and sub["last_error"] is None

    service.status = 500
    r = await client.post("/api/web-push/test", headers={**csrf(client), **HTTPS})
    assert r.json()["ok"] is False and "500" in r.json()["error"]

    # The browser unsubscribed or was uninstalled: the push service says 410, and it's forgotten.
    service.status = 410
    await client.post("/api/web-push/test", headers={**csrf(client), **HTTPS})
    assert (await client.get("/api/web-push", headers=HTTPS)).json()["subscription"] is None


async def test_who_is_notified(client, monkeypatch):
    from app.radio import simulated

    await do_setup(client)
    await _enable_install(client)
    b = Browser()
    service = PushService(b)
    monkeypatch.setattr(web_push, "transport", httpx.MockTransport(service.handler))
    r = await client.put(
        "/api/web-push/subscription",
        headers={**csrf(client), **HTTPS},
        json=b.subscription(dms=True, channels=False),
    )
    assert r.status_code == 200
    radio = await radio_adapter()

    # A direct message: notified, encrypted end to end.
    radio.inject(
        IncomingMessage(
            kind="dm",
            text="Summit!",
            pubkey_prefix=simulated._key("tracker")[:12],
            sender_timestamp=int(time.time()),
        )
    )

    async def got(n):
        return len(service.received) >= n

    await wait_for(lambda: got(1))
    n = service.received[0]
    assert n["k"] == "dm" and n["b"] == "Summit!" and n["n"] >= 1 and n["c"]
    assert service.headers[0]["urgency"] == "high" and len(service.headers[0]["topic"]) <= 32

    # A channel message: this browser only wants direct messages.
    radio.inject(
        IncomingMessage(
            kind="channel",
            text="Hello all",
            channel_slot=0,
            sender_label="Bob",
            sender_timestamp=int(time.time()),
        )
    )

    async def stored(body):
        async with db.session_factory()() as s:
            return (await s.execute(select(Message.id).where(Message.body == body))).first() is not None

    await wait_for(lambda: stored("Hello all"))
    await asyncio.sleep(0.3)  # let its (skipped) notification pass
    # The conversation's own alert setting wins: a muted DM isn't notified...
    async with db.session_factory()() as s:
        await s.execute(update(Conversation).where(Conversation.kind == "dm").values(sound="off"))
        # ...and an unmuted channel is.
        await s.execute(update(Conversation).where(Conversation.kind == "channel").values(sound="on"))
        await s.commit()
    radio.inject(
        IncomingMessage(
            kind="dm",
            text="Muted",
            pubkey_prefix=simulated._key("tracker")[:12],
            sender_timestamp=int(time.time()) + 1,
        )
    )
    radio.inject(
        IncomingMessage(
            kind="channel",
            text="Loud",
            channel_slot=0,
            sender_label="Bob",
            sender_timestamp=int(time.time()) + 2,
        )
    )
    await wait_for(lambda: got(2))
    await asyncio.sleep(0.5)
    assert [x["b"] for x in service.received] == ["Summit!", "Loud"]

    # Signing the browser out removes its subscription.
    await client.post("/api/auth/logout", headers=csrf(client))
    async with db.session_factory()() as s:
        assert (await s.execute(select(WebPushSubscription))).first() is None
