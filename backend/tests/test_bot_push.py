"""Push sources: a device on the local network sends readings to /push/<token>.

The fixture app is a weather console that uploads to a "customized server". Its uploads are shaped
like a request captured from such a console, which glues the readings straight onto the configured
path: "GET /data/stationtype=<model>&PASSKEY=<MAC>&dateutc=2021-03-19+20:20:12&tempinf=70.3…"
"""

import copy
import json
import logging
import time

import httpx
import pytest

from app.main import app
from app.radio.base import IncomingMessage
from app.services import bot, bot_apps, bot_recipes, push_inbox
from tests.conftest import csrf, do_setup, radio_adapter, wait_for
from tests.test_api import _connected_conversations
from tests.test_bot_apps import REAL_CLIENT, _catalog_site, _entry

PASSKEY = "AA:BB:CC:DD:EE:FF"
READINGS = (
    f"stationtype=ConsoleV4.2.9&PASSKEY={PASSKEY}&dateutc=2021-03-19+20:20:12&tempf=68.2&humidity=55"
    "&winddir=225&windspeedmph=3.4&windgustmph=8.1&dailyrainin=0.000&baromrelin=29.92&uv=3"
)

CONSOLE = {
    "schema": 2,
    "id": "weather-console",
    "name": "Weather console (local)",
    "version": "1.0.0",
    "category": "Weather",
    "summary": "Weather your console sends to MeshHome on your network.",
    "settings": [],
    "sources": {
        "station": {
            "network": "push",
            "format": "query",
            "max_age": 300,
            "help": "On the console: Customized Server → On, then the server, port and path below.",
        }
    },
    "commands": [
        {
            "name": "weather",
            "help": "local weather",
            "source": "station",
            "values": {
                "temp": {"from": ["tempf"]},
                "humidity": {"from": ["humidity"]},
                "wind": {"from": ["windspeedmph"]},
                "gust": {"from": ["windgustmph"]},
                "wind_dir": {"from": ["winddir"], "format": "compass"},
                "rain": {"from": [{"path": "dailyrainin", "label": "today"}]},
            },
            "reply": {
                "parts": [
                    {"items": ["{temp}°F", "{humidity}% RH"]},
                    {"items": ["wind {wind} mph[ {wind_dir}][, gust {gust}]"]},
                    {"items": ["rain {rain.label} {rain} in"]},
                ],
                "max_bytes": 150,
                "empty": "The weather station sent no outdoor readings.",
                "unavailable": "No reading from the weather station in the last 5 minutes.",
            },
        }
    ],
}
EXPECTED = "68.2°F 55% RH · wind 3.4 mph SW, gust 8.1 · rain today 0 in"


@pytest.fixture(autouse=True)
def _fresh():
    push_inbox._latest.clear()
    bot_apps._catalog = None
    bot_apps._tokens = None
    yield
    push_inbox._latest.clear()
    bot_apps._catalog = None


def _device(peer: str = "192.168.1.77") -> httpx.AsyncClient:
    """A device on the network (or elsewhere), talking to MeshHome without a session."""
    return REAL_CLIENT(transport=httpx.ASGITransport(app=app, client=(peer, 40000)), base_url="http://test")


async def _install(client, monkeypatch, recipe=CONSOLE) -> str:
    raw = json.dumps(recipe).encode()
    rid = recipe["id"]
    files = {"catalog.json": json.dumps({"schema": 1, "apps": [_entry(raw)]}).encode(), f"{rid}.json": raw}
    _catalog_site(monkeypatch, files)
    r = await client.post(f"/api/bot/apps/{rid}", headers=csrf(client), json={"settings": {}})
    assert r.status_code == 200, r.text
    push = next(a for a in r.json()["installed"] if a["id"] == rid)["push"]
    assert push["path"].startswith("/push/") and push["received_at"] is None and push["max_age"] == 300
    token = push["path"].removeprefix("/push/")
    assert len(token) == 32
    return token


async def _reply(client, app_id="weather-console") -> httpx.Response:
    return await client.post(f"/api/bot/apps/{app_id}/test", headers=csrf(client), json={})


# ---- the recipe format ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda r: r.update(schema=1), "recipe format 2"),
        (lambda r: r["sources"]["station"].update(url="http://x/y"), "can't set url"),
        (lambda r: r["sources"]["station"].update(timeout=3), "can't set timeout"),
        (lambda r: r["sources"]["station"].update(format="xml"), "'query' or 'json'"),
        (lambda r: r["sources"]["station"].update(max_age=5), "greater than or equal to 10"),
        (lambda r: r["sources"].update(other={"network": "push"}), "one push source"),
        (lambda r: r.update(schema=4), "newer MeshHome"),
    ],
)
def test_push_source_rules(change, reason):
    raw = copy.deepcopy(CONSOLE)
    change(raw)
    with pytest.raises(bot_recipes.RecipeError, match=reason):
        bot_recipes.load(raw)


def test_push_and_request_sources_can_mix():
    raw = copy.deepcopy(CONSOLE)
    raw["settings"] = [{"key": "host", "label": "Gateway", "type": "host"}]
    raw["sources"]["live"] = {"url": "http://{host}/x", "network": "local"}
    recipe = bot_recipes.load(raw)
    assert recipe.contacts() == [
        "contacts a device on your local network",
        "receives data from a device on your local network",
    ]
    bad = copy.deepcopy(raw)
    bad["sources"]["live"]["max_age"] = 60  # push-only field on a request source
    with pytest.raises(bot_recipes.RecipeError, match="Only a push source"):
        bot_recipes.load(bad)


def test_decoding():
    q = bot_recipes.load(CONSOLE).sources["station"]
    data = bot_recipes.decode_upload(
        q, "?&a=1&b=2&a=3&dateutc=2021-03-19+20:20:12", b"c=4", "application/x-www-form-urlencoded"
    )
    assert data == {"a": "3", "b": "2", "dateutc": "2021-03-19 20:20:12", "c": "4"}
    with pytest.raises(ValueError):
        bot_recipes.decode_upload(q, "", b"", "")
    j = bot_recipes.Source(network="push", format="json")
    assert bot_recipes.decode_upload(j, "", b'{"t": {"v": 1}}', "application/json") == {"t": {"v": 1}}
    for bad in (b"not json", b'"a string"'):
        with pytest.raises(ValueError):
            bot_recipes.decode_upload(j, "", bad, "application/json")


# ---- receiving uploads ---------------------------------------------------------------------------


async def test_uploads_feed_the_command(client, monkeypatch, caplog):
    await do_setup(client, mode="none")
    token = await _install(client, monkeypatch)

    r = await _reply(client)
    assert r.status_code == 502 and "Nothing has been received" in r.json()["detail"]

    caplog.set_level(logging.DEBUG)
    async with _device() as dev:
        # As captured: the readings glued straight onto the path (configured without "?").
        r = await dev.get(f"/push/{token}{READINGS}")
        assert (r.status_code, r.text) == (200, "ok")
    assert (await _reply(client)).json()["reply"] == EXPECTED
    state = (await client.get("/api/bot/apps")).json()
    assert state["installed"][0]["push"]["received_at"]
    # Device keys never reach the logs.
    logged = "\n".join(rec.getMessage() for rec in caplog.records if not rec.name.startswith("httpx"))
    assert PASSKEY not in logged and "tempf" not in logged and token not in logged


@pytest.mark.parametrize("sep", ["", "/", "?", "?&", "&", "/?"])
async def test_lenient_paths(client, monkeypatch, sep):
    await do_setup(client, mode="none")
    token = await _install(client, monkeypatch)
    async with _device() as dev:
        assert (await dev.get(f"/push/{token}{sep}{READINGS}")).status_code == 200
    assert (await _reply(client)).json()["reply"] == EXPECTED


async def test_form_post(client, monkeypatch):
    await do_setup(client, mode="none")
    token = await _install(client, monkeypatch)
    async with _device() as dev:
        r = await dev.post(
            f"/push/{token}", content=READINGS, headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
    assert r.status_code == 200 and (await _reply(client)).json()["reply"] == EXPECTED


async def test_json_uploads(client, monkeypatch):
    await do_setup(client, mode="none")
    recipe = copy.deepcopy(CONSOLE)
    recipe.update(id="sensor-json", name="JSON sensor")
    recipe["sources"]["station"]["format"] = "json"
    recipe["commands"][0]["values"] = {"temp": {"from": ["outdoor.temp"]}}
    recipe["commands"][0]["reply"]["parts"] = [{"items": ["{temp}°F"]}]
    token = await _install(client, monkeypatch, recipe)
    async with _device() as dev:
        assert (await dev.post(f"/push/{token}", json={"outdoor": {"temp": 71.5}})).status_code == 200
        assert (await dev.post(f"/push/{token}?x=1")).status_code == 400  # not JSON
    assert (await _reply(client, "sensor-json")).json()["reply"] == "71.5°F"


async def test_only_the_local_network_and_known_addresses(client, monkeypatch):
    await do_setup(client, mode="none")
    token = await _install(client, monkeypatch)
    async with _device("93.184.216.34") as dev:
        assert (await dev.get(f"/push/{token}?{READINGS}")).status_code == 403
    async with _device() as dev:
        # Relayed by a proxy that MeshHome doesn't trust, from a public address.
        r = await dev.get(f"/push/{token}?{READINGS}", headers={"X-Forwarded-For": "8.8.8.8, 10.0.0.2"})
        assert r.status_code == 403
        r = await dev.get(f"/push/{token}?{READINGS}", headers={"X-Forwarded-For": "192.168.1.20"})
        assert r.status_code == 200
        assert (await dev.get(f"/push/{'x' * 32}?{READINGS}")).status_code == 404
        assert (await dev.get(f"/push/short?{READINGS}")).status_code == 404
        assert (await dev.get("/push/")).status_code == 404
    async with _device("fe80::1") as dev:
        push_inbox._latest.clear()
        assert (await dev.get(f"/push/{token}?{READINGS}")).status_code == 200


async def test_size_and_rate_limits(client, monkeypatch):
    await do_setup(client, mode="none")
    token = await _install(client, monkeypatch)
    async with _device() as dev:
        assert (await dev.get(f"/push/{token}?{READINGS}&pad={'x' * 17000}")).status_code == 413
        r = await dev.post(
            f"/push/{token}",
            content=b"a=" + b"x" * 17000,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert r.status_code == 413
        assert (await dev.get(f"/push/{token}?{READINGS}")).status_code == 200
        # A second upload within a second is dropped quietly, still answered with ok.
        r = await dev.get(f"/push/{token}?tempf=99.9&humidity=10")
        assert (r.status_code, r.text) == (200, "ok")
    assert (await _reply(client)).json()["reply"] == EXPECTED


async def test_stale_readings(client, monkeypatch):
    await do_setup(client, mode="none")
    token = await _install(client, monkeypatch)
    async with _device() as dev:
        await dev.get(f"/push/{token}?{READINGS}")
    mono, wall, data = push_inbox._latest["weather-console"]
    push_inbox._latest["weather-console"] = (mono - 301, wall - 301, data)
    r = await _reply(client)
    assert r.status_code == 502 and "last 300 seconds" in r.json()["detail"]


async def test_new_address_and_removal(client, monkeypatch):
    await do_setup(client, mode="none")
    old = await _install(client, monkeypatch)
    state = (await client.post("/api/bot/apps/weather-console/push-token", headers=csrf(client))).json()
    new = state["installed"][0]["push"]["path"].removeprefix("/push/")
    assert new != old and len(new) == 32
    async with _device() as dev:
        assert (await dev.get(f"/push/{old}?{READINGS}")).status_code == 404
        assert (await dev.get(f"/push/{new}?{READINGS}")).status_code == 200
    assert (await client.post("/api/bot/apps/weather-console/push-token")).status_code == 403  # no CSRF
    await client.delete("/api/bot/apps/weather-console", headers=csrf(client))
    async with _device() as dev:
        assert (await dev.get(f"/push/{new}?{READINGS}")).status_code == 404
    assert "weather-console" not in push_inbox._latest


def test_an_update_that_adds_a_push_source_says_so():
    old = bot_recipes.load(json.loads((bot_apps.BUNDLED / "ecowitt.json").read_text()))
    raw = json.loads((bot_apps.BUNDLED / "ecowitt.json").read_text())
    raw.update(schema=2, version="2.0.0")
    raw["sources"]["station"] = {"network": "push"}
    assert "Will also receive data from a device on your local network" in bot_apps.changes(
        old, bot_recipes.load(raw)
    )


# ---- over the mesh -------------------------------------------------------------------------------


async def test_weather_over_the_mesh(client, monkeypatch):
    from app.radio import simulated

    simulated._SIM_STATE = None
    bot._last_reply.clear()
    bot._recent.clear()
    await do_setup(client)
    await _connected_conversations(client)
    await client.put("/api/settings/bot", headers=csrf(client), json={"enabled": True, "allow": "everyone"})
    token = await _install(client, monkeypatch)

    async def ask(text: str) -> str:
        async def replies():
            convs = (await client.get("/api/conversations")).json()
            conv = next((c for c in convs if c["title"].startswith("Tracker")), None)
            if conv is None:
                return []
            msgs = (await client.get(f"/api/conversations/{conv['id']}/messages?limit=200")).json()[
                "messages"
            ]
            return [m["body"] for m in msgs if m["direction"] == "out" and m["meta"].get("bot")]

        bot._last_reply.clear()
        before = len(await replies())
        (await radio_adapter()).inject(
            IncomingMessage(
                kind="dm",
                text=text,
                pubkey_prefix=simulated._key("tracker")[:12],
                sender_timestamp=int(time.time()),
            )
        )

        async def new():
            r = await replies()
            return r[-1] if len(r) > before else None

        return await wait_for(new)

    assert await ask("/weather") == "No reading from the weather station in the last 5 minutes."
    async with _device() as dev:
        assert (await dev.get(f"/push/{token}/{READINGS}")).status_code == 200
    assert await ask("/weather now") == EXPECTED


def test_access_log_hides_upload_paths():
    from app.api.push_receiver import HidePushPaths

    rec = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("192.168.1.77:40000", "GET", f"/push/{'t' * 32}?{READINGS}", "1.1", 200),
        None,
    )
    assert (
        HidePushPaths().filter(rec) and rec.getMessage() == '192.168.1.77:40000 - "GET /push/… HTTP/1.1" 200'
    )
    other = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, "%s %s %s", ("a", "GET", "/api/x"), None
    )
    assert HidePushPaths().filter(other) and other.getMessage() == "a GET /api/x"
