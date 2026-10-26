"""Sensors attached to this Pi: an environment sensor (BME680) and a GPS/GNSS receiver.

On a Raspberry Pi with the RAK6421 HAT these are a RAK1906 (BME680 on I2C bus 1, address 0x76 or
0x77) in a sensor slot and a RAK12501 (Quectel L76K, NMEA at 9600 baud on the Pi's UART,
/dev/serial0) in slot A. The radio software (ZephCore) doesn't read them on the Pi, so MeshHome
does: live readings, a reading every few minutes for history, the /env bot command, and,
optionally, the radio's advertised location from the GPS.

Readings happen in worker threads, so a slow or missing device never holds up the radio or the
web app. Where there's no such hardware (Docker, Kubernetes, another computer), each sensor
simply shows as not found.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete

from app import db
from app.models import AuditEvent, SensorReading
from app.services import app_settings

log = logging.getLogger(__name__)

I2C_BUS = 1
BME680_ADDRESSES = (0x76, 0x77)
GNSS_BAUD = 9600
READ_SECONDS = 60  # environment reading
HISTORY_SECONDS = 300  # a stored reading
RETENTION_DAYS = 30
FRESH_SECONDS = 180  # a reading older than this is stale
GNSS_SILENT_SECONDS = 10  # no data for this long: the receiver isn't talking
LOCATION_MIN_INTERVAL = 600  # at most one location change on the radio per 10 minutes
RETRY_SECONDS = 60


# ---- NMEA (GPS) ---------------------------------------------------------------------------------


def _nmea_ok(line: str) -> list[str] | None:
    """The fields of a valid NMEA sentence (checksum verified), or None."""
    line = line.strip()
    if not line.startswith("$") or "*" not in line:
        return None
    body, _, given = line[1:].rpartition("*")
    try:
        want = int(given[:2], 16)
    except ValueError:
        return None
    calc = 0
    for ch in body.encode("ascii", "replace"):
        calc ^= ch
    return body.split(",") if calc == want else None


def _coord(value: str, hemi: str) -> float | None:
    """ddmm.mmmm / dddmm.mmmm and N/S/E/W -> signed decimal degrees."""
    if not value or not hemi:
        return None
    try:
        v = float(value)
    except ValueError:
        return None
    deg = int(v // 100)
    out = deg + (v - deg * 100) / 60
    return -out if hemi in ("S", "W") else out


def parse_nmea(line: str) -> dict[str, Any] | None:
    """What a GGA or RMC sentence says (other sentences, or invalid ones: None).
    GGA: fix quality, satellites, HDOP, altitude, position. RMC: valid flag, position, date/time."""
    f = _nmea_ok(line)
    if not f or len(f[0]) < 5:
        return None
    kind = f[0][-3:]
    if kind == "GGA" and len(f) >= 10:
        quality = int(f[6]) if f[6].isdigit() else 0
        out: dict[str, Any] = {"kind": "GGA", "fix": quality > 0}
        out["satellites"] = int(f[7]) if f[7].isdigit() else None
        with contextlib.suppress(ValueError):
            out["hdop"] = float(f[8])
        with contextlib.suppress(ValueError):
            out["alt_m"] = float(f[9])
        if quality > 0:
            out["lat"], out["lon"] = _coord(f[2], f[3]), _coord(f[4], f[5])
        return out
    if kind == "RMC" and len(f) >= 10:
        out = {"kind": "RMC", "fix": f[2] == "A"}
        if out["fix"]:
            out["lat"], out["lon"] = _coord(f[3], f[4]), _coord(f[5], f[6])
        t, d = f[1], f[9]
        if len(t) >= 6 and len(d) == 6 and t[:6].isdigit() and d.isdigit():
            with contextlib.suppress(ValueError):
                out["utc"] = datetime(
                    2000 + int(d[4:6]),
                    int(d[2:4]),
                    int(d[0:2]),
                    int(t[0:2]),
                    int(t[2:4]),
                    int(t[4:6]),
                    tzinfo=UTC,
                ).isoformat()
        return out
    return None


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (
        math.sin((p2 - p1) / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(a))


# ---- devices (replaceable in tests) -------------------------------------------------------------


class Bme680:
    """A BME680 via Pimoroni's driver: temperature, humidity, pressure and gas resistance."""

    def __init__(self, bus: int, address: int):
        import bme680
        import smbus2

        self._s = bme680.BME680(i2c_addr=address, i2c_device=smbus2.SMBus(bus))
        self._s.set_humidity_oversample(bme680.OS_2X)
        self._s.set_pressure_oversample(bme680.OS_4X)
        self._s.set_temperature_oversample(bme680.OS_8X)
        self._s.set_filter(bme680.FILTER_SIZE_3)
        self._s.set_gas_status(bme680.ENABLE_GAS_MEAS)
        self._s.set_gas_heater_temperature(320)
        self._s.set_gas_heater_duration(150)
        self._s.select_gas_heater_profile(0)

    def read(self) -> dict[str, float | None] | None:
        if not self._s.get_sensor_data():
            return None
        d = self._s.data
        return {
            "temperature_c": round(d.temperature, 2),
            "humidity": round(d.humidity, 1),
            "pressure_hpa": round(d.pressure, 1),
            "gas_ohms": round(d.gas_resistance) if d.heat_stable else None,
        }


def gnss_device_for(configured: str, model_file: str = "/proc/device-tree/model") -> str:
    """The serial port to read. On a Raspberry Pi 5, /dev/serial0 is the debug UART, and the GPIO
    header's UART (where a RAK6421's slot A is wired) is /dev/ttyAMA0."""
    if configured != "/dev/serial0":
        return configured
    try:
        with open(model_file, "rb") as f:
            model = f.read().decode("ascii", "replace")
    except OSError:
        return configured
    return "/dev/ttyAMA0" if "Raspberry Pi 5" in model else configured


def open_environment(bus: int, address: int):
    return Bme680(bus, address)


def open_serial(device: str):
    import serial

    return serial.Serial(device, GNSS_BAUD, timeout=1)


# ---- the manager --------------------------------------------------------------------------------


class Sensors:
    def __init__(self) -> None:
        self.env_status = "off"  # off | not_found | ok | error
        self.env_address: int | None = None
        self.env: dict[str, Any] | None = None  # latest reading, with "at" (epoch)
        self.gnss_status = "off"  # off | not_found | silent | no_fix | fix | error
        self.gnss_device: str | None = None
        self.gnss: dict[str, Any] = {}
        self.location_update: dict[str, Any] | None = None  # the last change made on the radio
        self._env_dev: Any = None
        self._env_retry_at = 0.0
        self._gnss_thread: threading.Thread | None = None
        self._gnss_stop: threading.Event | None = None
        self._gnss_last_data = 0.0
        self._history_at = 0.0
        self._prune_at = 0.0
        self._location_checked = 0.0
        self._task: asyncio.Task | None = None
        self._wake: asyncio.Event | None = None
        self.open_environment: Callable[[int, int], Any] = open_environment
        self.open_serial: Callable[[str], Any] = open_serial

    # lifecycle
    def start(self) -> None:
        if self._task is None:
            self._wake = asyncio.Event()
            self._task = asyncio.create_task(self._loop(), name="sensors")

    def wake(self) -> None:
        """Apply changed settings now, not at the next reading."""
        self._env_retry_at = 0.0
        self._location_checked = 0.0
        if self._wake:
            self._wake.set()

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        self._stop_gnss()

    # state for the API and the bot
    def env_fresh(self) -> dict[str, Any] | None:
        return self.env if self.env and time.time() - self.env["at"] <= FRESH_SECONDS * 5 else None

    def snapshot(self) -> dict[str, Any]:
        g = dict(self.gnss)
        return {
            "environment": {"status": self.env_status, "address": self.env_address, "reading": self.env},
            "gnss": {
                "status": self.gnss_status,
                "device": self.gnss_device,
                "reading": g or None,
                "location_update": self.location_update,
            },
        }

    def env_reply(self, units: str = "metric") -> str:
        """The /env reply, e.g. "🌡️ 21.4°C · 💧 48% RH · ⏲️ 1013.2 hPa · 🍃 air 120 kΩ"."""
        e = self.env_fresh()
        if not e:
            return "No environment readings on this node right now."
        imperial = units == "imperial"
        t = e["temperature_c"]
        parts = [f"🌡️ {t * 9 / 5 + 32:.1f}°F" if imperial else f"🌡️ {t:.1f}°C"]
        if e.get("humidity") is not None:
            parts.append(f"💧 {e['humidity']:.0f}% RH")
        if e.get("pressure_hpa") is not None:
            p = e["pressure_hpa"]
            parts.append(f"⏲️ {p * 0.0295300:.2f} inHg" if imperial else f"⏲️ {p:.1f} hPa")
        if e.get("gas_ohms"):
            parts.append(f"🍃 air {e['gas_ohms'] / 1000:.0f} kΩ")
        age = time.time() - e["at"]
        text = " · ".join(parts)
        return text if age < FRESH_SECONDS else f"{text} (as of {int(age // 60)} min ago)"

    # the loop
    async def _loop(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - sensors are a nicety; never stop the loop
                log.warning("sensors: %s", exc)
            if self._wake is None:
                await asyncio.sleep(READ_SECONDS)
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), READ_SECONDS)
            self._wake.clear()

    async def _tick(self) -> None:
        async with db.session_factory()() as s:
            cfg = await app_settings.get_sensors_config(s)
        await self._environment(cfg)
        self._gnss_follow(cfg)
        now = time.time()
        if now - self._history_at >= HISTORY_SECONDS:
            self._history_at = now
            await self._store()
        if now - self._prune_at >= 3600:
            self._prune_at = now
            async with db.session_factory()() as s:
                await s.execute(
                    delete(SensorReading).where(
                        SensorReading.at < datetime.now(UTC) - timedelta(days=RETENTION_DAYS)
                    )
                )
                await s.commit()
        if cfg.update_location:
            await self._maybe_update_location(cfg)

    async def _environment(self, cfg: app_settings.SensorsConfig) -> None:
        if not cfg.environment:
            self.env_status, self._env_dev, self.env = "off", None, None
            return
        if self._env_dev is None:
            if time.time() < self._env_retry_at:
                return
            self._env_dev, self.env_address = await asyncio.to_thread(self._find_environment)
            if self._env_dev is None:
                self.env_status = "not_found"
                self._env_retry_at = time.time() + RETRY_SECONDS * 10
                return
            log.info("sensors: environment sensor (BME680) found at 0x%02x", self.env_address)
        try:
            reading = await asyncio.to_thread(self._env_dev.read)
        except Exception as exc:  # noqa: BLE001 - an unplugged sensor raises OSError and the like
            log.warning("sensors: could not read the environment sensor: %s", exc)
            self.env_status, self._env_dev = "error", None
            self._env_retry_at = time.time() + RETRY_SECONDS
            return
        if reading:
            self.env = {**reading, "at": time.time()}
            self.env_status = "ok"

    def _find_environment(self) -> tuple[Any, int | None]:
        for address in BME680_ADDRESSES:
            try:
                return self.open_environment(I2C_BUS, address), address
            except Exception:  # noqa: BLE001, S112 - absent at this address (OSError, RuntimeError)
                continue
        return None, None

    # GPS
    def _gnss_follow(self, cfg: app_settings.SensorsConfig) -> None:
        if not cfg.gnss:
            self._stop_gnss()
            self.gnss_status, self.gnss = "off", {}
            return
        device = gnss_device_for(cfg.gnss_device)
        if self._gnss_thread is None or not self._gnss_thread.is_alive() or self.gnss_device != device:
            self._stop_gnss()
            self.gnss_device = device
            self._gnss_stop = threading.Event()
            self._gnss_thread = threading.Thread(
                target=self._gnss_run, args=(device, self._gnss_stop), name="gnss", daemon=True
            )
            self._gnss_thread.start()
        elif (
            self.gnss_status in ("no_fix", "fix") and time.time() - self._gnss_last_data > GNSS_SILENT_SECONDS
        ):
            self.gnss_status = "silent"

    def _stop_gnss(self) -> None:
        if self._gnss_stop:
            self._gnss_stop.set()
        self._gnss_thread = None
        self._gnss_stop = None

    def _gnss_run(self, device: str, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                port = self.open_serial(device)
            except Exception as exc:  # noqa: BLE001 - missing device, no permission
                self.gnss_status = (
                    "not_found" if "No such file" in str(exc) or "could not open" in str(exc) else "error"
                )
                if self.gnss_status == "error":
                    log.warning("sensors: could not open the GPS on %s: %s", device, exc)
                stop.wait(RETRY_SECONDS)
                continue
            if self.gnss_status in ("off", "not_found", "error"):
                self.gnss_status = "silent"
            try:
                while not stop.is_set():
                    raw = port.readline()
                    if not raw:
                        if time.time() - self._gnss_last_data > GNSS_SILENT_SECONDS:
                            self.gnss_status = "silent"
                        continue
                    self._gnss_line(raw.decode("ascii", "replace"))
            except Exception as exc:  # noqa: BLE001 - unplugged, port closed
                log.warning("sensors: the GPS on %s stopped: %s", device, exc)
                self.gnss_status = "error"
            finally:
                with contextlib.suppress(Exception):
                    port.close()
            stop.wait(5)

    def _gnss_line(self, line: str) -> None:
        p = parse_nmea(line)
        if p is None:
            return
        now = time.time()
        self._gnss_last_data = now
        g = dict(self.gnss)
        g["at"] = now
        if p["kind"] == "GGA":
            g.update({k: p[k] for k in ("satellites", "hdop", "alt_m") if k in p})
        if "utc" in p:
            g["utc"] = p["utc"]
        if p["fix"] and p.get("lat") is not None and p.get("lon") is not None:
            g.update({"fix": True, "lat": round(p["lat"], 6), "lon": round(p["lon"], 6), "fix_at": now})
        elif p["kind"] == "RMC" and not p["fix"]:
            g["fix"] = False
        self.gnss = g
        self.gnss_status = "fix" if g.get("fix") and now - g.get("fix_at", 0) < FRESH_SECONDS else "no_fix"

    def gnss_fix(self) -> dict[str, Any] | None:
        g = self.gnss
        if g.get("fix") and time.time() - g.get("fix_at", 0) < FRESH_SECONDS:
            return g
        return None

    # history
    async def _store(self) -> None:
        e = self.env if self.env and time.time() - self.env["at"] < FRESH_SECONDS else None
        g = self.gnss_fix()
        if not e and not g:
            return
        async with db.session_factory()() as s:
            s.add(
                SensorReading(
                    temperature_c=e and e.get("temperature_c"),
                    humidity=e and e.get("humidity"),
                    pressure_hpa=e and e.get("pressure_hpa"),
                    gas_ohms=e and e.get("gas_ohms"),
                    lat=g and g.get("lat"),
                    lon=g and g.get("lon"),
                    alt_m=g and g.get("alt_m"),
                    satellites=g and g.get("satellites"),
                )
            )
            await s.commit()

    # the radio's location from the GPS
    async def _maybe_update_location(self, cfg: app_settings.SensorsConfig) -> None:
        from app.radio.base import RadioError
        from app.radio.supervisor import supervisor

        g = self.gnss_fix()
        now = time.time()
        if not g or not supervisor.connected or now - self._location_checked < LOCATION_MIN_INTERVAL:
            return
        self._location_checked = now
        try:
            ident = (await supervisor.read_node_config()).get("identity") or {}
        except (RadioError, TimeoutError) as exc:
            log.info("sensors: could not read the radio's location: %s", exc)
            return
        lat, lon = ident.get("lat"), ident.get("lon")
        moved = None if lat is None or lon is None else distance_m(lat, lon, g["lat"], g["lon"])
        if moved is not None and moved < cfg.location_min_move_m:
            return
        params = {
            "name": ident.get("name", ""),
            "lat": g["lat"],
            "lon": g["lon"],
            "share_location": bool(ident.get("share_location")),
        }
        try:
            await supervisor.configure_node("identity", params)
        except (RadioError, TimeoutError) as exc:
            log.warning("sensors: could not set the radio's location from the GPS: %s", exc)
            return
        self.location_update = {"at": now, "lat": g["lat"], "lon": g["lon"], "moved_m": moved}
        log.info(
            "sensors: set the radio's location from the GPS (moved %s m)",
            None if moved is None else round(moved),
        )
        async with db.session_factory()() as s:
            s.add(
                AuditEvent(
                    kind="node.location_from_gps", detail={"moved_m": None if moved is None else round(moved)}
                )
            )
            await s.commit()


manager = Sensors()
