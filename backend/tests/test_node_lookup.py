"""Who a key prefix is (path hops, repeaters' neighbours), and place names for positions."""

import pytest

from app.services import places
from tests.conftest import csrf, do_setup
from tests.test_api import _connected_conversations


async def test_lookup_by_key_prefix(client):
    await do_setup(client)
    await _connected_conversations(client)
    nodes = (await client.get("/api/map")).json()["nodes"]
    repeater = next(n for n in nodes if n["kind"] == 2)

    r = await client.get("/api/contacts/lookup", params={"prefix": repeater["public_key"][:4].upper()})
    assert r.status_code == 200
    body = r.json()
    assert body["prefix"] == repeater["public_key"][:4]
    match = next(c for c in body["items"] if c["id"] == repeater["id"])
    assert (match["lat"], match["lon"]) == (repeater["lat"], repeater["lon"])
    # The simulated radio advertises a position, so the distance to it is known.
    assert isinstance(match["distance_km"], float) and match["distance_km"] >= 0
    assert all(c["public_key"].startswith(body["prefix"]) for c in body["items"])

    full = await client.get("/api/contacts/lookup", params={"prefix": repeater["public_key"]})
    assert [c["id"] for c in full.json()["items"]] == [repeater["id"]]
    assert (await client.get("/api/contacts/lookup", params={"prefix": "0" * 64})).json()["items"] == []
    for bad in ("zz", "a", ""):
        assert (await client.get("/api/contacts/lookup", params={"prefix": bad})).status_code == 422


async def test_place_names_follow_the_map_setting(client, monkeypatch):
    await do_setup(client, mode="none")
    asked = []

    async def fake(lat, lon):
        asked.append((lat, lon))
        return "Springfield, Illinois, US"

    monkeypatch.setattr(places, "place_name", fake)
    r = await client.get("/api/places", params={"lat": 39.78, "lon": -89.65})
    assert r.json() == {
        "name": "Springfield, Illinois, US",
        "enabled": True,
        "source": "OpenStreetMap Nominatim",
    }
    assert (await client.get("/api/places", params={"lat": 91, "lon": 0})).status_code == 422

    cfg = (await client.get("/api/settings/map")).json()
    assert cfg["place_names"] is True
    r = await client.put("/api/settings/map", headers=csrf(client), json={**cfg, "place_names": False})
    assert r.status_code == 200
    r = await client.get("/api/places", params={"lat": 39.78, "lon": -89.65})
    assert r.json()["enabled"] is False and r.json()["name"] is None
    assert len(asked) == 1  # nothing is looked up while it's off


def test_place_label():
    assert (
        places.label({"city": "Springfield", "state": "Illinois", "country_code": "us"})
        == "Springfield, Illinois, US"
    )
    assert places.label({"village": "Tiny", "country_code": "gb"}) == "Tiny, GB"
    assert places.label({"state": "Berlin", "city": "Berlin", "country_code": "de"}) == "Berlin, DE"
    assert places.label({"county": "Somewhere County", "state": "Ohio"}) == "Somewhere County, Ohio"
    assert places.label({}) is None


@pytest.fixture
def fresh_cache(monkeypatch):
    monkeypatch.setattr(places, "_cache", type(places._cache)())
    monkeypatch.setattr(places, "MIN_INTERVAL", 0)


async def test_place_lookups_are_cached(fresh_cache, monkeypatch):
    calls = []

    async def fetch(lat, lon):
        calls.append((lat, lon))
        return "Somewhere, XX"

    monkeypatch.setattr(places, "_fetch", fetch)
    assert await places.place_name(51.50001, -0.12001) == "Somewhere, XX"
    assert await places.place_name(51.50004, -0.12004) == "Somewhere, XX"  # within ~100 m: cached
    assert await places.place_name(52.0, -0.12) == "Somewhere, XX"
    assert calls == [(51.5, -0.12), (52.0, -0.12)]


async def test_a_failed_lookup_is_retried_later(fresh_cache, monkeypatch):
    import httpx

    calls = []

    async def fetch(lat, lon):
        calls.append(1)
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(places, "_fetch", fetch)
    assert await places.place_name(10, 10) is None
    assert await places.place_name(10, 10) is None
    assert len(calls) == 1  # remembered for FAILURE_TTL
    monkeypatch.setattr(places, "FAILURE_TTL", 0)
    assert await places.place_name(10, 10) is None
    assert len(calls) == 2
