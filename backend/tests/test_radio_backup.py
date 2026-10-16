"""Radio snapshots in backups: a TCP radio's identity key, settings, channels and contacts are
captured with a backup, and put back on a (reflashed) radio only when the owner chooses to.

The simulated radio stands in for the TCP radio: it implements the same adapter calls, with a
real key pair."""

import pytest

from app.radio import identity, simulated
from app.radio.supervisor import supervisor
from app.services import backup, backup_crypto, radio_backup, remote_admin
from tests.conftest import csrf, do_setup, wait_for

PASS = "correct horse battery staple"


@pytest.fixture(autouse=True)
def fresh_radio(monkeypatch):
    monkeypatch.setattr(backup_crypto, "SCRYPT", {"n": 2**10, "r": 8, "p": 1})
    simulated._SIM_STATE = None
    yield
    simulated._SIM_STATE = None


async def _upload(client, data: bytes) -> str:
    r = await client.post(
        "/api/restore/upload",
        content=data,
        headers={**csrf(client), "Content-Type": "application/octet-stream"},
    )
    assert r.status_code == 200, r.text
    return r.json()["upload_id"]


async def _connected():
    await wait_for(lambda: _true(supervisor.connected), timeout=20)


async def _true(v):
    return v or None


async def _backup(client) -> tuple[dict, bytes]:
    r = await client.post("/api/backups", headers=csrf(client), json={"passphrase": PASS})
    assert r.status_code == 200, r.text
    created = r.json()
    return created, (await client.get(f"/api/backups/{created['name']}")).content


def test_public_key_from_the_firmware_private_key():
    prv, pub = remote_admin.generate_identity("")
    assert identity.public_key(bytes.fromhex(prv)) == pub
    assert identity.public_key(simulated.SIM_PRIVATE_KEY) == simulated.SIM_SELF_KEY
    for bad in (b"", bytes(64), b"x" * 63):
        with pytest.raises(ValueError):
            identity.public_key(bad)


async def test_backup_captures_the_radio(client):
    await do_setup(client)
    await _connected()
    created, data = await _backup(client)
    radio = created["radio"]
    assert radio["public_key"] == simulated.SIM_SELF_KEY and radio["key"] == "included"
    assert radio["channels"] == ["Public", "#home-sim"] and radio["contacts"] == len(simulated.SIM_CONTACTS)
    assert radio["name"] == "Home (simulated)" and created["radio_note"] is None
    assert "private_key" not in str(created)

    path = backup.backup_path(created["name"])
    opened = backup.open_backup(path, PASS)
    try:
        assert opened.manifest["format_version"] == 2 and opened.manifest["radio"] is True
        snap = backup.radio_snapshot(opened)
        assert snap.private_key == simulated.SIM_PRIVATE_KEY.hex()
        assert snap.channels[0].secret == b"sim-public-key!!".hex()
        summary = backup.inspect(opened)
    finally:
        opened.close()
    # Restoring the whole backup leaves the radio alone, and says how to restore it.
    assert summary["radio"]["public_key"] == simulated.SIM_SELF_KEY
    assert any("Restore a radio from a backup" in n for n in summary["notes"])


async def test_backup_without_key_export(client):
    await do_setup(client)
    await _connected()
    simulated.sim_state()["key_export"] = False
    created, _ = await _backup(client)
    assert created["radio"]["key"] == "disabled" and "doesn't allow exporting" in created["radio_note"]
    assert created["radio"]["channels"]  # the rest is still there


async def test_backup_without_a_radio(client):
    await do_setup(client, mode="none")
    created, data = await _backup(client)
    assert created["radio"] is None and created["radio_note"] is None
    upload_id = await _upload(client, data)
    r = await client.post(
        f"/api/radio-restore/{upload_id}/inspect", headers=csrf(client), json={"passphrase": PASS}
    )
    assert r.status_code == 422 and "no radio snapshot" in r.json()["detail"]


async def test_restore_a_reflashed_radio(client):
    await do_setup(client)
    await _connected()
    created, data = await _backup(client)

    # Reflashing: a new identity, default settings, no channels or contacts.
    st = simulated.sim_state()
    new_prv, new_pub = remote_admin.generate_identity("")
    st["private_key"], st["public_key"] = bytes.fromhex(new_prv), new_pub
    st.update(name="MeshCore-1234", lat=None, lon=None)
    st["radio"].update(freq_mhz=869.525, sf=11)
    st["channels"] = {0: ("Public", bytes(16))}
    st["removed_contacts"] = {c.public_key for c in simulated.SIM_CONTACTS}
    st["telemetry"] = {"base": 2, "location": 2, "environment": 2}

    upload_id = await _upload(client, data)
    r = await client.post(
        f"/api/radio-restore/{upload_id}/inspect", headers=csrf(client), json={"passphrase": PASS}
    )
    assert r.status_code == 200, r.text
    seen = r.json()
    assert seen["can_restore"] and not seen["same_identity"]
    assert seen["radio"]["public_key"] == new_pub and seen["snapshot"]["public_key"] == simulated.SIM_SELF_KEY

    r = await client.post(
        f"/api/radio-restore/{upload_id}/apply",
        headers=csrf(client),
        json={"passphrase": PASS, "parts": ["identity", "settings", "channels", "contacts"]},
    )
    assert r.status_code == 200, r.text
    steps = {s["part"]: s for s in r.json()["steps"]}
    assert all(s["ok"] for s in steps.values()), steps
    assert steps["contacts"]["detail"].startswith(f"{len(simulated.SIM_CONTACTS)} contacts added")

    st = simulated.sim_state()
    assert st["public_key"] == simulated.SIM_SELF_KEY and st["private_key"] == simulated.SIM_PRIVATE_KEY
    assert st["name"] == "Home (simulated)" and (st["lat"], st["lon"]) == simulated.SIM_HOME_POSITION
    assert st["radio"]["freq_mhz"] == 910.525 and st["radio"]["sf"] == 7
    assert st["telemetry"] == {"base": 0, "location": 0, "environment": 0}
    assert {slot: name for slot, (name, _) in st["channels"].items()} == {0: "Public", 1: "#home-sim"}
    assert st["channels"][0][1] == b"sim-public-key!!"
    assert not st["removed_contacts"] & {c.public_key for c in simulated.SIM_CONTACTS}

    # MeshHome reconnects and finds its original radio again.
    async def back():
        return (
            supervisor.connected
            and supervisor.radio
            and supervisor.radio.public_key == simulated.SIM_SELF_KEY
        )

    await wait_for(back, timeout=20)
    # The upload is used up.
    r = await client.post(
        f"/api/radio-restore/{upload_id}/inspect", headers=csrf(client), json={"passphrase": PASS}
    )
    assert r.status_code == 404


async def test_restore_only_some_parts(client):
    await do_setup(client)
    await _connected()
    _, data = await _backup(client)
    st = simulated.sim_state()
    st["name"] = "Renamed"
    st["channels"] = {}
    upload_id = await _upload(client, data)
    r = await client.post(
        f"/api/radio-restore/{upload_id}/apply",
        headers=csrf(client),
        json={"passphrase": PASS, "parts": ["channels"]},
    )
    assert r.status_code == 200, r.text
    assert [s["part"] for s in r.json()["steps"]] == ["channels"]
    assert simulated.sim_state()["name"] == "Renamed" and len(simulated.sim_state()["channels"]) == 2


async def test_same_identity_and_missing_key(client):
    await do_setup(client)
    await _connected()
    simulated.sim_state()["key_export"] = False
    _, data = await _backup(client)
    upload_id = await _upload(client, data)
    r = await client.post(
        f"/api/radio-restore/{upload_id}/inspect", headers=csrf(client), json={"passphrase": PASS}
    )
    assert r.json()["same_identity"] and r.json()["snapshot"]["key"] == "disabled"
    r = await client.post(
        f"/api/radio-restore/{upload_id}/apply",
        headers=csrf(client),
        json={"passphrase": PASS, "parts": ["identity"]},
    )
    step = r.json()["steps"][0]
    assert not step["ok"] and "doesn't allow exporting" in step["detail"]


async def test_radio_restore_checks_its_input(client):
    await do_setup(client)
    await _connected()
    _, data = await _backup(client)
    upload_id = await _upload(client, data)
    r = await client.post(
        f"/api/radio-restore/{upload_id}/apply",
        headers=csrf(client),
        json={"passphrase": PASS, "parts": ["firmware"]},
    )
    assert r.status_code == 422
    r = await client.post(
        f"/api/radio-restore/{upload_id}/apply",
        headers=csrf(client),
        json={"passphrase": "wrong passphrase!", "parts": ["identity"]},
    )
    assert r.status_code == 422 and "Wrong passphrase" in r.json()["detail"]
    client.cookies.clear()
    r = await client.post(f"/api/radio-restore/{upload_id}/inspect", json={"passphrase": PASS})
    assert r.status_code in (401, 403)


def test_snapshot_must_match_its_key():
    other_prv, _ = remote_admin.generate_identity("")
    base = {
        "captured_at": "2026-10-16T00:00:00+00:00",
        "public_key": simulated.SIM_SELF_KEY,
        "private_key": simulated.SIM_PRIVATE_KEY.hex(),
        "key": "included",
        "name": "x",
    }
    assert radio_backup.Snapshot(**base)
    for change in (
        {"private_key": other_prv},  # someone else's key
        {"private_key": None},  # says included, but isn't
        {"key": "disabled"},  # says missing, but is there
        {"public_key": "AB" * 32},
    ):
        with pytest.raises(ValueError):
            radio_backup.Snapshot(**{**base, **change})
    with pytest.raises(ValueError, match="damaged"):
        radio_backup.parse(b'{"public_key": "nope"}')


# ---- the TCP radio's side, against a stand-in for the meshcore library ---------------------------


class FakeCommands:
    def __init__(self, export):
        self.export, self.imported, self.added = export, None, []

    async def export_private_key(self):
        from meshcore.events import Event, EventType

        if self.export is None:
            return Event(EventType.DISABLED, {"reason": "private_key_export_disabled"})
        return Event(EventType.PRIVATE_KEY, {"private_key": self.export})

    async def import_private_key(self, key):
        from meshcore.events import Event, EventType

        self.imported = key
        return Event(EventType.OK, {})

    async def get_contacts(self, timeout=None):
        from meshcore.events import Event, EventType

        record = {
            "public_key": "ab" * 32,
            "type": 2,
            "flags": 1,
            "out_path_len": 1,
            "out_path": "c3",
            "out_path_hash_mode": 0,
            "adv_name": "Roof",
            "last_advert": 1760000000,
            "adv_lat": 40.0,
            "adv_lon": -105.0,
            "lastmod": 1760000001,  # a cache field: not part of the record
        }
        return Event(EventType.CONTACTS, {record["public_key"]: record})

    async def add_contact(self, contact):
        from meshcore.events import Event, EventType

        self.added.append(contact)
        return Event(EventType.OK, {})


def _tcp(export):
    from app.radio.meshcore_tcp import MeshCoreTcpRadio

    radio = MeshCoreTcpRadio("192.0.2.1", 5000)
    radio._mc = type("MC", (), {"is_connected": True, "commands": FakeCommands(export)})()
    return radio


async def test_tcp_radio_key_and_contacts():
    from app.radio.base import NotSupported

    radio = _tcp(simulated.SIM_PRIVATE_KEY)
    assert await radio.export_private_key() == simulated.SIM_PRIVATE_KEY
    await radio.import_private_key(simulated.SIM_PRIVATE_KEY)
    assert radio._mc.commands.imported == simulated.SIM_PRIVATE_KEY
    records = await radio.contact_records()
    assert records == [
        {
            k: v
            for k, v in (await radio._mc.commands.get_contacts()).payload["ab" * 32].items()
            if k != "lastmod"
        }
    ]
    await radio.add_contact_record(records[0])
    assert radio._mc.commands.added == records
    assert radio_backup.ContactSnap(**records[0])

    with pytest.raises(NotSupported, match="turned off"):
        await _tcp(None).export_private_key()
    with pytest.raises(Exception, match="malformed"):
        await _tcp(b"short").export_private_key()
