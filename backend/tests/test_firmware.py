"""Radio firmware up-to-date check, against release data shaped like MeshCore's GitHub releases."""

import httpx

from app import db
from app.models import Radio
from app.services import firmware
from tests.conftest import csrf, do_setup

REAL_CLIENT = httpx.AsyncClient

RELEASES = [
    {"tag_name": "repeater-v1.18.0", "draft": False, "prerelease": False},
    {
        "tag_name": "companion-v1.18.0",
        "draft": False,
        "prerelease": False,
        "published_at": "2026-10-01T10:00:00Z",
        "html_url": "https://github.test/releases/companion-v1.18.0",
    },
    {"tag_name": "companion-v1.19.0-beta", "draft": False, "prerelease": True},
    {"tag_name": "companion-v1.17.1", "draft": False, "prerelease": False},
]


def _mock_github(monkeypatch, releases=RELEASES, status_code=200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=releases)

    monkeypatch.setattr(
        firmware.httpx, "AsyncClient", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **kw)
    )
    monkeypatch.setattr(firmware, "catalog", firmware.ReleaseCatalog())


def test_versions():
    assert firmware.version_tuple("v1.17.1") == (1, 17, 1)
    assert firmware.version_tuple("1.17.1-d929643") == (1, 17, 1)
    assert firmware.version_tuple("") is None


async def test_latest_stable_companion_release(monkeypatch):
    _mock_github(monkeypatch)
    latest = await firmware.catalog.latest()
    assert latest["version"] == "1.18.0"  # not the prerelease, not the repeater release
    assert firmware.status(model="M7", version="v1.17.1", latest=latest)["up_to_date"] is False
    assert firmware.status(model="M7", version="v1.18.0", latest=latest)["up_to_date"] is True
    assert firmware.status(model="M7", version=None, latest=latest)["up_to_date"] is None

    _mock_github(monkeypatch, status_code=503)
    assert await firmware.catalog.latest() is None and "GitHub" in firmware.catalog.error


async def test_firmware_api(client, monkeypatch):
    _mock_github(monkeypatch)
    await do_setup(client, mode="none")
    assert (await client.get("/api/radio/firmware")).json() == {
        "available": False,
        "reason": "No radio has connected yet.",
    }
    async with db.session_factory()() as s:
        s.add(
            Radio(
                public_key="cd" * 32,
                name="Home",
                device_info={"model": "Elecrow ThinkNode M7", "ver": "v1.17.1"},
            )
        )
        await s.commit()
    st = (await client.get("/api/radio/firmware")).json()
    assert st["available"] and st["up_to_date"] is False
    assert st["current_version"] == "1.17.1" and st["latest"]["version"] == "1.18.0"
    assert (await client.post("/api/radio/firmware/check", headers=csrf(client))).json()[
        "up_to_date"
    ] is False
