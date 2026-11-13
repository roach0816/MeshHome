"""Settings → Health & logs: the log history (services/app_log.py) and the health checks
(services/health.py)."""

import asyncio
import logging
import socket
from datetime import timedelta

from app import db
from app.models import LogEntry, utcnow
from app.services import app_log, app_settings, health
from tests.conftest import csrf, do_setup, radio_adapter, wait_for


async def _logs(client, **params):
    r = await client.get("/api/system/logs", params=params)
    assert r.status_code == 200, r.text
    return r.json()


async def test_history_keeps_problems_grouped(client, caplog):
    caplog.set_level(logging.INFO)  # as in the app (LOG_LEVEL=INFO)
    await do_setup(client)
    lg = logging.getLogger("app.radio.supervisor")
    lg.warning("test: the radio sneezed")
    lg.warning("test: the radio sneezed")
    logging.getLogger("app.services.tracking").error("test: tracker lost")
    logging.getLogger("app.services.tracking").info("test: just chatting")  # INFO: recent only
    assert await app_log.flush()
    lg.warning("test: the radio sneezed")  # after the write: still the same entry
    assert await app_log.flush()

    page = await _logs(client, level="problems")
    sneeze = [e for e in page["items"] if e["message"] == "test: the radio sneezed"]
    assert len(sneeze) == 1 and sneeze[0]["count"] == 3 and sneeze[0]["component"] == "radio"
    assert sneeze[0]["level"] == "WARNING" and sneeze[0]["saved"] is True
    assert any(e["message"] == "test: tracker lost" and e["component"] == "tracking" for e in page["items"])
    assert not any(e["message"] == "test: just chatting" for e in page["items"])
    assert page["components"]["radio"] == "Radio"

    only = await _logs(client, component="tracking")
    assert {e["component"] for e in only["items"]} == {"tracking"}
    found = await _logs(client, q="SNEEZED")
    assert [e["message"] for e in found["items"]] == ["test: the radio sneezed"]

    recent = await _logs(client, view="recent", q="test:")
    msgs = [e["message"] for e in recent["items"]]
    assert "test: just chatting" in msgs and msgs.count("test: the radio sneezed") == 3
    assert recent["since"] and recent["kept"] == app_log.RECENT_MAX
    older = await _logs(client, view="recent", q="test:", before=recent["items"][0]["seq"], limit=1)
    assert older["items"][0]["seq"] < recent["items"][0]["seq"] and older["more"]


async def test_private_and_access_lines_are_not_kept(client):
    await do_setup(client)
    logging.getLogger("meshhome").warning("setup token: hunter2", extra={"private": True})
    logging.getLogger("uvicorn.access").warning("GET /secret")
    assert await app_log.flush()
    assert not (await _logs(client, q="hunter2"))["items"]
    assert not (await _logs(client, view="recent", q="hunter2"))["items"]
    assert not (await _logs(client, view="recent", q="/secret"))["items"]


async def test_waits_while_the_database_is_away(client, monkeypatch):
    await do_setup(client)
    real = db.session_factory

    def broken():
        def make():
            raise FileNotFoundError(2, "No such file or directory")

        return make

    monkeypatch.setattr(db, "session_factory", broken)
    logging.getLogger("app.radio.supervisor").warning("test: written later")
    assert not await app_log.flush()
    assert app_log.stats()["waiting"] >= 1 and app_log.stats()["write_error"]
    monkeypatch.setattr(db, "session_factory", real)
    # Before it's written, it's shown as not yet saved.
    waiting = await _logs(client, q="written later")
    assert waiting["items"][0]["saved"] is False
    assert await app_log.flush()
    saved = await _logs(client, q="written later")
    assert [e["saved"] for e in saved["items"]] == [True]


async def test_old_entries_are_pruned(client):
    await do_setup(client)
    async with db.session_factory()() as s:
        old = utcnow() - timedelta(days=app_log.RETAIN_DAYS + 1)
        s.add(
            LogEntry(
                first_at=old,
                last_at=old,
                level="WARNING",
                component="app",
                logger="t",
                message="test: ancient",
            )
        )
        await s.commit()
    await app_log.prune()
    assert not (await _logs(client, q="ancient"))["items"]


async def test_radio_history_and_health_when_connected(client):
    await do_setup(client)
    await radio_adapter()
    await app_log.flush()
    radio = [e for e in (await _logs(client, component="radio"))["items"] if e["event"] == "radio"]
    assert radio and radio[0]["message"].startswith("Connected to")
    started = (await _logs(client, component="app"))["items"]
    assert any(e["event"] == "app" and "started" in e["message"] for e in started)

    r = (await client.get("/api/system/health")).json()
    items = {i["id"]: i for i in r["items"]}
    assert items["radio"]["status"] == "info" and "simulated" in items["radio"]["summary"]
    assert items["database"]["status"] == "ok"
    assert items["services"]["status"] == "ok"
    assert items["sending"]["status"] == "ok"
    assert r["overall"] in ("ok", "warn")

    text = (await client.get("/api/system/health/report")).text
    assert "health report" in text and "Radio connection:" in text and "Radio connection history" in text


async def test_health_explains_an_offline_radio(client):
    with socket.socket() as sock:  # a port nothing listens on
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    await do_setup(client, mode="tcp", host="127.0.0.1", port=port)

    async def offline():
        r = (await client.get("/api/system/health")).json()
        radio = next(i for i in r["items"] if i["id"] == "radio")
        return r if radio["status"] == "bad" else None

    r = await wait_for(offline, timeout=15)
    radio = next(i for i in r["items"] if i["id"] == "radio")
    assert f"Nothing is accepting connections at 127.0.0.1:{port}" in radio["summary"]
    assert radio["fix"] and radio["link"] == "/settings/radio" and radio["logs"] == "radio"
    assert r["overall"] == "bad" and "attention" in r["summary"]
    await app_log.flush()
    offline_lines = (await _logs(client, level="problems", component="radio"))["items"]
    assert any(e["message"].startswith("Radio offline, retrying") for e in offline_lines)
    # Saving the settings is recorded too.
    r2 = await client.put(
        "/api/settings/radio",
        headers=csrf(client),
        json={"tech": "meshcore", "mode": "simulated", "sim_interval_seconds": 0},
    )
    assert r2.status_code == 200, r2.text
    await app_log.flush()
    assert any(e["event"] == "settings" for e in (await _logs(client, component="radio"))["items"])


async def test_api_keys_read_health_but_not_logs(client):
    await do_setup(client)
    key = (
        await client.post("/api/api-keys", headers=csrf(client), json={"name": "monitor", "scope": "read"})
    ).json()["key"]
    auth = {"Authorization": f"Bearer {key}"}
    client.cookies.clear()
    assert (await client.get("/api/system/health", headers=auth)).status_code == 200
    assert (await client.get("/api/system/logs", headers=auth)).status_code == 403
    assert (await client.get("/api/system/health/report", headers=auth)).status_code == 403


async def test_a_stopped_background_task_is_reported(client):
    await do_setup(client)

    async def crash():
        raise RuntimeError("boom")

    t = asyncio.create_task(crash())
    health.watch("test", "Test task", "app", lambda: t)
    try:
        await asyncio.gather(t, return_exceptions=True)
        await asyncio.sleep(0)
        r = (await client.get("/api/system/health")).json()
        services = next(i for i in r["items"] if i["id"] == "services")
        assert services["status"] == "bad" and "Test task" in services["summary"]
        await app_log.flush()
        lines = (await _logs(client, level="problems", q="Test task stopped"))["items"]
        assert lines and lines[0]["level"] == "ERROR" and "RuntimeError: boom" in (lines[0]["detail"] or "")
    finally:
        health._watched.pop("test", None)


def test_explain():
    tcp = app_settings.RadioConfig(mode="tcp", host="10.0.0.9", port=4403, tech="meshtastic")
    what, why, fix = health.explain("[Errno 111] Connect call failed ('10.0.0.9', 4403)", tcp, "meshtastic")
    assert what == "Nothing is accepting connections at 10.0.0.9:4403" and "4403" in why
    what, why, _ = health.explain("connection lost: EOF", tcp, "meshtastic")
    assert "one app at a time" in why
    what, _, _ = health.explain("TimeoutError", tcp, "meshtastic")
    assert what == "No answer from 10.0.0.9:4403"
    what, _, _ = health.explain(
        "Database unavailable: [Errno 2] No such file or directory", tcp, "meshtastic"
    )
    assert "database" in what
    hat = app_settings.RadioConfig(mode="hat", tech="meshtastic")
    what, _, fix = health.explain("[Errno 111] Connection refused", hat, "meshtastic")
    assert "meshtasticd" in what and any("radio-hat logs" in f for f in fix)
    assert health.span(3700) == "1 h 1 min" and health.ago(None) == "never"
