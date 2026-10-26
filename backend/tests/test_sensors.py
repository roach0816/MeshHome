"""Sensors attached to the Pi (services/sensors.py): NMEA parsing, the environment sensor and GPS
readers (with fake hardware), the radio's location from the GPS, history, the API and /env."""

import time
from datetime import UTC, datetime, timedelta

import pytest

from app import db
from app.models import SensorReading
from app.radio.base import IncomingMessage
from app.services import bot, sensors
from app.services.sensors import Sensors, distance_m, parse_nmea
from tests.conftest import csrf, do_setup, radio_adapter, wait_for
from tests.test_api import _connected_conversations

# From a RAK12501 (Quectel L76K) on a RAK6421 HAT, indoors without a fix.
NO_FIX = [
    "$GNGGA,,,,,,0,00,25.5,,,,,,*64",
    "$GNGLL,,,,,,V,N*7A",
    "$GNRMC,,V,,,,,,,,,,N,V*37",
    "$GPTXT,01,01,01,ANTENNA OK*35",
]


def nmea(body: str) -> str:
    cs = 0
    for ch in body.encode():
        cs ^= ch
    return f"${body}*{cs:02X}"


FIX = [
    nmea("GNGGA,123519.00,4807.0380,N,01131.0000,E,1,08,0.9,545.4,M,46.9,M,,"),
    nmea("GNRMC,123519.00,A,4807.0380,N,01131.0000,E,0.02,,251026,,,A,V"),
]


def test_parse_real_no_fix_sentences():
    gga = parse_nmea(NO_FIX[0])
    assert gga == {"kind": "GGA", "fix": False, "satellites": 0, "hdop": 25.5}
    assert parse_nmea(NO_FIX[2]) == {"kind": "RMC", "fix": False}
    assert parse_nmea(NO_FIX[1]) is None and parse_nmea(NO_FIX[3]) is None  # not used
    assert parse_nmea("$GNGGA,,,,,,0,00,25.5,,,,,,*65") is None  # bad checksum
    assert parse_nmea("garbage") is None and parse_nmea("") is None


def test_parse_fix_sentences():
    gga = parse_nmea(FIX[0])
    assert gga["fix"] and gga["satellites"] == 8 and gga["alt_m"] == 545.4
    assert gga["lat"] == pytest.approx(48.1173) and gga["lon"] == pytest.approx(11.516667, abs=1e-6)
    rmc = parse_nmea(FIX[1])
    assert rmc["fix"] and rmc["utc"] == "2026-10-25T12:35:19+00:00"
    south_west = parse_nmea(nmea("GNRMC,010203,A,3351.5000,S,15112.0000,W,0,,010126,,,A"))
    assert south_west["lat"] == pytest.approx(-33.858333) and south_west["lon"] == pytest.approx(-151.2)


def test_distance():
    assert distance_m(48.1173, 11.5167, 48.1173, 11.5167) == 0
    assert distance_m(0, 0, 0, 1) == pytest.approx(111195, rel=1e-3)


class FakeEnv:
    def __init__(self, reading):
        self.reading = reading

    def read(self):
        if isinstance(self.reading, Exception):
            raise self.reading
        return self.reading


async def test_environment_sensor_found_read_and_lost():
    s = Sensors()
    tried = []

    def opener(bus, address):
        tried.append((bus, address))
        if address == 0x76:
            raise OSError("no device")
        return FakeEnv({"temperature_c": 21.43, "humidity": 48.0, "pressure_hpa": 1013.2, "gas_ohms": 120000})

    s.open_environment = opener
    cfg = sensors.app_settings.SensorsConfig()
    await s._environment(cfg)
    assert tried == [(1, 0x76), (1, 0x77)] and s.env_address == 0x77 and s.env_status == "ok"
    assert s.env["temperature_c"] == 21.43
    assert s.env_reply() == "🌡️ 21.4°C · 💧 48% RH · ⏲️ 1013.2 hPa · 🍃 air 120 kΩ"
    assert len(s.env_reply().encode()) <= 150  # one direct message
    assert s.env_reply("imperial") == "🌡️ 70.6°F · 💧 48% RH · ⏲️ 29.92 inHg · 🍃 air 120 kΩ"

    # Unplugged: marked as an error, and looked for again later.
    s._env_dev.reading = OSError("Remote I/O error")
    await s._environment(cfg)
    assert s.env_status == "error" and s._env_dev is None

    # Turned off.
    await s._environment(sensors.app_settings.SensorsConfig(environment=False))
    assert s.env_status == "off" and s.env is None
    assert s.env_reply() == "No environment readings on this node right now."


async def test_environment_sensor_absent():
    s = Sensors()

    def opener(bus, address):
        raise OSError("[Errno 2] No such file or directory: '/dev/i2c-1'")

    s.open_environment = opener
    await s._environment(sensors.app_settings.SensorsConfig())
    assert s.env_status == "not_found" and s.env is None


class FakeSerial:
    def __init__(self, lines):
        self.lines = [(line + "\r\n").encode() for line in lines]

    def readline(self):
        if self.lines:
            return self.lines.pop(0)
        time.sleep(0.05)
        return b""

    def close(self):
        pass


async def test_gnss_reader_no_fix_then_fix():
    s = Sensors()
    feed = FakeSerial(NO_FIX)
    s.open_serial = lambda device: feed
    cfg = sensors.app_settings.SensorsConfig()
    s._gnss_follow(cfg)

    async def no_fix():
        return s.gnss_status == "no_fix"

    await wait_for(no_fix)
    assert s.gnss.get("fix") is False and s.gnss["satellites"] == 0 and s.gnss_fix() is None
    feed.lines += [(line + "\r\n").encode() for line in FIX]

    async def fixed():
        return s.gnss_status == "fix"

    await wait_for(fixed)
    g = s.gnss_fix()
    assert g["lat"] == pytest.approx(48.1173) and g["satellites"] == 8 and g["utc"].startswith("2026-10-25")
    assert s.snapshot()["gnss"]["status"] == "fix"
    s._gnss_follow(sensors.app_settings.SensorsConfig(gnss=False))
    assert s.gnss_status == "off"


async def test_gnss_device_missing():
    s = Sensors()

    def opener(device):
        raise OSError(f"[Errno 2] could not open port {device}: No such file or directory")

    s.open_serial = opener
    s._gnss_follow(sensors.app_settings.SensorsConfig(gnss_device="/dev/ttyNOPE"))

    async def missing():
        return s.gnss_status == "not_found"

    await wait_for(missing)
    s._stop_gnss()


async def test_location_from_gps(client):
    from app.radio import simulated

    simulated._SIM_STATE = None
    await do_setup(client)
    await _connected_conversations(client)
    s = Sensors()
    s.gnss = {"fix": True, "lat": 48.1173, "lon": 11.5167, "fix_at": time.time(), "at": time.time()}
    cfg = sensors.app_settings.SensorsConfig(update_location=True, location_min_move_m=100)
    await s._maybe_update_location(cfg)
    st = simulated.sim_state()
    assert (st["lat"], st["lon"]) == (48.1173, 11.5167) and s.location_update["moved_m"] is not None

    # A small move (about 30 m) doesn't change the radio; a bigger one does, but not more than
    # once per 10 minutes.
    s.gnss = {**s.gnss, "lat": 48.11757, "fix_at": time.time()}
    s._location_checked = 0
    await s._maybe_update_location(cfg)
    assert st["lat"] == 48.1173
    s.gnss = {**s.gnss, "lat": 48.1273, "fix_at": time.time()}
    await s._maybe_update_location(cfg)
    assert st["lat"] == 48.1173  # checked too recently
    s._location_checked = 0
    await s._maybe_update_location(cfg)
    assert st["lat"] == 48.1273 and st["name"]  # name kept


async def test_sensors_api_and_history(client):
    await do_setup(client)
    r = await client.get("/api/sensors")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"environment", "gnss", "config"} and body["config"]["environment"] is True

    r = await client.put(
        "/api/settings/sensors", headers=csrf(client), json={**body["config"], "update_location": True}
    )
    assert r.status_code == 200 and r.json()["update_location"] is True
    assert (await client.get("/api/sensors")).json()["config"]["update_location"] is True
    bad = await client.put(
        "/api/settings/sensors", headers=csrf(client), json={**body["config"], "gnss_device": "x"}
    )
    assert bad.status_code == 422

    now = datetime.now(UTC)
    async with db.session_factory()() as s:
        for i in range(400):  # every 5 minutes, about 33 hours
            s.add(SensorReading(at=now - timedelta(minutes=5 * i), temperature_c=20 + i % 3, humidity=50.0))
        await s.commit()
    h = (await client.get("/api/sensors/history?hours=24")).json()
    assert h["hours"] == 24 and 250 <= len(h["points"]) <= 300
    assert h["points"][0]["at"] < h["points"][-1]["at"] and h["points"][0]["humidity"] == 50.0
    h = (await client.get("/api/sensors/history?hours=48")).json()
    assert len(h["points"]) <= 300 and all(p["pressure_hpa"] is None for p in h["points"])


async def test_env_bot_command(client):
    from app.radio import simulated

    simulated._SIM_STATE = None
    bot._last_reply.clear()
    bot._recent.clear()
    await do_setup(client)
    await _connected_conversations(client)
    await client.put("/api/settings/bot", headers=csrf(client), json={"enabled": True, "allow": "everyone"})
    m = sensors.manager
    saved = (m.env_status, m.env, m._env_dev, m.open_environment)
    values = {"temperature_c": 18.25, "humidity": 61.0, "pressure_hpa": 1002.4, "gas_ohms": None}
    # The background loop keeps reading too (e.g. when settings change): give it the same sensor.
    m.open_environment = lambda bus, address: FakeEnv(values)
    m._env_dev, m.env_status, m.env = FakeEnv(values), "ok", {**values, "at": time.time()}
    try:

        async def ask(text: str) -> str:
            async def replies():
                convs = (await client.get("/api/conversations")).json()
                conv = next((c for c in convs if c["title"].startswith("Tracker")), None)
                if conv is None:
                    return []
                msgs = (await client.get(f"/api/conversations/{conv['id']}/messages?limit=200")).json()[
                    "messages"
                ]
                return [x["body"] for x in msgs if x["direction"] == "out" and x["meta"].get("bot")]

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

        assert await ask("/env") == "🌡️ 18.2°C · 💧 61% RH · ⏲️ 1002.4 hPa"
        cfg = (await client.get("/api/sensors")).json()["config"]
        await client.put("/api/settings/sensors", headers=csrf(client), json={**cfg, "units": "imperial"})
        assert await ask("/env now") == "🌡️ 64.8°F · 💧 61% RH · ⏲️ 29.60 inHg"
        assert "env (temperature, humidity and pressure here)" in await ask("/help")
        # Turned off under Bot settings: not answered, and not in /help.
        await client.put(
            "/api/settings/bot",
            headers=csrf(client),
            json={"enabled": True, "allow": "everyone", "env": False},
        )
        assert await ask("/env again") == "Unknown command /env. Try /help"
        assert "env (" not in await ask("/help me")
    finally:
        m.env_status, m.env, m._env_dev, m.open_environment = saved


def test_gnss_device_on_a_pi5(tmp_path):
    from app.services.sensors import gnss_device_for

    pi5, pi4 = tmp_path / "pi5", tmp_path / "pi4"
    pi5.write_bytes(b"Raspberry Pi 5 Model B Rev 1.0\x00")
    pi4.write_bytes(b"Raspberry Pi 4 Model B Rev 1.4\x00")
    assert gnss_device_for("/dev/serial0", str(pi5)) == "/dev/ttyAMA0"
    assert gnss_device_for("/dev/serial0", str(pi4)) == "/dev/serial0"
    assert gnss_device_for("/dev/ttyUSB0", str(pi5)) == "/dev/ttyUSB0"  # chosen by hand: kept
    assert gnss_device_for("/dev/serial0", str(tmp_path / "missing")) == "/dev/serial0"
