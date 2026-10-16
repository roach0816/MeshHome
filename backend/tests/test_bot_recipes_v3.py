"""Recipe format 3: groups and command arguments, Basic auth and raw headers, self-signed local
HTTPS, select settings, several sources per command, the extended path language, per-source size
limits, and logins. Fixtures are shaped like the apps waiting on them (Home Assistant, AdGuard Home,
Pi-hole v6, ESPN, dump1090-fa)."""

import base64
import copy
import json

import httpx
import pytest

from app.services import bot, bot_apps, bot_recipes

REAL_CLIENT = httpx.AsyncClient


@pytest.fixture(autouse=True)
def _fresh():
    bot_recipes._cache.clear()
    bot_recipes._sessions.clear()
    yield
    bot_recipes._cache.clear()
    bot_recipes._sessions.clear()


def _server(monkeypatch, handler, ip="192.168.1.50"):
    """Every request goes to handler(request); returns the requests seen and each client's options."""
    seen: list[httpx.Request] = []
    clients: list[dict] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def client(**kw):
        clients.append(kw)
        return REAL_CLIENT(transport=httpx.MockTransport(record), **kw)

    monkeypatch.setattr(bot_recipes.httpx, "AsyncClient", client)
    monkeypatch.setattr(bot_recipes, "_resolve", lambda host: [ip])
    return seen, clients


def _recipe(**fields) -> dict:
    base = {"schema": 3, "id": "test", "name": "Test", "version": "1.0.0", "summary": "A test app"}
    return {**base, **fields}


# ---- 1, 2: command arguments and groups ---------------------------------------------------------------

HA = _recipe(
    id="home-assistant-groups",
    name="Home Assistant groups",
    settings=[
        {"key": "host", "label": "Address", "type": "host"},
        {"key": "token", "label": "Access token", "type": "secret"},
        {
            "key": "groups",
            "label": "Entity groups",
            "type": "groups",
            "placeholder": "garage: cover.garage_left, cover.garage_right",
        },
    ],
    sources={
        "state": {
            "url": "http://{host}/api/states/{item}",
            "network": "local",
            "headers": {"Authorization": "Bearer {token}"},
        }
    },
    commands=[
        {
            "name": "ha",
            "help": "a group of entities",
            "source": "state",
            "group": "groups",
            "values": {
                "name": {"from": ["attributes.friendly_name"], "format": "text"},
                "state": {"from": ["state"], "format": "text"},
                "unit": {"from": ["attributes.unit_of_measurement"], "format": "text"},
            },
            "reply": {
                "each": "{name}: {state}[ {unit}]",
                "join": " · ",
                "empty": "No readings for that group.",
                "unavailable": "Home Assistant is not answering right now.",
            },
        }
    ],
)
HA_SETTINGS = {
    "host": "ha.test:8123",
    "token": "abc.def",
    "groups": "garage: cover.garage_left, cover.garage_right\nclimate: sensor.temp",
}
STATES = {
    "cover.garage_left": {"state": "closed", "attributes": {"friendly_name": "Left garage"}},
    "cover.garage_right": {"state": "open", "attributes": {"friendly_name": "Right garage"}},
    "sensor.temp": {"state": "21.5", "attributes": {"friendly_name": "Hall", "unit_of_measurement": "°C"}},
}


def _home_assistant(monkeypatch, down=()):
    def handler(request: httpx.Request) -> httpx.Response:
        entity = request.url.path.removeprefix("/api/states/")
        if entity in down or entity not in STATES:
            return httpx.Response(500)
        assert request.headers["Authorization"] == "Bearer abc.def"
        return httpx.Response(200, json=STATES[entity])

    return _server(monkeypatch, handler)


async def test_group_command_reads_each_item(monkeypatch):
    recipe = bot_recipes.load(HA)
    seen, _ = _home_assistant(monkeypatch)
    reply = await bot_recipes.run(recipe, "ha", HA_SETTINGS, "garage")
    assert reply == "Left garage: closed · Right garage: open"
    assert sorted(r.url.path for r in seen) == [
        "/api/states/cover.garage_left",
        "/api/states/cover.garage_right",
    ]
    assert await bot_recipes.run(recipe, "ha", HA_SETTINGS, "climate") == "Hall: 21.5 °C"


async def test_group_command_without_or_with_an_unknown_group(monkeypatch):
    recipe = bot_recipes.load(HA)
    seen, _ = _home_assistant(monkeypatch)
    assert await bot_recipes.run(recipe, "ha", HA_SETTINGS) == "Groups: garage, climate"
    assert (
        await bot_recipes.run(recipe, "ha", HA_SETTINGS, "nope") == "No group nope. Groups: garage, climate"
    )
    # The word is only compared with group names: never fetched, never expanded.
    for arg in ("../config", "cover.x%2f..", "cover.garage_left", "{groups}"):
        reply = await bot_recipes.run(recipe, "ha", HA_SETTINGS, arg)
        assert reply == f"No group {arg}. Groups: garage, climate"
    assert seen == []


async def test_group_command_partial_and_failed(monkeypatch):
    recipe = bot_recipes.load(HA)
    _home_assistant(monkeypatch, down={"cover.garage_right"})
    assert await bot_recipes.run(recipe, "ha", HA_SETTINGS, "garage") == "Left garage: closed"
    bot_recipes._cache.clear()
    _home_assistant(monkeypatch, down=set(STATES))
    with pytest.raises(bot_recipes.SourceError):
        await bot_recipes.run(recipe, "ha", HA_SETTINGS, "garage")
    # Every response answers, but none fills the template: the empty reply.
    _server(monkeypatch, lambda r: httpx.Response(200, json={"unrelated": 1}))
    bot_recipes._cache.clear()
    assert await bot_recipes.run(recipe, "ha", HA_SETTINGS, "garage") == "No readings for that group."


async def test_group_reply_drops_items_to_fit(monkeypatch):
    raw = copy.deepcopy(HA)
    raw["commands"][0]["reply"]["max_bytes"] = 30
    _home_assistant(monkeypatch)
    assert await bot_recipes.run(bot_recipes.load(raw), "ha", HA_SETTINGS, "garage") == "Left garage: closed"


async def test_group_items_are_url_encoded(monkeypatch):
    seen, _ = _server(monkeypatch, lambda r: httpx.Response(200, json={"state": "on", "attributes": {}}))
    raw = copy.deepcopy(HA)
    raw["commands"][0]["reply"]["each"] = "{state}"
    await bot_recipes.run(bot_recipes.load(raw), "ha", {**HA_SETTINGS, "groups": "x: zwave:node-1"}, "x")
    assert str(seen[0].url) == "http://ha.test:8123/api/states/zwave%3Anode-1"


def test_groups_setting_is_checked_and_tidied():
    recipe = bot_recipes.load(HA)
    out = bot_recipes.check_settings(recipe, {**HA_SETTINGS, "groups": " Garage :a.b,c \n\n  Hall: d "})
    assert out["groups"] == "garage: a.b, c\nhall: d"
    assert bot_recipes.parse_groups(out["groups"]) == {"garage": ["a.b", "c"], "hall": ["d"]}
    for bad, reason in [
        ("garage a, b", "colon"),
        ("gar age: a", "colon"),
        ("x" * 21 + ": a", "colon"),
        ("a: x\na: y", "twice"),
        ("a:", "no IDs"),
        ("a: " + ", ".join("x" * 7), "more than 6"),
        ("a: x/y", "isn't an ID"),
        ("\n".join(f"g{i}: x" for i in range(11)), "at most 10"),
        ("a: " + "x" * 101, "isn't an ID"),
    ]:
        with pytest.raises(ValueError, match=reason):
            bot_recipes.check_settings(recipe, {**HA_SETTINGS, "groups": bad})
    long = "\n".join(f"g{i}: " + ", ".join(f"sensor.{'x' * 50}{j}" for j in range(6)) for i in range(10))
    assert 253 < len(long) < 4000 and bot_recipes.check_settings(recipe, {**HA_SETTINGS, "groups": long})
    with pytest.raises(ValueError, match="too long"):
        bot_recipes.check_settings(recipe, {**HA_SETTINGS, "groups": long + "x" * 4000})


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda r: r.update(schema=2), "needs recipe format 3"),
        (lambda r: r["settings"].append({"key": "item", "label": "Item"}), "reserved"),
        (lambda r: r["commands"][0].update(group="host"), "groups setting"),
        (lambda r: r["commands"][0]["reply"].pop("each"), "each, not parts"),
        (lambda r: r["commands"][0]["reply"].update(parts=[{"items": ["{name}"]}]), "each, not parts"),
        (lambda r: r["commands"][0]["reply"].update(no_group="Try {name}"), "only use {groups} and {arg}"),
        (lambda r: r["commands"][0].pop("group"), "for group commands"),
        (
            lambda r: r["sources"]["state"].update(url="http://{host}/api/{groups}"),
            "a command's group reads it",
        ),
        (
            lambda r: r["commands"].append(
                {**r["commands"][0], "name": "hb", "group": None, "reply": {"parts": [{"items": ["{name}"]}]}}
            ),
            "only a source read by group commands",
        ),
        (lambda r: r["commands"][0].update(source=None, sources=["state"]), "reads one source"),
    ],
)
def test_invalid_group_recipes_are_refused(change, reason):
    raw = copy.deepcopy(HA)
    change(raw)
    with pytest.raises(bot_recipes.RecipeError, match=reason):
        bot_recipes.load(raw)


def test_parse_keeps_the_argument_for_groups():
    assert bot.parse("/HA Garage please") == ("ha", "garage")
    assert bot.parse("/ha") == ("ha", None)
    assert bot_recipes.argument("  ../config x") == "../config"


# ---- 3: Basic auth and header values as written --------------------------------------------------------

ADGUARD = _recipe(
    id="adguard-home",
    name="AdGuard Home",
    settings=[
        {"key": "host", "label": "Address", "type": "host"},
        {"key": "user", "label": "Username"},
        {"key": "password", "label": "Password", "type": "secret"},
    ],
    sources={
        "stats": {
            "url": "http://{host}/control/stats",
            "network": "local",
            "auth": {"basic": {"username": "{user}", "password": "{password}"}},
        }
    },
    commands=[
        {
            "name": "adguard",
            "help": "DNS filtering",
            "source": "stats",
            "values": {
                "queries": {"from": ["num_dns_queries"]},
                "blocked": {"from": ["num_blocked_filtering"]},
                "ms": {"from": ["avg_processing_time"]},
            },
            "reply": {"parts": [{"items": ["{queries} queries", "{blocked} blocked", "[{ms} s avg]"]}]},
        }
    ],
)


async def test_basic_auth_adguard(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        expected = "Basic " + base64.b64encode(b"admin:p@ss+/=w").decode()
        if request.headers.get("Authorization") != expected:
            return httpx.Response(401)
        return httpx.Response(
            200, json={"num_dns_queries": 12345, "num_blocked_filtering": 678, "avg_processing_time": 0.0123}
        )

    _server(monkeypatch, handler)
    settings = {"host": "10.0.0.2", "user": "admin", "password": "p@ss+/=w"}
    recipe = bot_recipes.load(ADGUARD)
    assert await bot_recipes.run(recipe, "adguard", settings) == "12345 queries 678 blocked 0.01 s avg"
    assert recipe.contacts() == ["contacts a device on your local network"]


async def test_headers_take_settings_as_written_and_refuse_control_characters(monkeypatch):
    raw = _recipe(
        settings=[{"key": "host", "label": "Address", "type": "host"}, {"key": "token", "label": "Token"}],
        sources={
            "nodes": {
                "url": "https://{host}/api2/json/nodes",
                "network": "local",
                "verify_tls": False,
                "headers": {"Authorization": "PVEAPIToken={token}"},
            }
        },
        commands=[
            {
                "name": "pve",
                "help": "Proxmox",
                "source": "nodes",
                "values": {"n": {"from": ["data"], "format": "count"}},
                "reply": {"parts": [{"items": ["{n} nodes"]}]},
            }
        ],
    )
    seen, clients = _server(monkeypatch, lambda r: httpx.Response(200, json={"data": [{}, {}]}))
    recipe = bot_recipes.load(raw)
    token = "root@pam!mesh=1b2c-3d4e"
    assert await bot_recipes.run(recipe, "pve", {"host": "pve.lan:8006", "token": token}) == "2 nodes"
    assert seen[0].headers["Authorization"] == f"PVEAPIToken={token}"
    assert clients[0]["verify"] is False
    assert recipe.contacts() == [
        "contacts a device on your local network (accepts its self-signed certificate)"
    ]
    with pytest.raises(ValueError, match="control characters"):
        bot_recipes.check_settings(recipe, {"host": "pve.lan", "token": "a\r\nX-Evil: 1"})
    with pytest.raises(bot_recipes.SourceError, match="control character"):  # stored before the check
        await bot_recipes.run(recipe, "pve", {"host": "pve.lan", "token": "a\r\nX-Evil: 1"}, use_cache=False)


async def test_format_2_headers_are_still_url_encoded(monkeypatch):
    raw = _recipe(
        schema=2,
        settings=[{"key": "host", "label": "Address", "type": "host"}, {"key": "key", "label": "Key"}],
        sources={"s": {"url": "http://{host}/x", "network": "local", "headers": {"X-Key": "{key}"}}},
        commands=[
            {
                "name": "xx",
                "help": "x",
                "source": "s",
                "values": {"v": {"from": ["v"]}},
                "reply": {"parts": [{"items": ["{v}"]}]},
            }
        ],
    )
    seen, clients = _server(monkeypatch, lambda r: httpx.Response(200, json={"v": 1}))
    await bot_recipes.run(bot_recipes.load(raw), "xx", {"host": "10.0.0.2", "key": "a+b/c="})
    assert seen[0].headers["X-Key"] == "a%2Bb%2Fc%3D" and clients[0]["verify"] is True


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda r: r.update(schema=2), "needs recipe format 3"),
        (
            lambda r: r["sources"]["stats"].update(headers={"authorization": "Bearer x"}),
            "both auth and an Authorization header",
        ),
        (
            lambda r: r["sources"]["stats"]["auth"]["basic"].update(password="{nope}"),
            "unknown settings: nope",
        ),
        (
            lambda r: (r.update(hosts=["a.example"]), r["sources"]["stats"].update(network="internet")),
            "isn't in hosts",
        ),
    ],
)
def test_invalid_auth_recipes_are_refused(change, reason):
    raw = copy.deepcopy(ADGUARD)
    change(raw)
    with pytest.raises(bot_recipes.RecipeError, match=reason):
        bot_recipes.load(raw)


# ---- 4: self-signed HTTPS ------------------------------------------------------------------------------


def test_verify_tls_is_only_for_local_sources():
    raw = _recipe(
        hosts=["api.example.com"],
        sources={"s": {"url": "https://api.example.com/x", "network": "internet", "verify_tls": False}},
        commands=[
            {
                "name": "x",
                "help": "x",
                "source": "s",
                "values": {"v": {"from": ["v"]}},
                "reply": {"parts": [{"items": ["{v}"]}]},
            }
        ],
    )
    with pytest.raises(bot_recipes.RecipeError, match="Only a local source"):
        bot_recipes.load(raw)


def test_update_shows_new_trust_and_sign_in():
    old = bot_recipes.load(ADGUARD)
    raw = copy.deepcopy(ADGUARD)
    raw["sources"]["stats"]["verify_tls"] = False
    assert bot_apps.changes(old, bot_recipes.load(raw)) == [
        "Will accept the device's self-signed certificate"
    ]
    assert bot_apps.changes(bot_recipes.load(raw), old) == []
    assert "Will also sign in to the device with your password" in bot_apps.changes(
        bot_recipes.load(HA), bot_recipes.load(PIHOLE)
    )
    changed = copy.deepcopy(PIHOLE)
    changed["logins"]["pihole"]["body"] = {"password": "{password}", "totp": "{password}"}
    assert bot_apps.changes(bot_recipes.load(PIHOLE), bot_recipes.load(changed)) == [
        "Changes how it signs in to the device"
    ]


# ---- 5, 6: select settings and several sources (ESPN) ------------------------------------------------

SPORTS = _recipe(
    id="sports",
    name="Sports scores",
    hosts=["site.api.espn.com"],
    settings=[
        {
            "key": "league",
            "label": "Sport",
            "type": "select",
            "options": [
                {"value": "football/nfl", "label": "NFL"},
                {"value": "hockey/nhl", "label": "NHL"},
            ],
        },
        {
            "key": "team",
            "label": "Team",
            "type": "select",
            "depends_on": "league",
            "options_by": {
                "football/nfl": [{"value": "kc", "label": "Kansas City Chiefs"}],
                "hockey/nhl": [{"value": "bos", "label": "Boston Bruins"}],
            },
        },
    ],
    sources={
        "team": {
            "url": "https://site.api.espn.com/apis/site/v2/sports/{league}/teams/{team}",
            "network": "internet",
        },
        "schedule": {
            "url": "https://site.api.espn.com/apis/site/v2/sports/{league}/teams/{team}/schedule",
            "network": "internet",
            "max_kb": 2048,
        },
    },
    commands=[
        {
            "name": "score",
            "help": "my team",
            "sources": ["team", "schedule"],
            "values": {
                **{
                    k: {
                        "source": "schedule",
                        "from": [
                            "events[last:competitions.0.status.type.completed=true].competitions[0]." + path
                        ],
                        "format": "text",
                    }
                    for k, path in {
                        "away": "competitors[homeAway=away].team.abbreviation",
                        "away_score": "competitors[homeAway=away].score.displayValue",
                        "home": "competitors[homeAway=home].team.abbreviation",
                        "home_score": "competitors[homeAway=home].score.displayValue",
                        "final": "status.type.shortDetail",
                    }.items()
                },
                "next": {"from": ["team.nextEvent[0].shortName"], "format": "text"},
                "when": {
                    "from": ["team.nextEvent[0].competitions[0].status.type.shortDetail"],
                    "format": "text",
                },
                "state": {
                    "from": ["team.nextEvent[0].competitions[0].status.type.state"],
                    "format": "text",
                    "map": {"pre": "Next", "in": "Now", "post": "Ended"},
                },
                "record": {"from": ["team.record.items[0].summary"], "format": "text"},
            },
            "reply": {
                "parts": [
                    {"items": ["Last: {away} {away_score} @ {home} {home_score} {final}"]},
                    {"items": ["{state}: {next} {when}"]},
                    {"items": ["{record}"]},
                ]
            },
        }
    ],
)


def _game(short, away, home, away_score=None, home_score=None, completed=True, detail="Final"):
    def side(where, abbr, score):
        out = {"homeAway": where, "team": {"abbreviation": abbr}}
        if score is not None:
            out["score"] = {"displayValue": str(score)}
        return out

    return {
        "shortName": short,
        "competitions": [
            {
                "status": {"type": {"completed": completed, "shortDetail": detail}},
                "competitors": [side("home", home, home_score), side("away", away, away_score)],
            }
        ],
    }


TEAM = {
    "team": {
        "displayName": "Kansas City Chiefs",
        "record": {"items": [{"summary": "4-0"}]},
        "nextEvent": [
            {
                "shortName": "LAC @ KC",
                "competitions": [
                    {"status": {"type": {"state": "pre", "shortDetail": "10/18 - 4:25 PM EDT"}}}
                ],
            }
        ],
    }
}
SCHEDULE = {
    "events": [
        _game("BAL @ KC", "BAL", "KC", 20, 27),
        _game("KC @ LV", "KC", "LV", 30, 27),
        _game("LAC @ KC", "LAC", "KC", completed=False, detail="10/18 - 4:25 PM EDT"),
    ]
}


def _espn(monkeypatch, schedule=SCHEDULE, team=TEAM):
    def handler(request: httpx.Request) -> httpx.Response:
        data = schedule if request.url.path.endswith("/schedule") else team
        return httpx.Response(200, json=data) if data is not None else httpx.Response(503)

    return _server(monkeypatch, handler, ip="203.0.113.9")


async def test_sports_reply_from_two_sources(monkeypatch):
    recipe = bot_recipes.load(SPORTS)
    seen, _ = _espn(monkeypatch)
    settings = bot_recipes.check_settings(recipe, {"league": "football/nfl", "team": "kc"})
    reply = await bot_recipes.run(recipe, "score", settings)
    assert reply == "Last: KC 30 @ LV 27 Final · Next: LAC @ KC 10/18 - 4:25 PM EDT · 4-0"
    # Select values go into the path as written.
    assert sorted(str(r.url) for r in seen) == [
        "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/kc",
        "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/kc/schedule",
    ]


async def test_one_source_failing_leaves_its_values_out(monkeypatch):
    recipe = bot_recipes.load(SPORTS)
    settings = {"league": "football/nfl", "team": "kc"}
    _espn(monkeypatch, schedule=None)
    assert await bot_recipes.run(recipe, "score", settings) == "Next: LAC @ KC 10/18 - 4:25 PM EDT · 4-0"
    bot_recipes._cache.clear()
    _espn(monkeypatch, schedule=None, team=None)
    with pytest.raises(bot_recipes.SourceError, match="HTTP 503"):
        await bot_recipes.run(recipe, "score", settings)


def test_select_settings():
    recipe = bot_recipes.load(SPORTS)
    assert bot_recipes.check_settings(recipe, {"league": "hockey/nhl", "team": "bos"}) == {
        "league": "hockey/nhl",
        "team": "bos",
    }
    for values, reason in [
        ({"league": "hockey/nhl", "team": "kc"}, "Team: choose one"),  # the parent changed
        ({"league": "hockey/nhl"}, "Team: choose one"),
        ({"league": "soccer/epl", "team": "kc"}, "Sport: choose one"),
    ]:
        with pytest.raises(ValueError, match=reason):
            bot_recipes.check_settings(recipe, values)
    # An update that drops a choice: cleared, and the app reports what it needs.
    stale = {"league": "hockey/nhl", "team": "kc"}
    kept = bot_apps._merge_settings(recipe, stale, stale, stale_ok=True)
    assert kept == {"league": "hockey/nhl"}
    assert bot_recipes.problem(recipe, kept) == "Team: choose one"
    assert bot_recipes.problem(recipe, {"league": "hockey/nhl", "team": "bos"}) is None


async def test_stale_select_never_reaches_a_request(monkeypatch):
    seen, _ = _espn(monkeypatch)
    recipe = bot_recipes.load(SPORTS)
    for settings in (
        {"league": "hockey/nhl", "team": "kc"},
        {"league": "hockey/nhl"},
        {"league": "x?y", "team": "kc"},
    ):
        with pytest.raises(bot_recipes.SourceError, match="choose one"):
            await bot_recipes.run(recipe, "score", settings)
    assert seen == []


def _many_options(raw: dict, per_parent: int) -> None:
    """30 sports, each with per_parent teams."""
    sports = [{"value": f"s{i}", "label": "x"} for i in range(30)]
    raw["settings"][0]["options"] = sports
    raw["settings"][1]["options_by"] = {
        s["value"]: [{"value": f"t{j}", "label": "x"} for j in range(per_parent)] for s in sports
    }


def test_a_full_league_list_fits():
    raw = copy.deepcopy(SPORTS)
    _many_options(raw, per_parent=32)  # 30 + 960 options
    assert bot_recipes.load(raw)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda r: r.update(schema=2), "needs recipe format 3"),
        (lambda r: r["settings"][0]["options"].append({"value": "a?b", "label": "x"}), "pattern"),
        (lambda r: r["settings"][0]["options"].append({"value": "nfl", "label": ""}), "at least 1"),
        (lambda r: r["settings"][0].update(depends_on="team"), "either options"),
        (lambda r: r["settings"][1].update(options=[{"value": "a", "label": "A"}]), "either options"),
        (lambda r: r["settings"][1].update(depends_on="nope"), "earlier select"),
        (lambda r: r["settings"].reverse(), "earlier select"),
        (lambda r: r["settings"][1]["options_by"].pop("hockey/nhl"), "one list for each league option"),
        (
            lambda r: r["settings"][1]["options_by"].update({"x/y": [{"value": "a", "label": "A"}]}),
            "one list",
        ),
        (
            lambda r: r["settings"].append(
                {"key": "z", "label": "Z", "options": [{"value": "a", "label": "A"}]}
            ),
            "only a select",
        ),
        (
            lambda r: r["settings"][0].update(options=[{"value": f"v{i}", "label": "x"} for i in range(61)]),
            "at most 60",
        ),
        (lambda r: _many_options(r, per_parent=33), "at most 1000 options"),
        (
            lambda r: r["commands"][0]["values"]["next"].update(source="nope"),
            "reads a source the command doesn't",
        ),
        (lambda r: r["commands"][0].update(source="team"), "either one source or a list"),
        (lambda r: r["commands"][0].update(sources=["team", "team"]), "twice"),
        (lambda r: r["commands"][0].update(sources=["team", "schedule", "team", "x"]), "at most 3"),
        (lambda r: r["commands"][0]["values"]["next"].update(format="number", map={"a": "b"}), "format text"),
        (lambda r: r["sources"]["team"].update(max_kb=4096), "less than or equal to 2048"),
    ],
)
def test_invalid_select_and_multi_source_recipes_are_refused(change, reason):
    raw = copy.deepcopy(SPORTS)
    change(raw)
    with pytest.raises(bot_recipes.RecipeError, match=reason):
        bot_recipes.load(raw)


# ---- 7: the path language ------------------------------------------------------------------------------

AIRCRAFT = {
    "now": 1760600000.1,
    "messages": 123456,
    "aircraft": [
        {"hex": "a1b2c3", "flight": "UAL123  ", "lat": 37.6, "lon": -122.4, "alt_baro": 12000},
        {"hex": "a1b2c4", "alt_baro": 3500},
        {"hex": "a1b2c5", "flight": "SWA88", "lat": 37.7, "lon": -122.2},
        {"hex": "a1b2c6", "lat": None},
        {"hex": "a1b2c7", "lat": ""},
    ],
}


def test_paths_on_the_espn_schedule():
    get = bot_recipes.get_path
    completed = "events[competitions.0.status.type.completed=true]"
    assert get(SCHEDULE, f"{completed}.shortName") == "BAL @ KC"  # first match
    assert get(SCHEDULE, "events[last:competitions.0.status.type.completed=TRUE].shortName") == "KC @ LV"
    assert get(SCHEDULE, "events[competitions.0.status.type.completed=false].shortName") == "LAC @ KC"
    assert get(SCHEDULE, "events[-1].shortName") == "LAC @ KC"
    assert get(SCHEDULE, "events[-3].shortName") == "BAL @ KC" and get(SCHEDULE, "events[-4]") is None
    has_score = "events[last:has:competitions.0.competitors.0.score.displayValue].shortName"
    assert get(SCHEDULE, has_score) == "KC @ LV"
    assert get(SCHEDULE, completed, count=True) == 2
    assert get(SCHEDULE, "events", count=True) == 3
    assert get(SCHEDULE, "events[competitions.0.status.type.shortDetail=Final/OT]", count=True) == 0
    assert get(SCHEDULE, "events[competitions.9.x=true].shortName") is None
    # Dots inside a filter value don't split the path.
    assert get({"a": [{"v": "1.5", "w": 2}]}, "a[v=1.5].w") == 2


def test_paths_on_dump1090_aircraft():
    get = bot_recipes.get_path
    assert get(AIRCRAFT, "aircraft", count=True) == 5
    assert get(AIRCRAFT, "aircraft[has:lat]", count=True) == 2  # null and "" don't count
    assert get(AIRCRAFT, "aircraft[last:has:lat].hex") == "a1b2c5"
    assert get(AIRCRAFT, "aircraft[has:flight].flight") == "UAL123  "
    assert get(AIRCRAFT, "aircraft[-1].hex") == "a1b2c7"
    assert get({"aircraft": []}, "aircraft", count=True) == 0
    assert get({"aircraft": []}, "aircraft[has:lat]", count=True) == 0
    assert get({}, "aircraft", count=True) is None
    assert get({"aircraft": {"x": 1}}, "aircraft", count=True) is None

    count = bot_recipes.Value.model_validate({"from": ["missing", "aircraft[has:lat]"], "format": "count"})
    assert bot_recipes.extract(count, AIRCRAFT)["value"] == "2"
    assert bot_recipes.extract(count, {}) is None
    for bad in ("a[last:]", "a[has:]", "a[has:x=1]", "a[x..y=1]", "a[last:-1]", "a[]", "a[.x=1]"):
        with pytest.raises(ValueError):
            bot_recipes.parse_path(bad)


def test_new_paths_need_format_3():
    raw = copy.deepcopy(HA)
    raw["commands"][0]["values"]["state"]["from"] = ["list[-1].state"]
    assert bot_recipes.load(raw)
    raw["schema"] = 2
    raw["commands"][0] = {
        **raw["commands"][0],
        "group": None,
        "reply": {"parts": [{"items": ["{state}"]}]},
    }
    raw["commands"][0].pop("group")
    raw["sources"]["state"]["url"] = "http://{host}/api/states"
    raw["settings"].pop()
    with pytest.raises(bot_recipes.RecipeError, match=r"The path list\[-1\].state needs recipe format 3"):
        bot_recipes.load(raw)


def test_map_and_duration():
    value = bot_recipes.Value.model_validate(
        {"from": ["s"], "format": "text", "map": {"on": "open", "off": ""}}
    )
    assert bot_recipes.extract(value, {"s": "ON"})["value"] == "open"
    assert bot_recipes.extract(value, {"s": "OFF"}) is None  # mapped to "": left out
    assert bot_recipes.extract(value, {"s": "UNDEF"})["value"] == "UNDEF"
    up = bot_recipes.Value.model_validate({"from": ["u"], "format": "duration"})
    assert [bot_recipes.extract(up, {"u": s})["value"] for s in (5, 83, 3600, 5000, 86400, 200000)] == [
        "5s",
        "1m23s",
        "1h",
        "1h23m",
        "1d",
        "2d7h",
    ]


# ---- 8: response size ------------------------------------------------------------------------------------


async def test_max_kb_and_uncached_sources(monkeypatch):
    big = {"events": [{"shortName": "x" * 1000}] * 1200}  # about 1.2 MB, like an NHL schedule
    _server(monkeypatch, lambda r: httpx.Response(200, json=big), ip="203.0.113.9")
    raw = copy.deepcopy(SPORTS)
    raw["commands"][0]["values"]["n"] = {"source": "schedule", "from": ["events"], "format": "count"}
    raw["commands"][0]["reply"]["parts"] = [{"items": ["{n} games"]}]
    settings = {"league": "hockey/nhl", "team": "bos"}
    assert await bot_recipes.run(bot_recipes.load(raw), "score", settings) == "1200 games"
    raw["sources"]["schedule"]["max_kb"] = 512
    with pytest.raises(bot_recipes.SourceError, match="too much data"):
        await bot_recipes.run(bot_recipes.load(raw), "score", settings, use_cache=False)
    raw["sources"]["schedule"].update(max_kb=2048, cache_seconds=0)
    raw["sources"]["team"]["cache_seconds"] = 0
    bot_recipes._cache.clear()
    await bot_recipes.run(bot_recipes.load(raw), "score", settings)
    assert bot_recipes._cache == {}


# ---- 9: logins (Pi-hole v6) ------------------------------------------------------------------------------

PIHOLE = _recipe(
    id="pihole",
    name="Pi-hole",
    category="Network",
    settings=[
        {"key": "host", "label": "Address", "type": "host"},
        {"key": "password", "label": "App password", "type": "secret"},
    ],
    sources={
        "summary": {
            "url": "http://{host}/api/stats/summary",
            "network": "local",
            "login": "pihole",
            "headers": {"X-FTL-SID": "{login.token}"},
        }
    },
    logins={
        "pihole": {
            "url": "http://{host}/api/auth",
            "body": {"password": "{password}"},
            "token_from": "session.sid",
            "ttl_from": "session.validity",
            "ttl_max": 1800,
        }
    },
    commands=[
        {
            "name": "dns",
            "help": "Pi-hole stats",
            "source": "summary",
            "values": {
                "total": {"from": ["queries.total"]},
                "blocked": {"from": ["queries.blocked"]},
                "pct": {"from": ["queries.percent_blocked"]},
                "domains": {"from": ["gravity.domains_being_blocked"]},
            },
            "reply": {
                "parts": [{"items": ["{total} queries", "{blocked} blocked ({pct}%)", "{domains} on lists"]}]
            },
        }
    ],
)
SUMMARY = {
    "queries": {"total": 41234, "blocked": 5123, "percent_blocked": 12.4238},
    "gravity": {"domains_being_blocked": 123456},
}


class PiHole:
    """A Pi-hole v6 that hands out sessions, and can forget them."""

    def __init__(self, password="hunter2+/="):
        self.password, self.sessions, self.logins = password, set(), 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth":
            assert request.method == "POST"
            if json.loads(request.content) != {"password": self.password}:
                return httpx.Response(401, json={"session": {"valid": False}})
            self.logins += 1
            sid = f"sid+/={self.logins}"
            self.sessions.add(sid)
            return httpx.Response(200, json={"session": {"valid": True, "sid": sid, "validity": 1800}})
        assert request.method == "GET"
        if request.headers.get("X-FTL-SID") not in self.sessions:
            return httpx.Response(401, json={"error": {"key": "unauthorized"}})
        return httpx.Response(200, json=SUMMARY)


async def test_pihole_login_is_reused_and_renewed(monkeypatch):
    pihole = PiHole()
    seen, _ = _server(monkeypatch, pihole)
    recipe = bot_recipes.load(PIHOLE)
    settings = {"host": "pi.hole", "password": "hunter2+/="}
    want = "41234 queries 5123 blocked (12.42%) 123456 on lists"
    assert await bot_recipes.run(recipe, "dns", settings, use_cache=False) == want
    assert await bot_recipes.run(recipe, "dns", settings, use_cache=False) == want
    assert pihole.logins == 1  # the session is reused, not one per request
    assert seen[1].headers["X-FTL-SID"] == "sid+/=1"  # as written
    assert recipe.contacts() == [
        "contacts a device on your local network",
        "signs in to the device with your password",
    ]

    pihole.sessions.clear()  # e.g. the Pi-hole restarted: sign in again once, and retry
    assert await bot_recipes.run(recipe, "dns", settings, use_cache=False) == want
    assert pihole.logins == 2

    # Another device or password never gets this session.
    other = {"host": "192.168.1.60", "password": "hunter2+/="}
    assert await bot_recipes.run(recipe, "dns", other, use_cache=False) == want
    assert pihole.logins == 3


async def test_pihole_refusals(monkeypatch):
    recipe = bot_recipes.load(PIHOLE)
    _server(monkeypatch, PiHole(password="right"))
    with pytest.raises(bot_recipes.SourceError, match="didn't accept the password"):
        await bot_recipes.run(recipe, "dns", {"host": "pi.hole", "password": "wrong"})

    # A device that keeps refusing the session: one new sign-in, one retry, then unavailable.
    pihole = PiHole()
    pihole.sessions = type("Never", (set,), {"__contains__": lambda self, x: False})()
    seen, _ = _server(monkeypatch, pihole)
    with pytest.raises(bot_recipes.SourceError, match="HTTP 401"):
        await bot_recipes.run(recipe, "dns", {"host": "pi.hole", "password": "hunter2+/="})
    assert pihole.logins == 2 and [r.method for r in seen] == ["POST", "GET", "POST", "GET"]

    _server(monkeypatch, PiHole(), ip="93.184.216.34")
    with pytest.raises(bot_recipes.SourceError, match="isn't on the local network"):
        await bot_recipes.run(recipe, "dns", {"host": "pi.hole", "password": "hunter2+/="})


async def test_login_reply_without_a_session(monkeypatch):
    _server(monkeypatch, lambda r: httpx.Response(200, json={"session": {"valid": False, "sid": None}}))
    with pytest.raises(bot_recipes.SourceError, match="had no session"):
        await bot_recipes.run(bot_recipes.load(PIHOLE), "dns", {"host": "pi.hole", "password": "x"})


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda r: r.update(schema=2), "needs recipe format 3"),
        (lambda r: r["logins"].update(other=r["logins"]["pihole"]), "one login"),
        (lambda r: r["sources"]["summary"].update(login="nope"), "unknown login"),
        (lambda r: r["sources"]["summary"].pop("login"), "only a source with a login"),
        (
            lambda r: r["sources"].update(
                other={"url": "http://{host}/x?sid={login.token}", "network": "local"}
            ),
            "only a source with a login",
        ),
        (
            lambda r: r["logins"]["pihole"].update(body={"password": "{login.token}"}),
            "only a source with a login",
        ),
        (lambda r: r["logins"]["pihole"].update(body={"password": "{nope}"}), "unknown settings"),
        (
            lambda r: (
                r["settings"].append({"key": "other", "label": "Other", "type": "host"}),
                r["logins"]["pihole"].update(url="http://{other}/api/auth"),
            ),
            "same scheme and host setting",
        ),
        (
            lambda r: r["logins"]["pihole"].update(url="http://evil.example/api/auth"),
            "same scheme and host setting",
        ),
        (
            lambda r: (
                r.update(hosts=["pi.example"]),
                r["sources"]["summary"].update(url="http://pi.example/x", network="internet"),
            ),
            "Only a local source can use a login",
        ),
        (
            lambda r: (
                r["sources"]["summary"].pop("login"),
                r["sources"]["summary"].update(headers={}),
            ),
            "isn't used",
        ),
        (lambda r: r["commands"][0]["reply"]["parts"][0]["items"].append("{login}"), "unknown value"),
    ],
)
def test_invalid_login_recipes_are_refused(change, reason):
    raw = copy.deepcopy(PIHOLE)
    change(raw)
    with pytest.raises(bot_recipes.RecipeError, match=reason):
        bot_recipes.load(raw)


# ---- choosing http or https, select defaults, longer secrets -----------------------------------

SCHEME = {
    "key": "scheme",
    "label": "Connection",
    "type": "select",
    "default": "http",
    "options": [
        {"value": "http", "label": "http:// (Home Assistant's default)"},
        {"value": "https", "label": "https:// (SSL turned on)"},
    ],
}


def _ha_with_scheme(**changes) -> dict:
    raw = copy.deepcopy(HA)
    raw["version"] = "1.1.0"
    raw["settings"].insert(0, copy.deepcopy(SCHEME))
    raw["sources"]["state"].update(url="{scheme}://{host}/api/states/{item}", verify_tls=False)
    raw.update(changes)
    return raw


async def test_scheme_select_chooses_http_or_https(monkeypatch):
    recipe = bot_recipes.load(_ha_with_scheme())
    seen, clients = _home_assistant(monkeypatch)
    for scheme in ("http", "https"):
        bot_recipes._cache.clear()
        reply = await bot_recipes.run(recipe, "ha", {**HA_SETTINGS, "scheme": scheme}, "garage")
        assert reply == "Left garage: closed · Right garage: open"
        assert {r.url.scheme for r in seen[-2:]} == {scheme}
        assert str(seen[-1].url).startswith(f"{scheme}://ha.test:8123/api/states/")
    assert clients[-1]["verify"] is False
    assert recipe.contacts() == [
        "contacts a device on your local network (accepts its self-signed certificate)"
    ]


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (
            lambda r: r["settings"][0]["options"].append({"value": "ftp", "label": "FTP"}),
            "only http and https",
        ),
        (lambda r: r["settings"][0].update(type="text", options=None, default=None), "only http and https"),
        (lambda r: r["sources"]["state"].update(url="{nope}://{host}/x"), "unknown settings: nope"),
        (
            lambda r: (
                r["settings"].insert(0, {**SCHEME, "key": "kind", "default": None}),
                r["settings"][1].update(
                    depends_on="kind",
                    options=None,
                    default=None,
                    options_by={
                        "http": [{"value": "http", "label": "x"}],
                        "https": [{"value": "https", "label": "x"}],
                    },
                ),
            ),
            "only http and https",
        ),
        (
            lambda r: (
                r.update(hosts=["ha.example"]),
                r["sources"]["state"].update(
                    url="{scheme}://ha.example/x", network="internet", verify_tls=True
                ),
            ),
            "Only a local source can take its scheme",
        ),
        (lambda r: r["sources"]["state"].update(url="{scheme}://{token}/x"), "host must be a host setting"),
        (lambda r: r["sources"]["state"].update(url="ftp://{host}/x"), "starts with http://, https://"),
    ],
)
def test_invalid_scheme_recipes_are_refused(change, reason):
    raw = _ha_with_scheme()
    change(raw)
    with pytest.raises(bot_recipes.RecipeError, match=reason):
        bot_recipes.load(raw)


def test_login_uses_the_sources_scheme():
    raw = copy.deepcopy(PIHOLE)
    raw["settings"].insert(0, copy.deepcopy(SCHEME))
    raw["sources"]["summary"]["url"] = "{scheme}://{host}/api/stats/summary"
    with pytest.raises(bot_recipes.RecipeError, match="same scheme and host setting"):
        bot_recipes.load(raw)  # the login still says http://
    raw["logins"]["pihole"]["url"] = "{scheme}://{host}/api/auth"
    assert bot_recipes.load(raw)


async def test_select_default_keeps_an_updated_app_running(monkeypatch):
    stored = bot_recipes.check_settings(bot_recipes.load(HA), HA_SETTINGS)  # installed at 1.0.0
    assert "scheme" not in stored
    newer = bot_recipes.load(_ha_with_scheme())
    assert bot_recipes.problem(newer, stored) is None
    seen, _ = _home_assistant(monkeypatch)
    assert await bot_recipes.run(newer, "ha", stored, "garage") == "Left garage: closed · Right garage: open"
    assert {r.url.scheme for r in seen} == {"http"}
    # Saving (or updating) stores it.
    assert bot_recipes.check_settings(newer, stored)["scheme"] == "http"
    assert bot_apps._merge_settings(newer, stored, stored, stale_ok=True)["scheme"] == "http"
    assert bot_recipes.check_settings(newer, {**stored, "scheme": "https"})["scheme"] == "https"
    # Without a default, a missing select still needs choosing.
    raw = _ha_with_scheme()
    raw["settings"][0].pop("default")
    assert bot_recipes.problem(bot_recipes.load(raw), stored) == "Connection: choose one"


@pytest.mark.parametrize(
    ("setting", "reason"),
    [
        ({**SCHEME, "default": "ftp"}, "must be one of its options"),
        ({"key": "x", "label": "X", "default": "a"}, "only a select has options or a default"),
        (
            {
                "key": "team",
                "label": "Team",
                "type": "select",
                "depends_on": "league",
                "default": "kc",
                "options_by": {"football/nfl": [{"value": "kc", "label": "KC"}]},
            },
            "must be one of its options",
        ),
    ],
)
def test_invalid_select_defaults_are_refused(setting, reason):
    with pytest.raises(ValueError, match=reason):
        bot_recipes.Setting.model_validate(setting)


def test_secrets_can_be_long_tokens():
    recipe = bot_recipes.load(HA)
    jwt = "eyJ" + "a" * 437  # an Enphase owner token is about 440 characters
    assert bot_recipes.check_settings(recipe, {**HA_SETTINGS, "token": jwt})["token"] == jwt
    with pytest.raises(ValueError, match="Access token is too long"):
        bot_recipes.check_settings(recipe, {**HA_SETTINGS, "token": "a" * 2001})
    with pytest.raises(ValueError, match="Address is too long"):
        bot_recipes.check_settings(recipe, {**HA_SETTINGS, "host": "a" * 254})
    with pytest.raises(ValueError, match="control characters"):
        bot_recipes.check_settings(recipe, {**HA_SETTINGS, "token": jwt + "\r\nX: y"})
