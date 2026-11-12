"""Tracking: where chosen devices are, such as a GPS tracker carried by a runner.

MeshCore companions don't send their position on a timer: an advert goes out when someone asks for
one. So MeshHome asks. On each device's interval it sends a telemetry request, and the device
answers with its latest GPS fix (and battery voltage) when it shares its location with this node
(MeshCore app → Telemetry → Location). Adverts that carry a position are recorded too.

Moving devices change routes. When a request on the stored route goes unanswered, the route is
forgotten and the request is sent again by flood; the reply teaches the radio the new route, so a
runner who moves between repeaters costs at most one missed position. With back-off on (the
default, kinder to a busy shared mesh), a device that keeps not answering is asked less often, up
to every 5 minutes, until it answers again.

Requests give way to repeater administration (services/remote_admin.py), and nothing is sent
while tracking is off or the radio isn't connected. Positions are kept for 30 days.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select

from app import db
from app.models import Contact, TrackedDevice, TrackPosition, utcnow
from app.radio.base import RadioError, advert_position, hops_from_path
from app.radio.supervisor import supervisor
from app.realtime import hub
from app.services import app_settings, remote_admin

log = logging.getLogger(__name__)

RETENTION_DAYS = 30
INTERVALS = (60, 120, 300, 600, 900)  # seconds a device can be asked at
BACK_OFF_AFTER = 3  # unanswered polls in a row
BACK_OFF_MAX = 300.0
MIN_WAIT = 8.0
MAX_WAIT = 30.0
IDLE_SECONDS = 30.0
ERROR_RETRY_SECONDS = 15.0
# Distinct on the map, on light and dark tiles, and against the course colours.
COLORS = ("#e11d48", "#2563eb", "#7c3aed", "#0891b2", "#ea580c", "#db2777", "#4f46e5", "#0d9488")

NO_POSITION = (
    "It answered without a position. Its GPS may not have a fix yet, or it doesn't share its "
    "location with this node (on the tracker, MeshCore app → Telemetry → Location)."
)


def parse_telemetry(result: Any) -> tuple[dict[str, float | None] | None, float | None]:
    """The position ({"lat", "lon", "alt_m"}) and battery voltage in a telemetry reply (Cayenne
    LPP, as the meshcore library decodes it). The node's own readings are on channel 1."""
    lpp = result.get("lpp") if isinstance(result, dict) else None
    fix: dict[str, float | None] | None = None
    battery: float | None = None
    for e in lpp if isinstance(lpp, list) else []:
        if not isinstance(e, dict):
            continue
        own = e.get("channel") == 1
        v = e.get("value")
        if e.get("type") == "gps" and isinstance(v, dict) and (fix is None or own):
            lat, lon = advert_position(v.get("latitude"), v.get("longitude"))
            if lat is not None and lon is not None:
                alt = v.get("altitude")
                fix = {"lat": lat, "lon": lon, "alt_m": float(alt) if isinstance(alt, int | float) else None}
        elif e.get("type") == "voltage" and isinstance(v, int | float) and (battery is None or own):
            battery = round(float(v), 2)
    return fix, battery


def parse_sensors(result: Any) -> dict[str, float]:
    """Readings from its environment sensors, when it shares them (MeshCore app → Telemetry →
    Environment): temperature (°C), barometric pressure (hPa) and the altitude worked out from
    it (m). The node's own sensors are on channel 1; other channels are preferred less."""
    lpp = result.get("lpp") if isinstance(result, dict) else None
    names = {"temperature": "temp_c", "barometer": "pressure_hpa", "altitude": "baro_alt_m"}
    out: dict[str, float] = {}
    own: set[str] = set()
    for e in lpp if isinstance(lpp, list) else []:
        if not isinstance(e, dict) or e.get("type") not in names:
            continue
        key, v = names[e["type"]], e.get("value")
        if (
            isinstance(v, int | float)
            and not isinstance(v, bool)
            and (key not in out or (e.get("channel") == 1 and key not in own))
        ):
            out[key] = round(float(v), 2)
            if e.get("channel") == 1:
                own.add(key)
    return out


def reply_route(heard: dict[str, Any] | None, contact_meta: dict[str, Any]) -> dict[str, Any] | None:
    """How a reply reached the radio: {"route", "hops" (tracker's end first), "snr", "rssi"}.
    A flood packet lists the repeaters it passed. A direct one arrives with an empty list (each
    repeater removes itself), so its hops are the stored route, reversed (it's ours to them)."""
    if heard is None:
        return None
    hops = list(heard.get("hops") or [])
    route = heard.get("route") or "direct"
    if route == "direct" and not hops:
        path_hex = str(contact_meta.get("path_hex") or "")
        n = int(contact_meta.get("path_len") if contact_meta.get("path_len") is not None else -1)
        if n > 0 and path_hex and len(path_hex) % (2 * n) == 0:
            hops = list(reversed(hops_from_path(path_hex, n, len(path_hex) // (2 * n))))
    return {"route": route, "hops": hops, "snr": heard.get("snr"), "rssi": heard.get("rssi")}


# Resting voltage -> charge, for a standard Li-ion/LiPo cell (full at 4.2 V) and a high-voltage
# one (full at 4.35-4.4 V, like the SenseCAP X1's). Rough: load and temperature move it a little.
_LIION = (
    (4.20, 100),
    (4.10, 90),
    (4.00, 80),
    (3.92, 70),
    (3.86, 60),
    (3.81, 50),
    (3.78, 40),
    (3.75, 30),
    (3.72, 20),
    (3.68, 10),
    (3.60, 5),
    (3.30, 0),
)
_LIHV = (
    (4.33, 100),
    (4.22, 90),
    (4.11, 80),
    (4.02, 70),
    (3.94, 60),
    (3.88, 50),
    (3.83, 40),
    (3.79, 30),
    (3.75, 20),
    (3.70, 10),
    (3.62, 5),
    (3.30, 0),
)
HIGH_VOLTAGE_ABOVE = 4.25
LOW_BATTERY_PCT = 20


def battery_percent(volts: float | None, max_seen: float | None = None) -> int | None:
    """About how full a single-cell battery is, from its voltage. A battery that has ever read
    above 4.25 V is taken to be a high-voltage one."""
    if volts is None or not 2.5 <= volts <= 5.0:
        return None
    table = _LIHV if (max_seen or 0) > HIGH_VOLTAGE_ABOVE or volts > HIGH_VOLTAGE_ABOVE else _LIION
    if volts >= table[0][0]:
        return 100
    for (v1, p1), (v2, p2) in zip(table, table[1:], strict=False):
        if volts >= v2:
            return round(p2 + (p1 - p2) * (volts - v2) / (v1 - v2))
    return 0


def next_interval(interval_s: int, failures: int, back_off: bool) -> float:
    if back_off and failures >= BACK_OFF_AFTER:
        return max(float(interval_s), min(interval_s * 2.0 ** (failures - BACK_OFF_AFTER + 1), BACK_OFF_MAX))
    return float(interval_s)


class Tracking:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()  # one poll at a time
        self._next_due: dict[uuid.UUID, float] = {}  # device id -> monotonic time
        self._polling: uuid.UUID | None = None
        self._prune_at = 0.0
        self._asked_at = 0.0  # when the last request went out (for matching its reply packet)

    # ---- lifecycle -------------------------------------------------------------------

    def start(self) -> None:
        self.__init__()
        supervisor.contacts_synced_hook = self.on_contacts_synced
        self._task = asyncio.create_task(self._loop(), name="tracking")

    async def stop(self) -> None:
        if supervisor.contacts_synced_hook == self.on_contacts_synced:
            supervisor.contacts_synced_hook = None
        if self._task:
            self._task.cancel()
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (asyncio.CancelledError, TimeoutError):
                pass
            self._task = None

    def wake(self) -> None:
        self._wake.set()

    def poll_soon(self, device_id: uuid.UUID) -> None:
        self._next_due[device_id] = 0.0
        self._wake.set()

    def forget(self, device_id: uuid.UUID) -> None:
        self._next_due.pop(device_id, None)

    def next_poll_in(self, device_id: uuid.UUID) -> float | None:
        due = self._next_due.get(device_id)
        return None if due is None else max(0.0, due - time.monotonic())

    @property
    def polling_now(self) -> uuid.UUID | None:
        return self._polling

    # ---- the schedule ----------------------------------------------------------------

    async def _loop(self) -> None:
        while True:
            try:
                delay = await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep tracking alive; try again shortly
                log.warning("tracking: %s", exc)
                delay = 10.0
            try:
                await asyncio.wait_for(self._wake.wait(), delay)
            except TimeoutError:
                pass
            self._wake.clear()

    async def _tick(self) -> float:
        now = time.monotonic()
        if now - self._prune_at >= 3600:
            self._prune_at = now
            await self._prune()
        async with db.session_factory()() as s:
            cfg = await app_settings.get_tracking_config(s)
            radio = supervisor.radio
            if not cfg.enabled or radio is None or not supervisor.connected:
                return IDLE_SECONDS
            ids = (
                (
                    await s.execute(
                        select(TrackedDevice.id)
                        .join(Contact, Contact.id == TrackedDevice.contact_id)
                        .where(TrackedDevice.polling, Contact.radio_id == radio.id)
                    )
                )
                .scalars()
                .all()
            )
        if not ids:
            return IDLE_SECONDS
        for i in ids:
            self._next_due.setdefault(i, now)
        device_id = min(ids, key=lambda i: self._next_due[i])
        wait = self._next_due[device_id] - now
        if wait > 0:
            return min(wait, IDLE_SECONDS)
        if remote_admin.busy():
            return 3.0  # give way to repeater administration
        await self.poll(device_id, cfg)
        return 0.2

    # ---- one poll --------------------------------------------------------------------

    async def _ask(self, public_key: str) -> tuple[str, Any]:
        self._asked_at = time.monotonic()
        try:
            ticket = await supervisor.remote_send(public_key, "telemetry")
            wait = min(max(ticket.timeout * 1.5, MIN_WAIT), MAX_WAIT)
            return "reply", await supervisor.remote_wait(ticket, wait)
        except TimeoutError:
            return "timeout", None
        except RadioError as exc:
            return "error", str(exc) or "the radio couldn't send the request"

    async def poll(self, device_id: uuid.UUID, cfg: app_settings.TrackingConfig | None = None) -> str | None:
        """Ask one device for its position now. Returns the outcome (its new status)."""
        async with self._lock:
            async with db.session_factory()() as s:
                row = (
                    await s.execute(
                        select(TrackedDevice, Contact)
                        .join(Contact, Contact.id == TrackedDevice.contact_id)
                        .where(TrackedDevice.id == device_id)
                    )
                ).first()
                if cfg is None:
                    cfg = await app_settings.get_tracking_config(s)
            if row is None:
                self.forget(device_id)
                return None
            device, contact = row
            started = time.monotonic()
            self._next_due[device_id] = started + device.interval_s  # until this poll is done
            self._polling = device_id
            hub.publish("tracking-updated", device_id=str(device_id))
            try:
                outcome, data = await self._ask(contact.public_key)
                flooded = False
                meta = contact.meta or {}
                had_route = int(meta.get("path_len") if meta.get("path_len") is not None else -1) >= 0
                if outcome == "timeout" and had_route and await supervisor.forget_route(contact.public_key):
                    # Probably moved out of that route: flood, and learn the new route from the reply.
                    flooded = True
                    outcome, data = await self._ask(contact.public_key)
                link = await self._reply_link(contact) if outcome == "reply" else None
                status = await self._record(device_id, outcome, data, flooded, link)
            finally:
                self._polling = None
            async with db.session_factory()() as s:
                d = await s.get(TrackedDevice, device_id)
                if d is not None:
                    wait = next_interval(d.interval_s, d.failures, cfg.back_off)
                    if status == "error":
                        # Nothing was sent (e.g. the radio was reconnecting): try again soon.
                        wait = min(wait, ERROR_RETRY_SECONDS)
                    self._next_due[device_id] = started + wait
            hub.publish("tracking-updated", device_id=str(device_id))
            return status

    async def _reply_link(self, contact: Contact) -> dict[str, Any] | None:
        """The reply's route and signal, from the radio's packet log (logged just before the reply
        is decoded; a moment's grace for the event order)."""
        heard = None
        for _ in range(3):
            heard = supervisor.reply_heard(contact.public_key, self._asked_at)
            if heard is not None:
                break
            await asyncio.sleep(0.15)
        if heard is None:
            return None
        async with db.session_factory()() as s:
            c = await s.get(Contact, contact.id)  # its route now, as the reply may have taught it
            meta = (c.meta if c is not None else None) or {}
        return reply_route(heard, meta)

    async def _record(
        self, device_id: uuid.UUID, outcome: str, data: Any, flooded: bool, link: dict[str, Any] | None = None
    ) -> str | None:
        now = utcnow()
        async with db.session_factory()() as s:
            d = await s.get(TrackedDevice, device_id, with_for_update=True)
            if d is None:
                return None
            d.last_poll_at = now
            if outcome == "reply":
                fix, battery = parse_telemetry(data)
                d.last_reply_at = now
                d.failures = 0
                if battery is not None:
                    d.battery_v = battery
                    d.battery_max_v = max(battery, d.battery_max_v or 0.0)
                if fix is not None:
                    d.status, d.status_detail = "ok", None
                    s.add(
                        TrackPosition(
                            device_id=device_id,
                            at=now,
                            source="telemetry",
                            battery_v=battery,
                            **fix,
                            **parse_sensors(data),
                            **(link or {}),
                        )
                    )
                else:
                    d.status, d.status_detail = "no_position", NO_POSITION
            elif outcome == "timeout":
                d.failures += 1
                d.status = "no_answer"
                d.status_detail = (
                    "No answer"
                    + (", on its route or by flood" if flooded else "")
                    + ". It may be out of range or switched off, or it may not have this node in its contacts."
                )
            else:
                d.status, d.status_detail = "error", str(data)[:300]
            status = d.status
            await s.commit()
        return status

    # ---- adverts ---------------------------------------------------------------------

    async def on_contacts_synced(self, radio_id: uuid.UUID) -> None:
        """After the radio's contact table was read: a tracked device's newer advert with a
        position becomes a position too."""
        recorded = False
        now = utcnow()
        async with db.session_factory()() as s:
            if not (await app_settings.get_tracking_config(s)).enabled:
                return
            rows = (
                await s.execute(
                    select(TrackedDevice, Contact)
                    .join(Contact, Contact.id == TrackedDevice.contact_id)
                    .where(
                        Contact.radio_id == radio_id,
                        Contact.lat.is_not(None),
                        Contact.lon.is_not(None),
                        Contact.last_advert_at.is_not(None),
                    )
                    .with_for_update(of=TrackedDevice)
                )
            ).all()
            for d, c in rows:
                if d.advert_seen_at is not None and c.last_advert_at <= d.advert_seen_at:
                    continue
                d.advert_seen_at = c.last_advert_at
                at = min(c.last_advert_at, now)
                if at < now - timedelta(days=RETENTION_DAYS):
                    continue
                s.add(TrackPosition(device_id=d.id, at=at, lat=c.lat, lon=c.lon, source="advert"))
                recorded = True
            await s.commit()
        if recorded:
            hub.publish("tracking-updated")

    async def _prune(self) -> None:
        try:
            async with db.session_factory()() as s:
                await s.execute(
                    delete(TrackPosition).where(
                        TrackPosition.at < datetime.now(UTC) - timedelta(days=RETENTION_DAYS)
                    )
                )
                await s.commit()
        except Exception as exc:  # noqa: BLE001
            log.info("could not prune old tracked positions: %s", exc)


manager = Tracking()
