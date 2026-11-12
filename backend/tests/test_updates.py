import json
import time

import pytest

from app.config import APP_VERSION, get_settings
from app.services import updates
from app.services.updates import ReleaseInfo, is_newer, parse_version
from tests.conftest import HEADERS, PASSWORD, csrf, do_setup


def test_version_compare():
    assert parse_version("v1.2.3") == (1, 2, 3)
    assert parse_version("1.2") is None
    assert is_newer("0.10.0", "0.9.9") and not is_newer("0.9.9", "0.10.0")
    assert not is_newer("garbage", "0.1.0")


def _bump(v: str) -> str:
    a, b, c = parse_version(v)
    return f"{a}.{b + 1}.0"


@pytest.fixture
def fake_release(monkeypatch):
    newer = _bump(APP_VERSION)

    async def fetch(self, which="latest"):
        if which == "latest":
            return ReleaseInfo(newer, f"https://example.invalid/v{newer}", "Release notes", None, True)
        assert which == f"tags/v{APP_VERSION}"
        return ReleaseInfo(
            APP_VERSION, f"https://example.invalid/v{APP_VERSION}", "Installed notes", None, True
        )

    monkeypatch.setattr(updates.UpdateChecker, "_fetch", fetch)
    updates.checker._checked_at = 0
    yield newer
    updates.checker.__init__()


@pytest.fixture
def native(monkeypatch, tmp_path):
    s = get_settings()
    monkeypatch.setattr(s, "install_kind", "native")
    monkeypatch.setattr(s, "state_dir", str(tmp_path))
    return tmp_path


async def test_update_available_container_cannot_install(client, fake_release):
    await do_setup(client, mode="none")
    info = (await client.get("/api/system/update")).json()
    assert info["update_available"] is True and info["latest"]["version"] == fake_release
    assert info["install_kind"] == "container" and info["can_install"] is False
    r = await client.post("/api/system/update", headers=csrf(client), json={"version": fake_release})
    assert r.status_code == 409


async def test_native_update_request_written_once(client, fake_release, native):
    await do_setup(client, mode="none")
    info = (await client.get("/api/system/update")).json()
    assert info["can_install"] is True
    bad = await client.post("/api/system/update", headers=csrf(client), json={"version": "99.0.0"})
    assert bad.status_code == 409  # only the latest published release
    ok = await client.post("/api/system/update", headers=csrf(client), json={"version": fake_release})
    assert ok.status_code == 202
    req = json.loads((native / "update-request.json").read_text())
    assert req["version"] == fake_release and req["requested_by"] == "owner"
    # The root updater reports progress through the status file.
    (native / "update-status.json").write_text(
        json.dumps({"state": "installing", "version": fake_release, "updated_at": time.time()})
    )
    again = await client.post("/api/system/update", headers=csrf(client), json={"version": fake_release})
    assert again.status_code == 409
    st = (await client.get("/api/system/update/status")).json()
    assert st["status"]["state"] == "installing"


async def test_setup_token_file_written_and_removed(client, native):
    from app.api.auth import SetupToken

    SetupToken.ensure()
    SetupToken.write_file()
    f = native / "setup-token"
    assert f.read_text().strip() == "test-setup-token" and oct(f.stat().st_mode & 0o777) == "0o600"
    r = await client.post(
        "/api/setup",
        headers=HEADERS,
        json={
            "setup_token": "test-setup-token",
            "username": "owner",
            "password": PASSWORD,
            "radio": {"mode": "none"},
        },
    )
    assert r.status_code == 200
    assert not f.exists()


async def test_installed_release_notes_when_up_to_date(client, monkeypatch):
    calls = []

    async def fetch(self, which="latest"):
        calls.append(which)
        return ReleaseInfo(
            APP_VERSION, f"https://example.invalid/v{APP_VERSION}", "What's new here", None, True
        )

    monkeypatch.setattr(updates.UpdateChecker, "_fetch", fetch)
    updates.checker.__init__()
    await do_setup(client, mode="none")
    info = (await client.get("/api/system/update")).json()
    assert info["update_available"] is False
    assert info["installed"]["version"] == APP_VERSION and info["installed"]["notes"] == "What's new here"
    assert calls == ["latest"]  # latest == installed, so no second request
    updates.checker.__init__()


async def test_installed_notes_fetched_separately_when_update_available(client, fake_release):
    await do_setup(client, mode="none")
    info = (await client.get("/api/system/update")).json()
    assert info["latest"]["version"] == fake_release
    assert info["installed"]["notes"] == "Installed notes"


def _github(api_status=403, latest_tag="v9.8.7", sums=True, web_ok=True):
    """A stand-in for api.github.com (refusing, as when its hourly limit is used up) and
    github.com (the latest-release redirect and the release's SHA256SUMS)."""
    import httpx

    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(f"{req.url.host}{req.url.path}")
        if req.url.host == "api.github.com":
            return httpx.Response(api_status, json={"message": "API rate limit exceeded"})
        if not web_ok:
            raise httpx.ConnectError("no route to host")
        repo = "/roach0816/MeshHome/releases"
        if req.url.path == f"{repo}/latest":
            return httpx.Response(302, headers={"location": f"https://github.com{repo}/tag/{latest_tag}"})
        if req.url.path.endswith("/SHA256SUMS") and sums and f"/{latest_tag}/" in req.url.path:
            v = latest_tag.lstrip("v")
            return httpx.Response(200, text=f"{'a' * 64}  meshhome-{v}.tar.gz\n{'b' * 64}  install.sh\n")
        return httpx.Response(404, text="Not Found")

    return httpx.MockTransport(handler), seen


async def test_falls_back_to_the_website_when_the_api_refuses(monkeypatch):
    transport, seen = _github()
    monkeypatch.setattr(updates.UpdateChecker, "transport", transport)
    c = updates.UpdateChecker()
    info = await c._fetch()
    assert (info.version, info.has_native_package, info.notes) == ("9.8.7", True, "")
    assert info.url == "https://github.com/roach0816/MeshHome/releases/tag/v9.8.7"
    assert seen[0].startswith("api.github.com")
    # A specific release (the installed one's page) the same way; one that doesn't exist fails.
    assert (await c._fetch("tags/v9.8.7")).version == "9.8.7"
    import httpx

    with pytest.raises(httpx.HTTPStatusError):  # the API's reason is the one reported
        await c._fetch("tags/v1.2.3")


async def test_release_without_package_yet(monkeypatch):
    transport, _ = _github(sums=False)
    monkeypatch.setattr(updates.UpdateChecker, "transport", transport)
    info = await updates.UpdateChecker()._fetch()
    assert info.version == "9.8.7" and info.has_native_package is False


async def test_reports_the_api_error_when_the_website_fails_too(monkeypatch):
    transport, _ = _github(web_ok=False)
    monkeypatch.setattr(updates.UpdateChecker, "transport", transport)
    c = updates.UpdateChecker()
    await c.check(force=True)
    assert c.summary()["error"].startswith("HTTPStatusError") and "403" in c.summary()["error"]


async def test_no_website_fallback_when_turned_off(monkeypatch):
    transport, seen = _github()
    monkeypatch.setattr(updates.UpdateChecker, "transport", transport)
    monkeypatch.setattr(get_settings(), "update_web_url", "")
    import httpx

    with pytest.raises(httpx.HTTPStatusError):
        await updates.UpdateChecker()._fetch()
    assert all(s.startswith("api.github.com") for s in seen)
