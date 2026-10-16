"""Radio snapshots in backups: a TCP radio's identity, settings, channels and contacts.

A TCP radio keeps these in its own flash, so reflashing its firmware can lose them. A backup made
while the radio is connected also holds a snapshot of it (radio.json, inside the backup's
encryption). Restoring a backup never writes to a radio: putting a snapshot back is a separate
step the owner chooses (Settings → Backup & restore → Restore a radio), because writing an identity
key makes the radio become that node, and two radios with one identity confuse the mesh.

The radio HAT isn't covered here: its data is files on the Pi, in the backup's system archive.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

from app.radio import identity
from app.radio.base import NotSupported, RadioError
from app.radio.supervisor import supervisor

log = logging.getLogger(__name__)

FORMAT = 1
PARTS = ("identity", "settings", "channels", "contacts")
SETTINGS = ("identity", "radio", "behavior", "telemetry", "tuning", "custom_vars")
READ_TIMEOUT = 30.0
MAX_CONTACT_FAILURES = 3  # in a row: the radio is probably full


class ChannelSnap(BaseModel):
    slot: int = Field(ge=0, le=255)
    name: str = Field(max_length=64)
    secret: str = Field(pattern=r"^[0-9a-f]{32}$")


class ContactSnap(BaseModel):
    """A contact record as the companion protocol stores it (base.CONTACT_RECORD_FIELDS)."""

    public_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    type: int = Field(ge=0, le=255)
    flags: int = Field(ge=0, le=255)
    out_path_len: int = Field(ge=-1, le=64)
    out_path: str = Field(default="", max_length=128, pattern=r"^[0-9a-f]*$")
    out_path_hash_mode: int = Field(ge=-1, le=3)
    adv_name: str = Field(max_length=64)
    last_advert: int = Field(ge=0, le=2**32 - 1)
    adv_lat: float = Field(ge=-90, le=90)
    adv_lon: float = Field(ge=-180, le=180)


class Snapshot(BaseModel):
    format: int = FORMAT
    captured_at: str
    public_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    # The 64-byte firmware key, hex. key says why it's missing: disabled (the firmware doesn't
    # allow exporting it) or unavailable (it couldn't be read).
    private_key: str | None = Field(default=None, pattern=r"^[0-9a-f]{128}$")
    key: Literal["included", "disabled", "unavailable"]
    name: str = Field(max_length=64)
    firmware: dict[str, Any] = Field(default_factory=dict)
    # As read_config returns them: identity, radio, behavior, telemetry, tuning, custom_vars.
    config: dict[str, Any] = Field(default_factory=dict)
    channels: list[ChannelSnap] = Field(default_factory=list, max_length=256)
    contacts: list[ContactSnap] | None = Field(default=None, max_length=5000)  # None: not read

    @model_validator(mode="after")
    def _key_matches(self) -> Snapshot:
        if (self.private_key is None) != (self.key != "included"):
            raise ValueError("inconsistent identity key")
        if self.private_key and identity.public_key(bytes.fromhex(self.private_key)) != self.public_key:
            raise ValueError("the identity key doesn't belong to this radio")
        return self


def parse(raw: bytes) -> Snapshot:
    """A snapshot from a backup. ValueError if it can't be used."""
    try:
        return Snapshot.model_validate_json(raw)
    except ValidationError as exc:
        raise ValueError(f"The radio snapshot in this backup is damaged ({exc.errors()[0]['msg']})") from None


# ---- taking a snapshot ---------------------------------------------------------------------------


async def capture() -> Snapshot:
    """A snapshot of the connected radio. RadioError if its basic settings can't be read; a missing
    key or contact list is recorded in the snapshot instead."""
    device = await supervisor.call("get_device_snapshot")
    config = await supervisor.call("read_config", timeout=READ_TIMEOUT)
    channels = await supervisor.call("get_channels", timeout=READ_TIMEOUT)
    private_key, key = None, "unavailable"
    try:
        raw = await supervisor.call("export_private_key")
        if identity.public_key(raw) == device.public_key.lower():
            private_key, key = raw.hex(), "included"
        else:
            log.warning("radio backup: the exported identity key doesn't match the radio's public key")
    except NotSupported:
        key = "disabled"
    except (RadioError, TimeoutError, ValueError) as exc:
        log.info("radio backup: identity key not read: %s", exc)
    contacts = None
    try:
        contacts = await supervisor.call("contact_records", timeout=READ_TIMEOUT)
    except (RadioError, NotSupported, TimeoutError) as exc:
        log.info("radio backup: contacts not read: %s", exc)
    return Snapshot(
        captured_at=datetime.now(UTC).isoformat(),
        public_key=device.public_key.lower(),
        private_key=private_key,
        key=key,
        name=device.name,
        firmware=dict(config.get("firmware") or {}),
        config={k: config[k] for k in SETTINGS if config.get(k) is not None},
        channels=[ChannelSnap(slot=c.slot, name=c.name, secret=c.secret.hex()) for c in channels],
        contacts=None if contacts is None else [ContactSnap(**c) for c in contacts],
    )


def summary(s: Snapshot) -> dict[str, Any]:
    """What a snapshot holds, for the owner (never the key itself)."""
    radio = s.config.get("radio") or {}
    return {
        "name": s.name,
        "public_key": s.public_key,
        "captured_at": s.captured_at,
        "firmware": s.firmware.get("version"),
        "model": s.firmware.get("model"),
        "key": s.key,
        "channels": [c.name for c in s.channels],
        "contacts": None if s.contacts is None else len(s.contacts),
        "radio": {k: radio.get(k) for k in ("freq_mhz", "bw_khz", "sf", "cr", "tx_power_dbm")},
    }


def describe(s: Snapshot) -> str:
    """One line for the backup's summary."""
    held = ["its identity key"] if s.key == "included" else []
    held += ["settings", f"{len(s.channels)} channel{'s' if len(s.channels) != 1 else ''}"]
    if s.contacts is not None:
        held.append(f"{len(s.contacts)} contact{'s' if len(s.contacts) != 1 else ''}")
    return f"{s.name} ({s.public_key[:8]}…): " + ", ".join(held)


KEY_NOTES = {
    "disabled": "The radio's firmware doesn't allow exporting its identity key, so it isn't in this backup.",
    "unavailable": "The radio's identity key couldn't be read, so it isn't in this backup.",
}


# ---- putting a snapshot back ---------------------------------------------------------------------


async def _configure(op: str, params: dict[str, Any]) -> None:
    await supervisor.call("configure", op, params, timeout=READ_TIMEOUT)


async def _settings(s: Snapshot) -> list[str]:
    """Write the snapshot's settings, checked like the Device settings page; what failed."""
    from app.api.radio_config import BehaviorIn, IdentityIn, RadioIn, TelemetryIn, TuningIn

    now = await supervisor.call("read_config", timeout=READ_TIMEOUT)
    cfg, failed = s.config, []

    async def apply(label: str, op: str, build) -> None:
        try:
            params = build()
            if params is not None:
                await _configure(op, params)
        except (ValidationError, ValueError, KeyError, TypeError):
            failed.append(f"{label}: the backup's value can't be used")
        except (RadioError, NotSupported, TimeoutError) as exc:
            failed.append(f"{label}: {exc}")

    def radio() -> dict[str, Any] | None:
        if "radio" not in cfg:
            return None
        r = dict(cfg["radio"])
        r.pop("max_tx_power_dbm", None)
        here = now.get("radio") or {}
        if here.get("max_tx_power_dbm") and r["tx_power_dbm"] > here["max_tx_power_dbm"]:
            r["tx_power_dbm"] = here["max_tx_power_dbm"]  # this hardware's limit
        if here.get("repeat") is None:
            r["repeat"] = None  # firmware without client repeat
        return RadioIn(**r).model_dump()

    def behavior() -> dict[str, Any] | None:
        if "behavior" not in cfg:
            return None
        b, here = dict(cfg["behavior"]), now.get("behavior") or {}
        for k in ("path_hash_mode", "default_flood_scope"):
            if here.get(k) is None:
                b[k] = None  # this firmware doesn't have it
        return BehaviorIn(**b).model_dump()

    await apply(
        "Name and location",
        "identity",
        lambda: cfg.get("identity") and IdentityIn(**cfg["identity"]).model_dump(),
    )
    await apply("Radio", "radio", radio)
    await apply("Behaviour", "behavior", behavior)
    await apply(
        "Telemetry",
        "telemetry",
        lambda: cfg.get("telemetry") and TelemetryIn(**cfg["telemetry"]).model_dump(),
    )
    await apply("Tuning", "tuning", lambda: cfg.get("tuning") and TuningIn(**cfg["tuning"]).model_dump())
    # Only variables this firmware has, and only where they differ.
    here_vars = now.get("custom_vars") or {}
    for key, value in (cfg.get("custom_vars") or {}).items():
        if key in here_vars and here_vars[key] != value:
            await apply(
                f"Variable {key}",
                "custom_var",
                lambda key=key, value=value: {"key": key, "value": str(value)},
            )
    return failed


async def _channels(s: Snapshot) -> list[str]:
    """Make the radio's channels the snapshot's (slots it doesn't have are cleared); what failed."""
    now = await supervisor.call("read_config", timeout=READ_TIMEOUT)
    max_channels = int(now.get("max_channels") or 0)
    failed = []
    wanted = {c.slot for c in s.channels}
    for c in s.channels:
        if max_channels and c.slot >= max_channels:
            failed.append(f"{c.name}: this radio has only {max_channels} channel slots")
            continue
        try:
            await _configure("channel", {"slot": c.slot, "name": c.name, "secret": bytes.fromhex(c.secret)})
        except (RadioError, NotSupported, TimeoutError) as exc:
            failed.append(f"{c.name}: {exc}")
    for c in await supervisor.call("get_channels", timeout=READ_TIMEOUT):
        if c.slot not in wanted:
            try:
                await _configure("channel_clear", {"slot": c.slot})
            except (RadioError, NotSupported, TimeoutError) as exc:
                failed.append(f"Clearing {c.name}: {exc}")
    return failed


async def _contacts(s: Snapshot) -> tuple[int, int, str | None]:
    """Add the snapshot's contacts the radio doesn't have: (added, failed, last error)."""
    have = {c.public_key.lower() for c in await supervisor.call("get_contacts", timeout=READ_TIMEOUT)}
    missing = [c for c in s.contacts or [] if c.public_key not in have]
    added = failed = in_a_row = 0
    error = None
    for i, c in enumerate(missing):
        try:
            await supervisor.call("add_contact_record", c.model_dump())
            added += 1
            in_a_row = 0
        except (RadioError, NotSupported, TimeoutError) as exc:
            failed += 1
            in_a_row += 1
            error = str(exc)
            if in_a_row >= MAX_CONTACT_FAILURES:
                failed += len(missing) - i - 1  # not tried
                break
    return added, failed, error


async def restore(s: Snapshot, parts: set[str]) -> list[dict[str, Any]]:
    """Write the chosen parts of a snapshot to the connected radio, then reconnect so MeshHome
    reads it afresh. One step per part: {"part", "ok", "detail"}."""
    steps: list[dict[str, Any]] = []

    def step(part: str, ok: bool, detail: str) -> None:
        steps.append({"part": part, "ok": ok, "detail": detail})

    # The identity first: importing a key makes the firmware reload its contacts.
    if "identity" in parts:
        current = await supervisor.call("get_device_snapshot")
        if s.private_key is None:
            step("identity", False, KEY_NOTES[s.key])
        elif current.public_key.lower() == s.public_key:
            step("identity", True, "The radio already has this identity.")
        else:
            try:
                await supervisor.call("import_private_key", bytes.fromhex(s.private_key))
                step("identity", True, f"The radio is now {s.public_key[:8]}…")
            except NotSupported as exc:
                step("identity", False, str(exc))
            except (RadioError, TimeoutError) as exc:
                step("identity", False, f"The radio didn't accept the identity key: {exc}")
    if "settings" in parts:
        failed = await _settings(s)
        step(
            "settings", not failed, "; ".join(failed) or "Name, location, radio and other settings restored."
        )
    if "channels" in parts:
        failed = await _channels(s)
        step("channels", not failed, "; ".join(failed) or f"{len(s.channels)} channels restored.")
    if "contacts" in parts:
        if s.contacts is None:
            step("contacts", False, "The backup has no contact list from the radio.")
        else:
            added, failed, error = await _contacts(s)
            detail = f"{added} contact{'s' if added != 1 else ''} added"
            if failed:
                detail += f"; {failed} not added ({error})"
            step("contacts", not failed, detail + ".")
    supervisor.reload()
    await asyncio.sleep(0)  # let the reconnect start
    return steps
