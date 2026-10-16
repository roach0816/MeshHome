"""After the rename to MeshHome, everything made under the old names keeps working: browser
sign-ins, API keys, backups and environment variables."""

import os
import uuid

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app import db
from app.config import Settings
from app.main import app
from app.models import ApiKey, User, utcnow
from app.security import token_digest
from app.services import backup, backup_crypto
from tests.conftest import csrf, do_setup
from tests.test_backup import PASS, _rewrite, _upload, fast_kdf  # noqa: F401


async def test_sign_in_from_before_the_rename_still_works(client):
    await do_setup(client)
    token, csrf_token = client.cookies.get("mh_session"), client.cookies.get("mh_csrf")
    # A browser signed in under 0.8: old cookie names and the old X-Requested-With value.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as old:
        old.cookies.set("mch_session", token)
        old.cookies.set("mch_csrf", csrf_token)
        assert (await old.get("/api/auth/me")).status_code == 200
        legacy = {"X-Requested-With": "meshcore-home", "X-CSRF-Token": csrf_token}
        r = await old.put("/api/settings/notifications", headers=legacy, json={"sound": "dms"})
        assert r.status_code == 200, r.text

        # Signing in again moves the browser to the new names and drops the old cookies.
        r = await old.post(
            "/api/auth/login", headers=legacy, json={"username": "owner", "password": "correct horse battery"}
        )
        assert r.status_code == 200
        set_cookies = r.headers.get_list("set-cookie")
        assert any(c.startswith("mh_session=") for c in set_cookies)
        assert any(c.startswith("mch_session=") and "Max-Age=0" in c for c in set_cookies)


async def test_api_key_from_before_the_rename_still_works(client):
    await do_setup(client)
    raw = "mch_" + "k" * 43
    async with db.session_factory()() as s:
        user = (await s.execute(select(User))).scalar_one()
        s.add(
            ApiKey(
                id=uuid.uuid4(),
                user_id=user.id,
                name="old script",
                prefix=raw[:12],
                key_hash=token_digest(raw),
                scope="read",
                created_at=utcnow(),
            )
        )
        await s.commit()
    r = await client.get("/api/conversations", headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 200, r.text


async def test_backup_from_before_the_rename_restores(client, fast_kdf):  # noqa: F811
    await do_setup(client)
    path = await backup.create(PASS)
    assert path.name.startswith("meshhome-") and path.suffix == ".mhb"
    data = _rewrite(path, PASS, lambda m: m.update(format="meshcore-home-backup"))
    assert data.startswith(backup_crypto.MAGIC)
    old = backup_crypto.LEGACY_MAGIC + data[len(backup_crypto.MAGIC) :]

    upload_id = await _upload(client, old, csrf(client))
    r = await client.post(
        f"/api/restore/{upload_id}/inspect", headers=csrf(client), json={"passphrase": PASS}
    )
    assert r.status_code == 200, r.text
    assert r.json()["can_restore"] is True, r.json()

    # Old file names are still listed, newest first, among new ones.
    (path.parent / "meshcore-home-20200101-000000.mchb").write_bytes(old)
    names = [b["name"] for b in backup.list_backups()]
    assert names[0] == path.name and names[-1] == "meshcore-home-20200101-000000.mchb"
    (path.parent / "meshcore-home-20200101-000000.mchb").unlink()


def test_environment_variables_old_and_new(monkeypatch):
    for k in ("MESHHOME_INSTALL_KIND", "MESHCORE_INSTALL_KIND", "MESHHOME_STATE_DIR", "MESHCORE_STATE_DIR"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MESHCORE_INSTALL_KIND", "native")
    monkeypatch.setenv("MESHCORE_STATE_DIR", "/var/lib/old")
    s = Settings()
    assert (s.install_kind, s.state_dir) == ("native", "/var/lib/old")

    monkeypatch.setenv("MESHHOME_STATE_DIR", "/var/lib/new")
    assert Settings().state_dir == "/var/lib/new"  # the new name wins
    assert os.environ["MESHCORE_STATE_DIR"] == "/var/lib/old"
