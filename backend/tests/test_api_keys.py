"""API keys: lifecycle, read/write permissions, and owner-only endpoints."""

import uuid
from datetime import timedelta

from sqlalchemy import update

from app import db
from app.models import ApiKey, utcnow
from tests.conftest import csrf, do_setup
from tests.test_api import _connected_conversations


def bearer(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


async def _create(client, scope: str, **extra) -> dict:
    r = await client.post(
        "/api/api-keys", headers=csrf(client), json={"name": f"{scope} tester", "scope": scope, **extra}
    )
    assert r.status_code == 201, r.text
    return r.json()


async def test_api_key_lifecycle_and_permissions(client):
    await do_setup(client)
    convs = await _connected_conversations(client)
    public = next(c for c in convs if c["title"] == "Public")

    read = await _create(client, "read")
    write = await _create(client, "write", expires_in_days=30)
    assert read["key"].startswith("mh_") and len(read["key"]) > 40
    assert write["api_key"]["expires_at"] is not None

    # The list never contains the key itself, only its prefix.
    listed = (await client.get("/api/api-keys")).json()
    assert {k["prefix"] for k in listed} == {read["key"][:12], write["key"][:12]}
    assert read["key"] not in str(listed) and write["key"] not in str(listed)

    # A fresh client with no cookies: the key alone authenticates.
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as svc:
        assert (await svc.get("/api/conversations")).status_code == 401
        r = await svc.get("/api/conversations", headers=bearer(read["key"]))
        assert r.status_code == 200 and any(c["id"] == public["id"] for c in r.json())
        assert (await svc.get("/api/contacts", headers=bearer(read["key"]))).status_code == 200
        assert (await svc.get("/api/auth/me", headers=bearer(read["key"]))).json()["username"]

        send = {"client_message_id": uuid.uuid4().hex, "body": "hello from a script"}
        url = f"/api/conversations/{public['id']}/messages"
        r = await svc.post(url, headers=bearer(read["key"]), json=send)
        assert r.status_code == 403 and "read-only" in r.json()["detail"]
        # Write key: no CSRF token or X-Requested-With needed (keys are never sent by a browser).
        r = await svc.post(url, headers=bearer(write["key"]), json=send)
        assert r.status_code == 200 and r.json()["body"] == "hello from a script"
        r = await svc.put(
            f"/api/conversations/{public['id']}/read-position",
            headers=bearer(write["key"]),
            json={"position": r.json()["position"]},
        )
        assert r.status_code == 204

        # Owner administration stays with the signed-in browser.
        for method, path, body in [
            ("GET", "/api/api-keys", None),
            ("POST", "/api/api-keys", {"name": "x", "scope": "write"}),
            ("POST", "/api/system/update", {"version": "9.9.9"}),
            ("PUT", "/api/radio/config/identity", {"name": "x", "share_location": False}),
            ("PUT", "/api/settings/radio", {"mode": "none"}),
            ("POST", "/api/auth/password", {"current_password": "x", "new_password": "y" * 12}),
        ]:
            r = await svc.request(method, path, headers=bearer(write["key"]), json=body)
            assert r.status_code == 403, (method, path, r.status_code, r.text)

        # Unknown and malformed keys.
        assert (await svc.get("/api/conversations", headers=bearer("mh_nope"))).status_code == 401
        assert (await svc.get("/api/conversations", headers=bearer("not-a-key"))).status_code == 401

        # Expired keys stop working.
        async with db.session_factory()() as s:
            await s.execute(
                update(ApiKey)
                .where(ApiKey.id == uuid.UUID(write["api_key"]["id"]))
                .values(expires_at=utcnow() - timedelta(minutes=1))
            )
            await s.commit()
        assert (await svc.get("/api/conversations", headers=bearer(write["key"]))).status_code == 401
        assert any(k["expired"] for k in (await client.get("/api/api-keys")).json())

        # Last use is recorded; revoking ends access.
        assert next(k for k in (await client.get("/api/api-keys")).json() if k["scope"] == "read")[
            "last_used_at"
        ]
        r = await client.delete(f"/api/api-keys/{read['api_key']['id']}", headers=csrf(client))
        assert r.status_code == 204
        assert (await svc.get("/api/conversations", headers=bearer(read["key"]))).status_code == 401


async def test_api_key_validation(client):
    await do_setup(client)
    bad = await client.post("/api/api-keys", headers=csrf(client), json={"name": "  ", "scope": "read"})
    assert bad.status_code == 422
    bad = await client.post("/api/api-keys", headers=csrf(client), json={"name": "x", "scope": "admin"})
    assert bad.status_code == 422
    # Managing keys needs the browser session's CSRF protection like any other change.
    r = await client.post("/api/api-keys", json={"name": "x", "scope": "read"})
    assert r.status_code == 403
