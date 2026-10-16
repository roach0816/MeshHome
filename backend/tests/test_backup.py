"""Backup and restore: the encrypted file format, and full restores through the API."""

import io
import json
import tarfile

import pytest
from sqlalchemy import func, select, text

from app import db
from app.api.auth import SetupToken
from app.config import get_settings
from app.models import Contact, Message
from app.radio.supervisor import supervisor
from app.services import backup, backup_crypto
from tests.conftest import HEADERS, PASSWORD, csrf, do_setup, wait_for
from tests.test_api import _connected_conversations

PASS = "correct horse battery staple"


# ---- file format ------------------------------------------------------------------------------


def _roundtrip(data: bytes, passphrase=PASS, decrypt_with=PASS) -> bytes:
    enc = io.BytesIO()
    backup_crypto.encrypt(io.BytesIO(data), enc, passphrase)
    out = io.BytesIO()
    backup_crypto.decrypt(io.BufferedReader(io.BytesIO(enc.getvalue())), out, decrypt_with)
    return out.getvalue()


def test_encryption_roundtrip_and_tamper_detection(monkeypatch):
    monkeypatch.setattr(backup_crypto, "CHUNK", 1000)  # several chunks
    monkeypatch.setattr(backup_crypto, "SCRYPT", {"n": 2**10, "r": 8, "p": 1})
    data = bytes(range(256)) * 20
    assert _roundtrip(data) == data
    assert _roundtrip(b"") == b""
    with pytest.raises(backup_crypto.BackupError, match="Wrong passphrase"):
        _roundtrip(data, decrypt_with="not the passphrase")
    with pytest.raises(backup_crypto.BackupError, match="at least 10"):
        _roundtrip(data, passphrase="short")

    enc = io.BytesIO()
    backup_crypto.encrypt(io.BytesIO(data), enc, PASS)
    raw = enc.getvalue()
    header_end = raw.index(b"\n", len(backup_crypto.MAGIC)) + 1
    chunk = 4 + 1000 + 16

    def dec(b: bytes):
        backup_crypto.decrypt(io.BufferedReader(io.BytesIO(b)), io.BytesIO(), PASS)

    with pytest.raises(backup_crypto.BackupError, match="incomplete|damaged"):
        dec(raw[: header_end + 2 * chunk + 10])  # cut mid-chunk
    with pytest.raises(backup_crypto.BackupError, match="damaged"):
        dec(raw[: header_end + 2 * chunk])  # cut cleanly after a chunk: last-chunk flag catches it
    flipped = bytearray(raw)
    flipped[header_end + chunk + 30] ^= 1
    with pytest.raises(backup_crypto.BackupError, match="damaged or was changed"):
        dec(bytes(flipped))
    with pytest.raises(backup_crypto.BackupError, match="not a MeshHome backup"):
        dec(b"PK\x03\x04zipfile")


# ---- through the API ---------------------------------------------------------------------------


@pytest.fixture
def fast_kdf(monkeypatch):
    monkeypatch.setattr(backup_crypto, "SCRYPT", {"n": 2**10, "r": 8, "p": 1})


async def _with_messages(client):
    await _connected_conversations(client)
    for _ in range(3):
        supervisor.simulate_incoming()

    async def some():
        return (await _counts())[0] >= 3

    await wait_for(some)


async def _counts():
    async with db.session_factory()() as s:
        msgs = (await s.execute(select(func.count()).select_from(Message))).scalar_one()
        contacts = (await s.execute(select(func.count()).select_from(Contact))).scalar_one()
    return msgs, contacts


async def _upload(client, data: bytes, headers: dict) -> str:
    r = await client.post(
        "/api/restore/upload", content=data, headers={**headers, "Content-Type": "application/octet-stream"}
    )
    assert r.status_code == 200, r.text
    return r.json()["upload_id"]


async def test_backup_and_restore_in_settings(client, fast_kdf):
    await do_setup(client)
    await _with_messages(client)
    before = await _counts()
    assert before[0] > 0 and before[1] > 0

    r = await client.post("/api/backups", headers=csrf(client), json={"passphrase": PASS})
    assert r.status_code == 200, r.text
    name = r.json()["name"]
    assert r.json()["system_included"] is False  # not a native install
    listed = (await client.get("/api/backups")).json()
    assert name in [b["name"] for b in listed["backups"]] and listed["native"] is False
    data = (await client.get(f"/api/backups/{name}")).content
    assert data.startswith(backup_crypto.MAGIC)
    assert (
        await client.post("/api/backups", headers=csrf(client), json={"passphrase": "short"})
    ).status_code == 422

    # Change things after the backup.
    async with db.session_factory()() as s:
        await s.execute(text("DELETE FROM messages"))
        await s.execute(text("UPDATE users SET username = 'someone-else'"))
        await s.commit()

    upload_id = await _upload(client, data, csrf(client))
    r = await client.post(
        f"/api/restore/{upload_id}/inspect", headers=csrf(client), json={"passphrase": "wrong passphrase!"}
    )
    assert r.status_code == 422 and "Wrong passphrase" in r.json()["detail"]
    r = await client.post(
        f"/api/restore/{upload_id}/inspect", headers=csrf(client), json={"passphrase": PASS}
    )
    summary = r.json()
    assert r.status_code == 200 and summary["can_restore"] and summary["counts"]["messages"] == before[0]
    assert any("Sign-in sessions" in x for x in summary["not_restored"])

    r = await client.post(f"/api/restore/{upload_id}/apply", headers=csrf(client), json={"passphrase": PASS})
    assert r.status_code == 200, r.text
    assert await _counts() == before
    # Everyone signs in again, with the restored account.
    assert (await client.get("/api/auth/me")).status_code == 401
    r = await client.post(
        "/api/auth/login", headers=HEADERS, json={"username": "owner", "password": PASSWORD}
    )
    assert r.status_code == 200
    # The upload is consumed.
    r = await client.post(
        f"/api/restore/{upload_id}/inspect", headers=csrf(client), json={"passphrase": PASS}
    )
    assert r.status_code == 404


async def test_restore_from_setup_wizard(client, fast_kdf):
    await do_setup(client)
    await _with_messages(client)
    before = await _counts()
    path = await backup.create(PASS)
    data = path.read_bytes()

    # A fresh installation: empty database, wizard not run.
    async with db.session_factory()() as s:
        names = ", ".join(t.name for t in backup.tables())
        await s.execute(text(f"TRUNCATE {names}, sessions CASCADE"))
        await s.commit()
    client.cookies.clear()
    token = SetupToken.ensure()
    assert (await client.get("/api/setup/status")).json()["needs_setup"] is True

    wrong = {**HEADERS, "X-Setup-Token": "nope"}
    r = await client.post(
        "/api/restore/upload", content=data, headers={**wrong, "Content-Type": "application/octet-stream"}
    )
    assert r.status_code == 403
    good = {**HEADERS, "X-Setup-Token": token}
    upload_id = await _upload(client, data, good)
    assert (
        await client.post(f"/api/restore/{upload_id}/inspect", headers=good, json={"passphrase": PASS})
    ).json()["can_restore"]
    r = await client.post(f"/api/restore/{upload_id}/apply", headers=good, json={"passphrase": PASS})
    assert r.status_code == 200, r.text
    assert r.json()["safety_backup"] is None  # nothing to keep on a fresh install
    assert (await client.get("/api/setup/status")).json()["needs_setup"] is False
    assert await _counts() == before
    assert SetupToken.value is None
    # Setup is complete now: the setup token no longer opens restore.
    r = await client.post(
        "/api/restore/upload", content=data, headers={**good, "Content-Type": "application/octet-stream"}
    )
    assert r.status_code == 401


def _rewrite(path, passphrase, change_manifest=None, extra_system=None):
    """Decrypt a backup, change its manifest (and optionally add a system part), re-encrypt."""
    opened = backup.open_backup(path, passphrase)
    try:
        manifest = dict(opened.manifest)
        if change_manifest:
            change_manifest(manifest)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            digests = {}
            for name, p in opened.members.items():
                if name != "manifest.json":
                    backup._add(tar, name, p.read_bytes(), digests)
            if extra_system is not None:
                backup._add(tar, "system.tar.gz", extra_system, digests)
                manifest["system"] = True
            manifest["sha256"] = digests
            backup._add(tar, "manifest.json", json.dumps(manifest).encode(), {})
        out = io.BytesIO()
        backup_crypto.encrypt(io.BytesIO(buf.getvalue()), out, passphrase)
        return out.getvalue()
    finally:
        opened.close()


def _system_part(manifest: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = json.dumps(manifest).encode()
        info = tarfile.TarInfo("./system-manifest.json")  # as the installer packs it
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


async def test_newer_backup_is_refused(client, fast_kdf):
    await do_setup(client)
    path = await backup.create(PASS)

    def newer(m):
        m["schema_revision"] = "9999"
        m["app_version"] = "9.9.9"

    upload_id = await _upload(client, _rewrite(path, PASS, newer), csrf(client))
    summary = (
        await client.post(
            f"/api/restore/{upload_id}/inspect", headers=csrf(client), json={"passphrase": PASS}
        )
    ).json()
    assert summary["can_restore"] is False and "newer MeshHome (v9.9.9)" in summary["errors"][0]
    r = await client.post(f"/api/restore/{upload_id}/apply", headers=csrf(client), json={"passphrase": PASS})
    assert r.status_code == 409


async def test_native_backup_into_a_container(client, fast_kdf, monkeypatch):
    await do_setup(client)
    await client.put("/api/settings/radio", headers=csrf(client), json={"mode": "none"})
    async with db.session_factory()() as s:
        await s.execute(
            text("UPDATE app_settings SET value = jsonb_set(value, '{mode}', '\"hat\"') WHERE key = 'radio'")
        )
        await s.commit()
    path = await backup.create(PASS)
    system = _system_part(
        {"https": True, "https_host": "home.example.org", "provider_name": "Cloudflare", "hat_data": True}
    )
    data = _rewrite(path, PASS, lambda m: m.update(install_kind="native"), extra_system=system)

    assert get_settings().install_kind != "native"
    upload_id = await _upload(client, data, csrf(client))
    summary = (
        await client.post(
            f"/api/restore/{upload_id}/inspect", headers=csrf(client), json={"passphrase": PASS}
        )
    ).json()
    assert (
        summary["can_restore"] and summary["install_kind"] == "native" and summary["system_restore"] is False
    )
    joined = " ".join(summary["not_restored"])
    assert "Ingress" in joined and "radio HAT" in joined
    assert any("radio HAT, which isn't available here" in n for n in summary["notes"])
    r = await client.post(f"/api/restore/{upload_id}/apply", headers=csrf(client), json={"passphrase": PASS})
    assert r.status_code == 200 and r.json()["system"] == "none"
    async with db.session_factory()() as s:
        mode = (await s.execute(text("SELECT value->>'mode' FROM app_settings WHERE key = 'radio'"))).scalar()
    assert mode == "none"  # the HAT isn't available in a container
