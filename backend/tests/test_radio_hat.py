"""Radio HAT option (native Raspberry Pi installs): availability, requests to the root helper,
and the "hat" radio mode. The test machine is not a Pi, so Pi-specific facts are patched."""

import json
import time

import pytest

from app.config import get_settings
from app.services import radio_hat
from tests.conftest import csrf, do_setup


@pytest.fixture
def native(monkeypatch, tmp_path):
    s = get_settings()
    monkeypatch.setattr(s, "install_kind", "native")
    monkeypatch.setattr(s, "state_dir", str(tmp_path))
    return tmp_path


@pytest.fixture
def pi5(monkeypatch):
    monkeypatch.setattr(radio_hat, "pi_model", lambda: "Raspberry Pi 5 Model B Rev 1.0")
    monkeypatch.setattr(radio_hat, "hat_product", lambda: "WisMesh Pi HAT (RAKwireless)")
    monkeypatch.setattr(radio_hat, "glibc_version", lambda: "2.41")

    async def absent():
        return {"installed": False}

    monkeypatch.setattr(radio_hat, "service_state", absent)


def test_pinned_release_and_glibc_check(monkeypatch):
    lock = radio_hat.pinned()
    assert lock["ZEPHCORE_VERSION"] and len(lock["ZEPHCORE_PI4_SHA256"]) == 64
    assert (
        radio_hat._version("2.36")
        < radio_hat._version(lock["ZEPHCORE_MIN_GLIBC"])
        <= radio_hat._version("2.41")
    )


async def test_old_os_is_explained(client, native, pi5, monkeypatch):
    monkeypatch.setattr(radio_hat, "glibc_version", lambda: "2.36")
    await do_setup(client)
    info = (await client.get("/api/system/radio-hat")).json()
    assert (
        info["available"] is False
        and "Trixie" in info["unavailable_reason"]
        and "2.36" in info["unavailable_reason"]
    )
    r = await client.post("/api/system/radio-hat", headers=csrf(client), json={"action": "install"})
    assert r.status_code == 409


def test_board_detection():
    assert radio_hat.board("Raspberry Pi 5 Model B Rev 1.0") == "pi5"
    assert radio_hat.board("Raspberry Pi 4 Model B Rev 1.5") == "pi4"
    assert radio_hat.board("Raspberry Pi 400 Rev 1.0") == "pi4"
    assert radio_hat.board("Raspberry Pi 3 Model B Plus Rev 1.3") is None
    assert radio_hat.board(None) is None


async def test_container_install_has_no_radio_hat(client):
    await do_setup(client)
    info = (await client.get("/api/system/radio-hat")).json()
    assert info["available"] is False and "install.sh" in info["unavailable_reason"]
    r = await client.post("/api/system/radio-hat", headers=csrf(client), json={"action": "install"})
    assert r.status_code == 409
    r = await client.put("/api/settings/radio", headers=csrf(client), json={"mode": "hat"})
    assert r.status_code == 422


async def test_radio_hat_setup_request_and_mode(client, native, pi5, monkeypatch):
    await do_setup(client)
    info = (await client.get("/api/system/radio-hat")).json()
    assert info["available"] is True and info["board"] == "pi5" and info["phase"] == "absent"
    assert info["host"] == "127.0.0.1" and info["port"] == 5000

    # Remove/restart need an installed service; install goes to the root helper.
    r = await client.post("/api/system/radio-hat", headers=csrf(client), json={"action": "restart"})
    assert r.status_code == 409
    r = await client.post("/api/system/radio-hat", headers=csrf(client), json={"action": "install"})
    assert r.status_code == 202
    req = json.loads((native / "config-request.json").read_text())
    assert req["action"] == "hat-install"
    # One privileged change at a time.
    r = await client.post("/api/system/radio-hat", headers=csrf(client), json={"action": "reboot"})
    assert r.status_code == 409
    (native / "config-request.json").unlink()

    # The helper's progress is reported back.
    (native / "radio-hat.json").write_text(
        json.dumps({"state": "installing", "message": "Downloading ZephCore", "updated_at": time.time()})
    )
    info = (await client.get("/api/system/radio-hat")).json()
    assert info["phase"] == "installing" and info["last_message"] == "Downloading ZephCore"

    # "rebooting" lasts until the Pi has started again; nothing rewrites the file after that.
    restart_at = time.time()
    (native / "radio-hat.json").write_text(
        json.dumps({"state": "rebooting", "message": "Restarting the Pi", "updated_at": restart_at})
    )
    monkeypatch.setattr(radio_hat, "boot_time", lambda: restart_at - 3600)
    assert (await client.get("/api/system/radio-hat")).json()["phase"] == "rebooting"
    monkeypatch.setattr(radio_hat, "boot_time", lambda: restart_at + 30)
    assert (await client.get("/api/system/radio-hat")).json()["phase"] == "absent"

    # An unsupported system is explained, and setup is refused.
    (native / "radio-hat.json").write_text(
        json.dumps({"state": "unsupported", "message": "needs Raspberry Pi OS 13", "updated_at": time.time()})
    )
    info = (await client.get("/api/system/radio-hat")).json()
    assert info["available"] is False and "Raspberry Pi OS 13" in info["unavailable_reason"]

    # Mode "hat" is accepted on a native install (the radio is then 127.0.0.1:5000).
    r = await client.put("/api/settings/radio", headers=csrf(client), json={"mode": "hat"})
    assert r.status_code == 200 and r.json()["mode"] == "hat"


async def test_api_keys_cannot_manage_the_radio_hat(client, native, pi5):
    await do_setup(client)
    key = (
        await client.post("/api/api-keys", headers=csrf(client), json={"name": "x", "scope": "write"})
    ).json()["key"]
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as svc:
        h = {"Authorization": f"Bearer {key}"}
        assert (await svc.get("/api/system/radio-hat", headers=h)).status_code == 200
        r = await svc.post("/api/system/radio-hat", headers=h, json={"action": "reboot"})
        assert r.status_code == 403


async def test_running_radio_is_ready_inside_the_app_sandbox(client, native, pi5, monkeypatch):
    # The app has a private /dev (PrivateDevices=true): the SPI device only shows in sysfs.
    sysfs = native / "spidev0.0"
    monkeypatch.setattr(radio_hat, "SPIDEV", str(native / "no-such-device"))
    monkeypatch.setattr(radio_hat, "SPIDEV_SYSFS", str(sysfs))
    svc = {"installed": True, "active": False, "state": "inactive (dead)", "condition_met": True}

    async def service():
        return svc

    monkeypatch.setattr(radio_hat, "service_state", service)
    monkeypatch.setattr(radio_hat, "boot_time", lambda: time.time())
    (native / "radio-hat.json").write_text(
        json.dumps({"state": "rebooting", "message": "Restarting the Pi", "updated_at": time.time() - 120})
    )
    await do_setup(client)
    assert (await client.get("/api/system/radio-hat")).json()["phase"] == "needs_reboot"

    sysfs.mkdir()
    info = (await client.get("/api/system/radio-hat")).json()
    assert info["phase"] == "stopped" and info["spi_device"] is True

    # A running service is ready, whatever an older status file says.
    svc.update(active=True, state="active (running)")
    (native / "radio-hat.json").write_text(json.dumps({"state": "needs_reboot", "updated_at": time.time()}))
    assert (await client.get("/api/system/radio-hat")).json()["phase"] == "ready"
