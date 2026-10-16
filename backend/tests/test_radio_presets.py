"""MeshCore radio presets: bundled snapshot, validation, and the daily live refresh."""

import httpx
import pytest

from app.config import get_settings
from app.services import radio_presets
from tests.conftest import do_setup


def test_parse_skips_bad_entries():
    presets = radio_presets.parse(
        [
            {
                "title": "USA",
                "frequency": "910.525",
                "bandwidth": "62.5",
                "spreading_factor": "7",
                "coding_rate": "5",
            },
            {
                "title": "Canada",
                "frequency": "910.525",
                "bandwidth": "62.5",
                "spreading_factor": "7",
                "coding_rate": "5",
                "network_settings": {"path_hash_size": 3},
            },
            {"title": "Missing SF", "frequency": "910.525", "bandwidth": "62.5", "coding_rate": "5"},
            {
                "title": "Silly",
                "frequency": "9999",
                "bandwidth": "62.5",
                "spreading_factor": "7",
                "coding_rate": "5",
            },
            "not a dict",
        ]
    )
    assert [p["id"] for p in presets] == ["usa", "canada"]
    assert presets[0] == {
        "id": "usa",
        "title": "USA",
        "freq_mhz": 910.525,
        "bw_khz": 62.5,
        "sf": 7,
        "cr": 5,
        "path_hash_size": None,
    }
    assert presets[1]["path_hash_size"] == 3
    assert radio_presets.parse(None) == []


def test_bundled_snapshot_covers_the_regions():
    titles = {p["title"] for p in radio_presets.PresetCatalog()._presets}
    assert len(titles) >= 20
    assert {"USA", "Canada", "EU/UK (Narrow)", "Australia", "New Zealand (Narrow)"} <= titles


async def test_presets_endpoint_bundled_when_offline(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "radio_presets_url", "")
    monkeypatch.setattr(radio_presets, "catalog", radio_presets.PresetCatalog())
    await do_setup(client)
    r = await client.get("/api/radio/presets")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "bundled" and any(p["id"] == "usa" for p in body["presets"])


async def test_live_refresh_and_fallback(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers["user-agent"])
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={
                    "config": {
                        "suggested_radio_settings": {
                            "info_message": "Live list",
                            "entries": [
                                {
                                    "title": "Testland",
                                    "frequency": "915.0",
                                    "bandwidth": "125",
                                    "spreading_factor": "9",
                                    "coding_rate": "5",
                                }
                            ],
                        }
                    }
                },
            )
        return httpx.Response(503)

    real = httpx.AsyncClient
    monkeypatch.setattr(
        radio_presets.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )
    monkeypatch.setattr(get_settings(), "radio_presets_url", "https://presets.test/config")
    cat = radio_presets.PresetCatalog()
    live = await cat.get()
    assert live["source"] == "live" and [p["title"] for p in live["presets"]] == ["Testland"]
    assert calls[0].startswith("MeshHome/")
    # Cached for a day; a failed refresh keeps the last good list.
    assert (await cat.get())["presets"][0]["title"] == "Testland" and len(calls) == 1
    cat._next_fetch = 0
    again = await cat.get()
    assert again["source"] == "live" and again["presets"][0]["title"] == "Testland" and len(calls) == 2


@pytest.mark.parametrize("payload", [{"config": {}}, {"nope": 1}, []])
async def test_unusable_live_answer_keeps_bundled(monkeypatch, payload):
    real = httpx.AsyncClient
    monkeypatch.setattr(
        radio_presets.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)), **kw),
    )
    monkeypatch.setattr(get_settings(), "radio_presets_url", "https://presets.test/config")
    cat = radio_presets.PresetCatalog()
    got = await cat.get()
    assert got["source"] == "bundled" and len(got["presets"]) >= 20
