"""Install as an app: the setting (HTTPS only) and the web app manifest."""

from tests.conftest import csrf, do_setup

HTTPS = {"X-Forwarded-Proto": "https"}  # as a TLS-terminating proxy (nginx, an ingress) sends


async def test_install_needs_https_and_the_owner(client):
    await do_setup(client, mode="none")
    r = await client.get("/api/settings/app-install")
    assert r.json() == {"enabled": False, "https": False}
    assert (await client.get("/manifest.webmanifest")).status_code == 404

    # Over plain HTTP it can't be turned on.
    r = await client.put("/api/settings/app-install", headers=csrf(client), json={"enabled": True})
    assert r.status_code == 409 and "HTTPS" in r.json()["detail"]

    # API keys can't change it.
    key = (
        await client.post("/api/api-keys", headers=csrf(client), json={"name": "x", "scope": "write"})
    ).json()["key"]
    r = await client.put(
        "/api/settings/app-install",
        headers={"Authorization": f"Bearer {key}", **HTTPS},
        json={"enabled": True},
    )
    assert r.status_code == 403

    r = await client.put(
        "/api/settings/app-install", headers={**csrf(client), **HTTPS}, json={"enabled": True}
    )
    assert r.status_code == 200 and r.json() == {"enabled": True, "https": True}
    m = await client.get("/manifest.webmanifest")
    assert m.status_code == 200
    assert m.headers["content-type"].startswith("application/manifest+json")
    manifest = m.json()
    assert manifest["display"] == "standalone" and manifest["start_url"] == "/"
    assert {i["sizes"] for i in manifest["icons"]} >= {"192x192", "512x512"}
    assert manifest["share_target"]["action"] == "/share" and manifest["share_target"]["method"] == "GET"

    # Turning it off works from anywhere (and makes the manifest disappear).
    r = await client.put("/api/settings/app-install", headers=csrf(client), json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert (await client.get("/manifest.webmanifest")).status_code == 404
