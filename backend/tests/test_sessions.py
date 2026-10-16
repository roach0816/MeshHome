"""Sign-in sessions (browsers only since the mobile apps were removed), signed-in devices,
/api/meta, and Bearer tokens (API keys only)."""

import asyncio
import contextlib
import socket
from datetime import timedelta

import uvicorn
import websockets
from httpx import ASGITransport, AsyncClient

from app import db
from app.main import app
from app.models import Session, User, utcnow
from app.security import new_token, token_digest
from tests.conftest import HEADERS, PASSWORD, csrf, do_setup


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@contextlib.asynccontextmanager
async def fresh_client():
    """A client with no cookies, like another browser."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def login(c: AsyncClient, **extra) -> dict:
    r = await c.post(
        "/api/auth/login", headers=HEADERS, json={"username": "owner", "password": PASSWORD, **extra}
    )
    assert r.status_code == 200, r.text
    return r.json()


async def test_meta_before_and_after_setup(client):
    r = await client.get("/api/meta")
    assert r.status_code == 200
    meta = r.json()
    assert meta["product"] == "meshhome"
    assert meta["api_version"] == 1
    assert meta["needs_setup"] is True
    assert meta["install_kind"] == "container"
    assert "signed_in_devices" in meta["features"]
    assert "app_sessions" not in meta["features"] and "push_notifications" not in meta["features"]
    assert "software_updates" not in meta["features"]  # native installs only

    await do_setup(client)
    assert (await client.get("/api/meta")).json()["needs_setup"] is False


async def test_sign_in_gives_cookies_only(client):
    await do_setup(client)
    async with fresh_client() as other:
        body = await login(other)
        assert body == {"username": "owner", "home_name": "Home"}
        assert "mh_session" in other.cookies and "mh_csrf" in other.cookies
    # An app from an older release asking for a token gets an ordinary browser session.
    async with fresh_client() as phone:
        body = await login(phone, client="ios", device_name="Old phone")
        assert "token" not in body and "mh_session" in phone.cookies


async def test_an_app_session_from_an_older_release_is_refused(client):
    await do_setup(client)
    token = "mhd_" + new_token()
    async with db.session_factory()() as s:
        owner = (await s.execute(User.__table__.select())).first()
        s.add(
            Session(
                token_hash=token_digest(token),
                user_id=owner.id,
                csrf_token=new_token(),
                expires_at=utcnow() + timedelta(days=30),
                client="ios",
                device_name="Old phone",
            )
        )
        await s.commit()
    async with fresh_client() as phone:
        assert (await phone.get("/api/auth/me", headers=bearer(token))).status_code == 401
        phone.cookies.set("mh_session", token)
        assert (await phone.get("/api/auth/me")).status_code == 401
    # Not listed, and removed by "sign out everywhere else".
    assert len((await client.get("/api/auth/sessions")).json()) == 1
    assert (await client.delete("/api/auth/sessions", headers=csrf(client))).status_code == 204
    async with db.session_factory()() as s:
        assert (await s.execute(Session.__table__.select().where(Session.client == "ios"))).first() is None


async def test_signed_in_devices_and_sign_out(client):
    await do_setup(client)
    async with fresh_client() as laptop:
        await login(laptop)
        sessions = (await client.get("/api/auth/sessions")).json()
        assert sorted(s["current"] for s in sessions) == [False, True]
        laptop_id = next(s["id"] for s in sessions if not s["current"])

        # Changes from the browser still need CSRF.
        assert (await client.delete(f"/api/auth/sessions/{laptop_id}")).status_code == 403
        r = await client.delete(f"/api/auth/sessions/{laptop_id}", headers=csrf(client))
        assert r.status_code == 204
        assert (await laptop.get("/api/auth/me")).status_code == 401
    r = await client.delete(f"/api/auth/sessions/{laptop_id}", headers=csrf(client))
    assert r.status_code == 404
    assert [s["current"] for s in (await client.get("/api/auth/sessions")).json()] == [True]


async def test_sign_out_everywhere_else(client):
    await do_setup(client)
    async with fresh_client() as laptop:
        await login(laptop)
        assert (await client.delete("/api/auth/sessions", headers=csrf(client))).status_code == 204
        assert (await client.get("/api/auth/me")).status_code == 200
        assert (await laptop.get("/api/auth/me")).status_code == 401


async def test_a_browser_cookie_is_not_a_bearer_token(client):
    await do_setup(client)
    async with fresh_client() as other:
        r = await other.get("/api/auth/me", headers=bearer(client.cookies.get("mh_session")))
        assert r.status_code == 401
        assert r.json()["detail"] == "Invalid or expired API key"


async def test_restore_upload_refuses_an_api_key(client):
    await do_setup(client)
    r = await client.post("/api/api-keys", headers=csrf(client), json={"name": "script", "scope": "write"})
    key = r.json()["key"]
    async with fresh_client() as other:
        headers = {"Content-Type": "application/octet-stream", **bearer(key)}
        r = await other.post("/api/restore/upload", content=b"not a backup", headers=headers)
        assert r.status_code == 401


# ---- WebSocket: needs a real server, since the test client can't speak WebSocket ---------------


@contextlib.asynccontextmanager
async def live_server():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="warning"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        yield f"ws://127.0.0.1:{port}/ws"
    finally:
        server.should_exit = True
        await task
        sock.close()


async def _closed_with(ws) -> int:
    async def drain():
        with contextlib.suppress(websockets.ConnectionClosed):
            while True:
                await ws.recv()

    await asyncio.wait_for(drain(), timeout=5)
    return ws.close_code


async def test_websocket_closes_when_api_key_revoked(client):
    await do_setup(client)
    r = await client.post("/api/api-keys", headers=csrf(client), json={"name": "listener", "scope": "read"})
    created = r.json()
    async with live_server() as url:
        async with websockets.connect(url, additional_headers=bearer(created["key"])) as ws:
            await asyncio.wait_for(ws.recv(), timeout=5)
            r = await client.delete(f"/api/api-keys/{created['api_key']['id']}", headers=csrf(client))
            assert r.status_code == 204
            assert await _closed_with(ws) == 4401
