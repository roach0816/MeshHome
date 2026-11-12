"""Installation-specific settings captured by the setup wizard and stored in the database."""

import secrets
from typing import Literal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppSetting, User, utcnow

# "hat": the radio HAT on this Pi (ZephCore on 127.0.0.1:5000, managed by the native installer).
RadioMode = Literal["simulated", "tcp", "hat", "none"]
# The mesh technology MeshHome runs: everything (contacts, channels, messages, tracking) follows it.
RadioTech = Literal["meshcore", "meshtastic"]
MESHTASTIC_TCP_PORT = 4403


class RadioConfig(BaseModel):
    # With the radio HAT, the technology is what the HAT runs (effective_tech).
    tech: RadioTech = "meshcore"
    mode: RadioMode = "simulated"
    host: str = ""
    port: int = Field(default=5000, ge=1, le=65535)
    # Maintenance pause: closes the TCP connection so a maintenance client can take over.
    paused: bool = False
    # Simulated mode only: average seconds between generated incoming messages (0 disables).
    sim_interval_seconds: int = Field(default=60, ge=0, le=3600)

    @field_validator("host")
    @classmethod
    def _strip_host(cls, v: str) -> str:
        v = v.strip()
        if any(c.isspace() for c in v) or "/" in v:
            raise ValueError("Enter a hostname or IP address only (no scheme or path)")
        return v


class InstallationConfig(BaseModel):
    home_name: str = "Home"
    setup_completed_at: str | None = None


OSM_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"


class MapConfig(BaseModel):
    # Any XYZ raster tile server. OpenStreetMap's public tiles are the default; their usage policy
    # asks for attribution and light, interactive use, which fits a single household.
    tile_url: str = Field(default=OSM_TILE_URL, max_length=500)
    attribution: str = Field(default="© OpenStreetMap contributors", max_length=200)
    max_zoom: int = Field(default=19, ge=1, le=22)
    # Name the place at a node's advertised position ("Springfield, Illinois, US") in its details,
    # looked up by this server from OpenStreetMap Nominatim (app/services/places.py).
    place_names: bool = True
    # Use the tiles saved on this server (Settings → Map → Offline map) instead of the tile server,
    # e.g. at an event with no internet. Places outside the saved area show no map.
    offline: bool = False

    @field_validator("tile_url")
    @classmethod
    def _valid_template(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith("https://"):
            raise ValueError("Tile URL must start with https://")
        if not all(p in v for p in ("{z}", "{x}", "{y}")):
            raise ValueError("Tile URL must contain {z}, {x} and {y}")
        return v


class NotificationConfig(BaseModel):
    # Sound for new incoming messages, played by open browser tabs. Per-conversation overrides
    # (conversations.sound) take precedence.
    sound: Literal["off", "all", "dms"] = "dms"


NOTIFICATIONS_KEY = "notifications"


class AppInstallConfig(BaseModel):
    # Lets browsers install MeshHome as an app (web app manifest and service worker; the frontend's
    # lib/pwa.ts). Off until the owner turns it on, which needs HTTPS: browsers only allow installing
    # and service workers on secure pages.
    enabled: bool = False


APP_INSTALL_KEY = "app_install"
# This server's VAPID key pair for web push (app/services/web_push.py), created on first use.
WEB_PUSH_KEYS_KEY = "web_push_keys"


class BotConfig(BaseModel):
    # Answer DMs that start with "/" (see app/services/bot.py). Off until the owner turns it on.
    enabled: bool = False
    # Who may use it: favourite contacts only, or every known contact.
    allow: Literal["favorites", "everyone"] = "favorites"
    # /env: the environment sensor's readings (services/sensors.py), for those allowed above.
    env: bool = True


BOT_KEY = "bot"


class RoutingConfig(BaseModel):
    # The repeater (its public key) that direct traffic goes through whenever that works; see
    # services/preferred_route.py. None: leave routing to the radio.
    preferred_repeater: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")

    @field_validator("preferred_repeater")
    @classmethod
    def _lower(cls, v: str | None) -> str | None:
        return v.lower() if v else None


ROUTING_KEY = "routing"


class WeatherConfig(BaseModel):
    # Before bot apps: the Ecowitt gateway for /weather. Read once to install the Ecowitt
    # app with the same address (bot_apps.migrate), and kept so a rollback still finds it.
    host: str = Field(default="", max_length=253)

    @field_validator("host")
    @classmethod
    def _host_only(cls, v: str) -> str:
        v = v.strip()
        if v and (any(c.isspace() for c in v) or "/" in v or "@" in v or "?" in v or "#" in v):
            raise ValueError("Enter only the gateway's IP address or hostname (optionally :port)")
        return v


class SensorsConfig(BaseModel):
    # Sensors attached to this Pi (services/sensors.py): read them when they're found.
    environment: bool = True  # a BME680 on I2C (e.g. a RAK1906 in a RAK6421 sensor slot)
    gnss: bool = True  # a GPS/GNSS receiver sending NMEA on a serial port (e.g. a RAK12501)
    gnss_device: str = Field(default="/dev/serial0", pattern=r"^/dev/[A-Za-z0-9_./-]{1,40}$")
    # Keep the radio's advertised location up to date from the GPS, when it has moved this far.
    update_location: bool = False
    location_min_move_m: int = Field(default=100, ge=20, le=10000)
    # The app-wide units setting (UnitsConfig), kept here too for clients that read it from the
    # sensors' settings; saving the sensors' settings saves it.
    units: Literal["metric", "imperial"] = "metric"


class UnitsConfig(BaseModel):
    # How measurements are shown everywhere: radio sensors, the /env bot reply and tracking.
    # Stored values are always metric, and so is the API.
    units: Literal["metric", "imperial"] = "metric"


class TrackingConfig(BaseModel):
    # Tracking (services/tracking.py): ask chosen contacts, such as GPS trackers, for their
    # position on a schedule and show them on their own map. Off until turned on.
    enabled: bool = False
    # After 3 unanswered polls in a row, ask less often (up to every 5 minutes) until it answers.
    # Turn off on your own small mesh (an event) to keep asking at the device's interval.
    back_off: bool = True
    # How much of each device's recent path the map draws behind it.
    trail_minutes: int = Field(default=30, ge=0, le=1440)


# Meshtastic trackers (services/meshtastic.py): where MeshHome hears them. "hat": meshtasticd on
# this Pi's radio HAT (127.0.0.1:4403, set up by the native installer); "network": a Meshtastic node
# with Wi-Fi or Ethernet (its API on TCP port 4403); "simulated": a simulated node and trackers.
MeshtasticSource = Literal["off", "hat", "network", "simulated"]


class MeshtasticConfig(BaseModel):
    source: MeshtasticSource = "off"
    host: str = Field(default="", max_length=253)
    port: int = Field(default=4403, ge=1, le=65535)

    @field_validator("host")
    @classmethod
    def _strip_host(cls, v: str) -> str:
        v = v.strip()
        if any(c.isspace() for c in v) or "/" in v:
            raise ValueError("Enter a hostname or IP address only (no scheme or path)")
        return v


# 1.1.x only (migrate_meshtastic_settings): Meshtastic trackers had a connection of their own, and
# the MeshCore radio mode from before the HAT was switched to Meshtastic was kept here.
MESHTASTIC_KEY = "meshtastic"
HAT_SWITCH_KEY = "radio_hat_switch"
SENSORS_KEY = "sensors"
TRACKING_KEY = "tracking"
UNITS_KEY = "units"
WEATHER_KEY = "weather"
RADIO_KEY = "radio"
MAP_KEY = "map"
INSTALLATION_KEY = "installation"
FINGERPRINT_KEY = "fingerprint_key"


async def _get(db: AsyncSession, key: str) -> dict | None:
    row = await db.get(AppSetting, key)
    return row.value if row else None


async def _put(db: AsyncSession, key: str, value: dict) -> None:
    stmt = insert(AppSetting).values(key=key, value=value, updated_at=utcnow())
    stmt = stmt.on_conflict_do_update(
        index_elements=[AppSetting.key], set_={"value": value, "updated_at": utcnow()}
    )
    await db.execute(stmt)


async def get_raw(db: AsyncSession, key: str) -> dict | None:
    """A setting stored as free-form JSON (e.g. the installed bot apps)."""
    return await _get(db, key)


async def put_raw(db: AsyncSession, key: str, value: dict) -> None:
    await _put(db, key, value)


async def get_radio_config(db: AsyncSession) -> RadioConfig:
    raw = await _get(db, RADIO_KEY)
    return RadioConfig.model_validate(raw) if raw else RadioConfig(mode="none")


async def put_radio_config(db: AsyncSession, cfg: RadioConfig) -> None:
    await _put(db, RADIO_KEY, cfg.model_dump())


async def get_map_config(db: AsyncSession) -> MapConfig:
    raw = await _get(db, MAP_KEY)
    return MapConfig.model_validate(raw) if raw else MapConfig()


async def put_map_config(db: AsyncSession, cfg: MapConfig) -> None:
    await _put(db, MAP_KEY, cfg.model_dump())


async def get_notification_config(db: AsyncSession) -> NotificationConfig:
    raw = await _get(db, NOTIFICATIONS_KEY)
    return NotificationConfig.model_validate(raw) if raw else NotificationConfig()


async def put_notification_config(db: AsyncSession, cfg: NotificationConfig) -> None:
    await _put(db, NOTIFICATIONS_KEY, cfg.model_dump())


async def get_app_install_config(db: AsyncSession) -> AppInstallConfig:
    raw = await _get(db, APP_INSTALL_KEY)
    return AppInstallConfig.model_validate(raw) if raw else AppInstallConfig()


async def put_app_install_config(db: AsyncSession, cfg: AppInstallConfig) -> None:
    await _put(db, APP_INSTALL_KEY, cfg.model_dump())


async def get_web_push_keys(db: AsyncSession) -> dict[str, str] | None:
    raw = await _get(db, WEB_PUSH_KEYS_KEY)
    return raw if raw and raw.get("private_key") and raw.get("public_key") else None


async def put_web_push_keys(db: AsyncSession, keys: dict[str, str]) -> None:
    await _put(db, WEB_PUSH_KEYS_KEY, keys)


async def get_bot_config(db: AsyncSession) -> BotConfig:
    raw = await _get(db, BOT_KEY)
    return BotConfig.model_validate(raw) if raw else BotConfig()


async def put_bot_config(db: AsyncSession, cfg: BotConfig) -> None:
    await _put(db, BOT_KEY, cfg.model_dump())


async def get_routing_config(db: AsyncSession) -> RoutingConfig:
    raw = await _get(db, ROUTING_KEY)
    return RoutingConfig.model_validate(raw) if raw else RoutingConfig()


async def put_routing_config(db: AsyncSession, cfg: RoutingConfig) -> None:
    await _put(db, ROUTING_KEY, cfg.model_dump())


async def get_weather_config(db: AsyncSession) -> WeatherConfig:
    raw = await _get(db, WEATHER_KEY)
    return WeatherConfig.model_validate(raw) if raw else WeatherConfig()


async def put_weather_config(db: AsyncSession, cfg: WeatherConfig) -> None:
    await _put(db, WEATHER_KEY, cfg.model_dump())


async def get_units(db: AsyncSession) -> UnitsConfig:
    raw = await _get(db, UNITS_KEY)
    if raw:
        return UnitsConfig.model_validate(raw)
    # Before 1.0.20 the choice was the sensors' own; it carries over.
    sensors = await _get(db, SENSORS_KEY)
    return UnitsConfig(units=SensorsConfig.model_validate(sensors).units if sensors else "metric")


async def put_units(db: AsyncSession, cfg: UnitsConfig) -> None:
    await _put(db, UNITS_KEY, cfg.model_dump())


async def get_sensors_config(db: AsyncSession) -> SensorsConfig:
    raw = await _get(db, SENSORS_KEY)
    cfg = SensorsConfig.model_validate(raw) if raw else SensorsConfig()
    cfg.units = (await get_units(db)).units
    return cfg


async def put_sensors_config(db: AsyncSession, cfg: SensorsConfig) -> None:
    await _put(db, SENSORS_KEY, cfg.model_dump())
    await put_units(db, UnitsConfig(units=cfg.units))


def effective_tech(cfg: RadioConfig) -> RadioTech:
    """The technology in use: the radio HAT's is what it runs (it can be switched outside the app,
    with `meshhome radio-hat setup`)."""
    if cfg.mode == "hat":
        from app.services import radio_hat

        return radio_hat.firmware()  # type: ignore[return-value]
    return cfg.tech


async def get_tech(db: AsyncSession) -> RadioTech:
    return effective_tech(await get_radio_config(db))


async def migrate_meshtastic_settings(db: AsyncSession) -> bool:
    """From 1.1.x: Meshtastic trackers had a connection of their own (Settings → Tracking), and
    switching the HAT to Meshtastic set the radio mode to None. Now Meshtastic is a technology of
    the radio connection itself, so that connection becomes the radio's. Caller commits."""
    raw = await _get(db, MESHTASTIC_KEY)
    switch = await _get(db, HAT_SWITCH_KEY) or {}
    if raw is None and not switch:
        return False
    radio = await get_radio_config(db)
    old = MeshtasticConfig.model_validate(raw) if raw else MeshtasticConfig()
    if radio.mode == "none":
        if switch.get("radio_mode") == "hat" or old.source == "hat":
            radio = radio.model_copy(update={"mode": "hat", "tech": "meshtastic"})
        elif old.source == "network" and old.host:
            radio = radio.model_copy(
                update={"mode": "tcp", "tech": "meshtastic", "host": old.host, "port": old.port}
            )
        elif old.source == "simulated":
            radio = radio.model_copy(update={"mode": "simulated", "tech": "meshtastic"})
        await put_radio_config(db, radio)
    for key in (MESHTASTIC_KEY, HAT_SWITCH_KEY):
        row = await db.get(AppSetting, key)
        if row is not None:
            await db.delete(row)
    await db.flush()
    return True


async def get_tracking_config(db: AsyncSession) -> TrackingConfig:
    raw = await _get(db, TRACKING_KEY)
    return TrackingConfig.model_validate(raw) if raw else TrackingConfig()


async def put_tracking_config(db: AsyncSession, cfg: TrackingConfig) -> None:
    await _put(db, TRACKING_KEY, cfg.model_dump())


async def get_installation(db: AsyncSession) -> InstallationConfig:
    raw = await _get(db, INSTALLATION_KEY)
    return InstallationConfig.model_validate(raw) if raw else InstallationConfig()


async def put_installation(db: AsyncSession, cfg: InstallationConfig) -> None:
    await _put(db, INSTALLATION_KEY, cfg.model_dump())


async def get_fingerprint_key(db: AsyncSession) -> bytes:
    """Per-installation key for channel fingerprints; generated on first use, never exposed."""
    raw = await _get(db, FINGERPRINT_KEY)
    if raw:
        return bytes.fromhex(raw["hex"])
    key = secrets.token_bytes(32)
    await _put(db, FINGERPRINT_KEY, {"hex": key.hex()})
    return key


async def setup_complete(db: AsyncSession) -> bool:
    return (await db.execute(select(User.id).limit(1))).first() is not None
