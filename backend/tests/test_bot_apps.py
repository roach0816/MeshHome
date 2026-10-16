"""Bot apps: recipes, the engine that runs them, the catalog, and the API to manage them.

The Ecowitt app (the first catalog app) must answer /weather exactly as the built-in command did,
so its fixtures are a GW2000 shaped like Ecowitt's documented examples.
"""

import copy
import hashlib
import json
import time

import httpx
import pytest

from app.radio.base import IncomingMessage
from app.services import app_settings, bot, bot_apps, bot_recipes
from tests.conftest import csrf, do_setup, radio_adapter, wait_for
from tests.test_api import _connected_conversations

REAL_CLIENT = httpx.AsyncClient
ECOWITT = json.loads((bot_apps.BUNDLED / "ecowitt.json").read_text())

# A GW2000 with a WS90 and a WH41 PM2.5 sensor, units set to °F / mph / in.
IMPERIAL = {
    "common_list": [
        {"id": "0x02", "val": "79.2", "unit": "F"},
        {"id": "0x07", "val": "65%"},
        {"id": "3", "val": "79.2", "unit": "F"},
        {"id": "0x03", "val": "66.4", "unit": "F"},
        {"id": "0x0B", "val": "3.40mph"},
        {"id": "0x0C", "val": "6.04mph"},
        {"id": "0x19", "val": "11.41mph"},
        {"id": "0x15", "val": "512.00w/m2"},
        {"id": "0x17", "val": "4"},
        {"id": "0x0A", "val": "37", "battery": "5"},
    ],
    "piezoRain": [
        {"id": "0x0D", "val": "0.12in"},
        {"id": "0x0E", "val": "0.00in/Hr"},
        {"id": "0x7C", "val": "0.31in"},
        {"id": "0x10", "val": "0.12in"},
        {"id": "0x11", "val": "0.50in"},
        {"id": "0x13", "val": "20.37in", "battery": "4"},
    ],
    "wh25": [{"intemp": "71.0", "unit": "F", "inhumi": "40%", "abs": "29.40inHg", "rel": "29.40inHg"}],
    "ch_pm25": [{"channel": "1", "PM25": "11.0", "PM25_RealAQI": "46", "PM25_24HAQI": "55", "battery": "6"}],
}

# Older firmware (no 24-hour total), metric units, a WH46 combo sensor and no WH41.
METRIC_OLD = {
    "common_list": [
        {"id": "0x02", "val": "21.5", "unit": "C"},
        {"id": "0x07", "val": "58%"},
        {"id": "0x0B", "val": "1.5m/s"},
        {"id": "0x0C", "val": "3.1m/s"},
        {"id": "0x0A", "val": "250"},
    ],
    "piezoRain": [{"id": "0x0D", "val": "0.0 mm"}, {"id": "0x10", "val": "2.4 mm"}],
    "co2": [
        {"temp": "24.0", "unit": "C", "PM25": "13.0", "PM25_RealAQI": "53", "PM10": "13.9", "CO2": "880"}
    ],
}


@pytest.fixture(autouse=True)
def _fresh():
    bot_recipes._cache.clear()
    bot_apps._catalog = None
    yield
    bot_apps._catalog = None


def _station(monkeypatch, payload=None, status_code=200, ip="192.168.1.50"):
    """A fake gateway at station.test (on the local network unless ip says otherwise)."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if payload is None:
            raise httpx.ConnectError("unreachable", request=request)
        return httpx.Response(status_code, json=payload)

    monkeypatch.setattr(
        bot_recipes.httpx,
        "AsyncClient",
        lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **kw),
    )
    monkeypatch.setattr(bot_recipes, "_resolve", lambda host: [ip])
    bot_recipes._cache.clear()
    return seen


# ---- the engine ----------------------------------------------------------------------------------


def test_ecowitt_recipe_matches_the_old_weather_command():
    cmd = bot_recipes.load(ECOWITT).command("weather")
    text = bot_recipes.compose(cmd, IMPERIAL)
    assert text == "79.2°F 65% RH · wind 3.4 mph NE, gust 6.04 · rain 24h 0.31 in · AQI 46 PM2.5 11"
    assert bot_recipes.compose(cmd, METRIC_OLD) == (
        "21.5°C 58% RH · wind 1.5 m/s WSW, gust 3.1 · rain today 2.4 mm · AQI 53 PM2.5 13 PM10 13.9 CO2 880ppm"
    )
    cmd.reply.max_bytes = 90  # extras are dropped from the end of the droppable part to fit
    short = bot_recipes.compose(cmd, METRIC_OLD)
    assert len(short.encode()) <= 90 and "CO2" not in short and short.startswith("21.5°C")
    assert bot_recipes.compose(cmd, {"common_list": [{"id": "0x02", "val": "--"}]}) == (
        "The weather station reported no outdoor readings."
    )
    assert (
        bot_recipes.compose(cmd, ["not", "a", "dict"]) == "The weather station reported no outdoor readings."
    )


def test_paths_and_compass():
    data = {"a": [{"id": "0x0A", "val": "1"}, {"id": "x", "v": {"w": 2}}], "b": {"c": [5, 6]}}
    assert bot_recipes.get_path(data, "a[id=0x0a].val") == "1"  # ids compare without case
    assert bot_recipes.get_path(data, "a[1].v.w") == 2
    assert bot_recipes.get_path(data, "b.c[1]") == 6
    assert bot_recipes.get_path(data, "a[id=nope].val") is None
    assert bot_recipes.get_path(data, "b.c[9]") is None
    for bad in ("a..b", "a[", "a]b", ""):
        with pytest.raises(ValueError):
            bot_recipes.parse_path(bad)
    assert [bot_recipes.compass(d) for d in (0, 11, 12, 90, 180, 349, 359)] == [
        "N",
        "N",
        "NNE",
        "E",
        "S",
        "N",
        "N",
    ]


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda r: r.update(schema=4), "newer MeshHome"),
        (
            lambda r: r["sources"]["live"].update(url="http://example.com/x", network="internet"),
            "isn't in hosts",
        ),
        (lambda r: r["sources"]["live"].update(url="http://{host}.evil/x"), "host setting"),
        (lambda r: r["settings"][0].update(type="text"), "host setting"),
        (lambda r: r["commands"][0].update(name="ping"), "built into the bot"),
        (lambda r: r["commands"][0]["reply"]["parts"][0]["items"].append("{nope}"), "unknown value"),
        (lambda r: r["commands"][0]["values"]["temp"].update({"from": ["a[b"]}), "Bad path"),
        (lambda r: r["sources"]["live"].update(url="http://{host}/{secret}"), "unknown settings"),
        (lambda r: r.update(id="Bad Id"), "pattern"),
        (lambda r: r.update(category="<script>"), "pattern"),
    ],
)
def test_invalid_recipes_are_refused(change, reason):
    raw = copy.deepcopy(ECOWITT)
    change(raw)
    with pytest.raises(bot_recipes.RecipeError, match=reason):
        bot_recipes.load(raw)


def test_internet_sources_only_reach_their_hosts():
    raw = copy.deepcopy(ECOWITT)
    raw["hosts"] = ["api.example.com"]
    raw["sources"]["live"].update(url="https://api.example.com/v1?station={host}", network="internet")
    recipe = bot_recipes.load(raw)
    assert recipe.contacts() == ["contacts api.example.com"]


async def test_local_sources_stay_on_the_local_network(monkeypatch):
    recipe = bot_recipes.load(ECOWITT)
    _station(monkeypatch, IMPERIAL, ip="93.184.216.34")
    with pytest.raises(bot_recipes.SourceError, match="isn't on the local network"):
        await bot_recipes.run(recipe, "weather", {"host": "station.test"})
    with pytest.raises(bot_recipes.SourceError, match="isn't on the local network"):
        await bot_recipes.run(recipe, "weather", {"host": "8.8.8.8"})
    seen = _station(monkeypatch, IMPERIAL)
    assert (await bot_recipes.run(recipe, "weather", {"host": "station.test:8080"})).startswith("79.2°F")
    assert seen == ["http://station.test:8080/get_livedata_info"]
    _station(monkeypatch, IMPERIAL, status_code=500)
    with pytest.raises(bot_recipes.SourceError, match="HTTP 500"):
        await bot_recipes.run(recipe, "weather", {"host": "station.test"}, use_cache=False)


# ---- the catalog ---------------------------------------------------------------------------------


def _catalog_site(monkeypatch, files: dict[str, bytes]):
    base = "https://catalog.test/apps/"

    def handler(request: httpx.Request) -> httpx.Response:
        name = str(request.url).removeprefix(base)
        return httpx.Response(200, content=files[name]) if name in files else httpx.Response(404)

    monkeypatch.setattr(
        bot_apps.httpx, "AsyncClient", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **kw)
    )
    monkeypatch.setattr(bot_apps.get_settings(), "bot_apps_catalog_url", base + "catalog.json")


def _entry(raw: bytes, **extra) -> dict:
    r = json.loads(raw)
    return {
        "id": r["id"],
        "name": r["name"],
        "version": r["version"],
        "summary": r["summary"],
        "recipe": f"{r['id']}.json",
        "sha256": hashlib.sha256(raw).hexdigest(),
        **extra,
    }


async def test_online_catalog_is_checked(monkeypatch):
    good = json.dumps({**ECOWITT, "version": "1.1.0"}).encode()
    tampered = json.dumps(
        {**ECOWITT, "id": "other", "commands": [{**ECOWITT["commands"][0], "name": "other"}]}
    ).encode()
    catalog = {
        "schema": 1,
        "apps": [
            _entry(good),
            {**_entry(tampered), "sha256": "0" * 64},  # recipe doesn't match its checksum
            {
                **_entry(good),
                "id": "away",
                "recipe": "https://elsewhere.test/x.json",
            },  # not beside the catalog
        ],
    }
    _catalog_site(
        monkeypatch,
        {"catalog.json": json.dumps(catalog).encode(), "ecowitt.json": good, "other.json": tampered},
    )
    cat = await bot_apps.catalog(force=True)
    assert (
        cat.source == "online"
        and list(cat.apps) == ["ecowitt"]
        and cat.apps["ecowitt"].recipe.version == "1.1.0"
    )

    _catalog_site(monkeypatch, {})  # unreachable: the bundled copy
    cat = await bot_apps.catalog(force=True)
    assert cat.source == "bundled" and "ecowitt" in cat.apps and cat.error


# ---- managing apps -------------------------------------------------------------------------------


async def test_install_test_configure_update_remove(client, monkeypatch):
    await do_setup(client, mode="none")
    h = csrf(client)
    state = (await client.get("/api/bot/apps")).json()
    assert state["installed"] == [] and state["catalog_source"] == "bundled"
    eco = next(a for a in state["catalog"] if a["id"] == "ecowitt")
    assert eco["category"] == "Weather" and state["categories"] == ["Weather"]
    assert eco["commands"] == [{"name": "weather", "help": "local weather", "group": None}]
    assert (
        eco["contacts"] == ["contacts a device on your local network"] and eco["settings"][0]["key"] == "host"
    )

    # Try it before installing, with unsaved settings.
    seen = _station(monkeypatch, IMPERIAL)
    r = await client.post(
        "/api/bot/apps/ecowitt/test", headers=h, json={"settings": {"host": "station.test"}}
    )
    assert r.status_code == 200 and r.json()["reply"].startswith("79.2°F") and seen
    _station(monkeypatch, None)
    r = await client.post(
        "/api/bot/apps/ecowitt/test", headers=h, json={"settings": {"host": "station.test"}}
    )
    assert r.status_code == 502  # the details are shown to the owner here, never sent over the air

    assert (await client.post("/api/bot/apps/ecowitt", headers=h, json={"settings": {}})).status_code == 409
    bad = {"settings": {"host": "http://station.test/x"}}
    assert (await client.post("/api/bot/apps/ecowitt", headers=h, json=bad)).status_code == 409
    r = await client.post("/api/bot/apps/ecowitt", headers=h, json={"settings": {"host": " station.test "}})
    assert r.status_code == 200, r.text
    mine = r.json()["installed"][0]
    assert mine["values"] == {"host": "station.test"} and mine["enabled"] and mine["update"] is None
    assert (
        await client.post("/api/bot/apps/ecowitt", headers=h, json={"settings": {"host": "x"}})
    ).status_code == 409

    r = await client.patch("/api/bot/apps/ecowitt", headers=h, json={"enabled": False})
    assert r.json()["installed"][0]["enabled"] is False
    r = await client.patch(
        "/api/bot/apps/ecowitt", headers=h, json={"settings": {"host": "gw.lan:8080"}, "enabled": True}
    )
    assert r.json()["installed"][0]["values"] == {"host": "gw.lan:8080"}

    # A newer version in the catalog is offered, with what changes, and only runs once approved.
    newer = copy.deepcopy(ECOWITT)
    newer["version"] = "1.1.0"
    newer["commands"].append({**copy.deepcopy(ECOWITT["commands"][0]), "name": "rain", "help": "rain only"})
    raw = json.dumps(newer).encode()
    _catalog_site(
        monkeypatch,
        {"catalog.json": json.dumps({"schema": 1, "apps": [_entry(raw)]}).encode(), "ecowitt.json": raw},
    )
    state = (await client.post("/api/bot/apps/refresh", headers=h)).json()
    assert state["installed"][0]["version"] == ECOWITT["version"]
    assert state["installed"][0]["update"] == {"version": "1.1.0", "changes": ["New command /rain"]}
    state = (await client.post("/api/bot/apps/ecowitt/update", headers=h, json={})).json()
    assert state["installed"][0]["version"] == "1.1.0" and state["installed"][0]["update"] is None
    assert state["installed"][0]["values"] == {"host": "gw.lan:8080"}  # settings carried over

    # API keys can't manage apps.
    key = (await client.post("/api/api-keys", headers=h, json={"name": "x", "scope": "write"})).json()["key"]
    r = await client.get("/api/bot/apps", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 403

    assert (await client.delete("/api/bot/apps/ecowitt", headers=h)).json()["installed"] == []
    assert (await client.delete("/api/bot/apps/ecowitt", headers=h)).status_code == 409


async def test_commands_cant_clash(client, monkeypatch):
    await do_setup(client, mode="none")
    h = csrf(client)
    twin = copy.deepcopy(ECOWITT)
    twin.update(id="twin", name="Twin station")
    raw_twin, raw_eco = json.dumps(twin).encode(), json.dumps(ECOWITT).encode()
    _catalog_site(
        monkeypatch,
        {
            "catalog.json": json.dumps({"schema": 1, "apps": [_entry(raw_eco), _entry(raw_twin)]}).encode(),
            "ecowitt.json": raw_eco,
            "twin.json": raw_twin,
        },
    )
    assert (
        await client.post("/api/bot/apps/ecowitt", headers=h, json={"settings": {"host": "a.lan"}})
    ).status_code == 200
    r = await client.post("/api/bot/apps/twin", headers=h, json={"settings": {"host": "b.lan"}})
    assert r.status_code == 409 and "/weather is already used by Ecowitt" in r.json()["detail"]


async def test_weather_station_from_before_apps_is_carried_over(client):
    from app import db

    await do_setup(client, mode="none")
    async with db.session_factory()() as s:
        await s.execute(
            app_settings.AppSetting.__table__.delete().where(
                app_settings.AppSetting.key == bot_apps.STORE_KEY
            )
        )
        await app_settings.put_weather_config(s, app_settings.WeatherConfig(host="gw2000.lan"))
        await bot_apps.migrate(s)
        await bot_apps.migrate(s)  # once only
        await s.commit()
        apps = await bot_apps.installed(s)
    assert [(a.id, a.settings, a.enabled) for a in apps] == [("ecowitt", {"host": "gw2000.lan"}, True)]


# ---- the bot --------------------------------------------------------------------------------------


async def test_bot_answers_with_apps(client, monkeypatch):
    from app.radio import simulated

    simulated._SIM_STATE = None
    bot._last_reply.clear()
    bot._recent.clear()
    await do_setup(client)
    await _connected_conversations(client)
    h = csrf(client)
    await client.put("/api/settings/bot", headers=h, json={"enabled": True, "allow": "everyone"})

    async def bot_replies() -> list[str]:
        convs = (await client.get("/api/conversations")).json()
        conv = next((c for c in convs if c["title"].startswith("Tracker")), None)
        if conv is None:
            return []
        msgs = (await client.get(f"/api/conversations/{conv['id']}/messages?limit=200")).json()["messages"]
        return [m["body"] for m in msgs if m["direction"] == "out" and m["meta"].get("bot")]

    async def ask(text: str) -> str:
        bot._last_reply.clear()
        before = len(await bot_replies())
        (await radio_adapter()).inject(
            IncomingMessage(
                kind="dm",
                text=text,
                pubkey_prefix=simulated._key("tracker")[:12],
                sender_timestamp=int(time.time()),
            )
        )

        async def new_reply():
            replies = await bot_replies()
            return replies[-1] if len(replies) > before else None

        return await wait_for(new_reply)

    assert await ask("/weather") == "Unknown command /weather. Try /help"
    assert "/weather" not in await ask("/help")

    _station(monkeypatch, IMPERIAL)
    assert (
        await client.post("/api/bot/apps/ecowitt", headers=h, json={"settings": {"host": "station.test"}})
    ).status_code == 200
    # (Each command differs: identical messages within a second are treated as one, like radio repeats.)
    assert (await ask("/weather please")).startswith("79.2°F 65% RH · wind 3.4 mph NE")
    assert "/weather (local weather)" in await ask("/help again")

    _station(monkeypatch, None)
    assert await ask("/Weather") == "The weather station is not answering right now."  # no address on air

    await client.patch("/api/bot/apps/ecowitt", headers=h, json={"enabled": False})
    assert await ask("/weather now") == "Unknown command /weather. Try /help"


async def test_categories_come_from_the_catalogs_apps(client, monkeypatch):
    await do_setup(client, mode="none")
    apps = {}
    for app_id, category in (("ecowitt", "Weather"), ("lights", "Home automation"), ("misc", None)):
        r = copy.deepcopy(ECOWITT)
        r.update(id=app_id, name=app_id.title())
        r["commands"][0]["name"] = app_id
        if category:
            r["category"] = category
        else:
            r.pop("category", None)
        apps[app_id] = json.dumps(r).encode()
    catalog = {"schema": 1, "apps": [_entry(raw) for raw in apps.values()]}
    _catalog_site(
        monkeypatch,
        {"catalog.json": json.dumps(catalog).encode(), **{f"{k}.json": v for k, v in apps.items()}},
    )
    state = (await client.post("/api/bot/apps/refresh", headers=csrf(client))).json()
    assert state["categories"] == ["Home automation", "Weather", "Other"]  # Other last
    assert {a["id"]: a["category"] for a in state["catalog"]} == {
        "ecowitt": "Weather",
        "lights": "Home automation",
        "misc": "Other",
    }
