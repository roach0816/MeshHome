"""Read and change settings on the connected MeshCore node.

Validation mirrors the MeshCore companion firmware (examples/companion_radio/MyMesh.cpp), so
obviously invalid values are rejected before anything is sent to the radio. Channel keys are
write-only: they are never returned, except a newly generated random key, once, to the owner.
"""

import base64
import binascii
import secrets
import time
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, require_auth, require_session
from app.db import get_db
from app.models import AuditEvent
from app.radio.base import PUBLIC_CHANNEL_KEY, NotSupported, RadioError, hashtag_key
from app.radio.supervisor import supervisor
from app.services import radio_presets

router = APIRouter(prefix="/api/radio", tags=["radio-config"])

# Standard LoRa bandwidths (kHz) accepted by SX126x/SX127x radios.
LORA_BANDWIDTHS = (7.8, 10.4, 15.6, 20.8, 31.25, 41.7, 62.5, 125.0, 250.0, 500.0)
MAX_NAME_BYTES = 31  # node_name[32] including the terminator


MAX_SCOPE_CHARS = 30  # the firmware stores the "#"-prefixed name in 31 bytes


def _name_ok(v: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError("Name cannot be empty")
    if len(v.encode("utf-8")) > MAX_NAME_BYTES:
        raise ValueError(f"Name must be at most {MAX_NAME_BYTES} bytes")
    return v


def _scope_ok(v: str | None) -> str | None:
    """Region scope names are stored without the leading "#"; empty means no scope."""
    if v is None:
        return None
    v = v.strip().lstrip("#")
    if any(c.isspace() for c in v):
        raise ValueError("Scope names cannot contain spaces")
    if len(v.encode("utf-8")) > MAX_SCOPE_CHARS:
        raise ValueError(f"Scope names must be at most {MAX_SCOPE_CHARS} bytes")
    return v


class IdentityIn(BaseModel):
    name: str
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    share_location: bool = False

    @field_validator("name")
    @classmethod
    def _n(cls, v: str) -> str:
        return _name_ok(v)

    @model_validator(mode="after")
    def _both_or_neither(self):
        if (self.lat is None) != (self.lon is None):
            raise ValueError("Enter both latitude and longitude, or neither")
        return self


class RadioIn(BaseModel):
    freq_mhz: float = Field(ge=150, le=2500)
    bw_khz: float
    sf: int = Field(ge=5, le=12)
    cr: int = Field(ge=5, le=8)
    # The firmware accepts -9 dBm, but the client library encodes power as unsigned.
    tx_power_dbm: int = Field(ge=1, le=30)
    repeat: bool | None = None

    @field_validator("bw_khz")
    @classmethod
    def _bw(cls, v: float) -> float:
        if not any(abs(v - b) < 0.01 for b in LORA_BANDWIDTHS):
            raise ValueError("Choose a standard LoRa bandwidth")
        return v


class BehaviorIn(BaseModel):
    auto_add_contacts: bool
    multi_acks: int = Field(ge=0, le=1)
    path_hash_mode: int | None = Field(default=None, ge=0, le=2)
    default_flood_scope: str | None = Field(default=None, max_length=31)

    @field_validator("default_flood_scope")
    @classmethod
    def _scope(cls, v: str | None) -> str | None:
        return _scope_ok(v)


TelemetryMode = Literal[0, 1, 2]  # deny / only contacts flagged for telemetry / everyone


class TelemetryIn(BaseModel):
    base: TelemetryMode
    location: TelemetryMode
    environment: TelemetryMode


class TuningIn(BaseModel):
    rx_delay: float = Field(ge=0, le=20)
    airtime_factor: float = Field(ge=0, le=9)


class ChannelIn(BaseModel):
    name: str
    # keep: reuse the slot's current key; hashtag: derived from "#name"; public: the well-known
    # MeshCore Public key; random: new 128-bit key (returned once); custom: provided below.
    key_mode: Literal["keep", "hashtag", "public", "random", "custom"]
    key: str | None = Field(default=None, max_length=64)
    # Region scope for messages sent on this channel (app-side). Omit to leave it unchanged.
    flood_scope: str | None = Field(default=None, max_length=31)

    @field_validator("name")
    @classmethod
    def _n(cls, v: str) -> str:
        v = _name_ok(v)
        if v.startswith("#") and (len(v) < 2 or any(c.isspace() for c in v)):
            raise ValueError('Hashtag names need at least one character after "#" and no spaces')
        return v

    @field_validator("flood_scope")
    @classmethod
    def _s(cls, v: str | None) -> str | None:
        return _scope_ok(v)

    @model_validator(mode="after")
    def _consistent(self):
        if self.key_mode == "hashtag" and not self.name.startswith("#"):
            raise ValueError('Hashtag channels need a name starting with "#"')
        if self.key_mode != "hashtag" and self.name.startswith("#"):
            raise ValueError('Names starting with "#" always use the key derived from the name')
        if self.key_mode == "custom" and not self.key:
            raise ValueError("Enter the channel key")
        return self


class ChannelAddIn(ChannelIn):
    key_mode: Literal["hashtag", "public", "random", "custom"]


class ChannelScopeIn(BaseModel):
    flood_scope: str | None = Field(default=None, max_length=31)

    @field_validator("flood_scope")
    @classmethod
    def _s(cls, v: str | None) -> str | None:
        return _scope_ok(v)


class CustomVarIn(BaseModel):
    key: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_.-]+$")
    value: str = Field(max_length=64)


class AdvertIn(BaseModel):
    flood: bool = False


def _parse_key(text: str) -> bytes:
    t = text.strip()
    try:
        raw = bytes.fromhex(t) if len(t) == 32 else base64.b64decode(t, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Key must be 32 hex characters or base64"
        ) from exc
    if len(raw) != 16:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Key must be exactly 128 bits (16 bytes)")
    return raw


async def _apply(db: AsyncSession, op: str, params: dict[str, Any], audit: dict | None = None) -> None:
    try:
        await supervisor.configure_node(op, params)
    except NotSupported as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except (RadioError, TimeoutError) as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"The radio did not accept the change: {exc}"
        ) from exc
    db.add(AuditEvent(kind=f"node.{op}", detail=audit or {}))
    await db.commit()


async def _config():
    try:
        return await supervisor.read_node_config()
    except NotSupported as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except (RadioError, TimeoutError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Cannot read node settings: {exc}") from exc


@router.get("/config")
async def get_config(ctx: AuthContext = Depends(require_auth)):
    return await _config()


@router.get("/presets")
async def get_presets(ctx: AuthContext = Depends(require_auth)):
    """MeshCore's suggested radio presets by country/region (see app.services.radio_presets)."""
    return await radio_presets.catalog.get()


@router.put("/config/identity")
async def put_identity(
    body: IdentityIn, ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)
):
    await _apply(db, "identity", body.model_dump(), {"share_location": body.share_location})
    return await _config()


@router.put("/config/radio")
async def put_radio(
    body: RadioIn, ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)
):
    current = await _config()
    max_power = (current.get("radio") or {}).get("max_tx_power_dbm")
    if max_power and body.tx_power_dbm > max_power:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"This radio allows at most {max_power} dBm"
        )
    if current.get("radio", {}).get("repeat") is None:
        body.repeat = None  # firmware without client repeat support
    await _apply(db, "radio", body.model_dump(), body.model_dump())
    return await _config()


@router.put("/config/behavior")
async def put_behavior(
    body: BehaviorIn, ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)
):
    await _apply(db, "behavior", body.model_dump(), body.model_dump())
    return await _config()


@router.put("/config/telemetry")
async def put_telemetry(
    body: TelemetryIn, ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)
):
    await _apply(db, "telemetry", body.model_dump(), body.model_dump())
    return await _config()


@router.put("/config/tuning")
async def put_tuning(
    body: TuningIn, ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)
):
    await _apply(db, "tuning", body.model_dump(), body.model_dump())
    return await _config()


def _secret_for(body: ChannelIn) -> tuple[bytes | None, bytes | None]:
    """(secret to write, newly generated key). None secret means "derive from #name"."""
    if body.key_mode == "public":
        return PUBLIC_CHANNEL_KEY, None
    if body.key_mode == "random":
        key = secrets.token_bytes(16)
        return key, key
    if body.key_mode == "custom":
        return _parse_key(body.key or ""), None
    return None, None  # hashtag


def _key_formats(key: bytes) -> dict[str, str]:
    return {"hex": key.hex(), "base64": base64.b64encode(key).decode()}


@router.get("/channels/hashtag-key")
async def hashtag_channel_key(name: str, ctx: AuthContext = Depends(require_auth)):
    """The key of a "#name" channel. It is derived from the public name, so it is not a secret."""
    try:
        name = ChannelIn(name=name, key_mode="hashtag").name
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Enter a name starting with #") from exc
    return {"name": name, **_key_formats(hashtag_key(name))}


@router.post("/channels")
async def add_channel(
    body: ChannelAddIn, ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)
):
    """Add a channel in the first free slot (the "Add channel" flows)."""
    current = await _config()
    max_channels = int(current.get("max_channels") or 0)
    used = {ch["slot"] for ch in current.get("channels", []) if ch.get("name")}
    slot = next((i for i in range(max_channels) if i not in used), None)
    if slot is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"All {max_channels} channel slots on the radio are in use. Remove a channel in Node settings first.",
        )
    secret, new_key = _secret_for(body)
    effective = hashtag_key(body.name) if secret is None else secret
    try:
        existing = await supervisor.radio_channels()
    except (RadioError, TimeoutError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Cannot read the radio's channels: {exc}") from exc
    for ch in existing:
        if ch.secret == effective:
            # Two slots with one key would receive every message twice.
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"This channel is already on the radio as {ch.name} (slot {ch.slot})",
            )
    await _apply(
        db,
        "channel",
        {"slot": slot, "name": body.name, "secret": secret},
        {"slot": slot, "key_mode": body.key_mode, "added": True},
    )
    conversation_id = await supervisor.set_channel_scope(slot, body.flood_scope or None)
    result: dict[str, Any] = {
        "slot": slot,
        "name": body.name,
        "conversation_id": conversation_id,
        "config": await _config(),
    }
    if body.key_mode != "custom":
        # Public and hashtag keys are well known or derived from the name. A new random key is
        # returned this once so it can be shared; it is never stored or returned again.
        result["share"] = _key_formats(effective)
    return result


@router.put("/channels/{slot}/scope")
async def put_channel_scope(
    slot: int,
    body: ChannelScopeIn,
    ctx: AuthContext = Depends(require_session),
    db: AsyncSession = Depends(get_db),
):
    conversation_id = await supervisor.set_channel_scope(slot, body.flood_scope or None)
    if conversation_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No active channel in that slot")
    db.add(AuditEvent(kind="channel.scope", detail={"slot": slot, "scoped": bool(body.flood_scope)}))
    await db.commit()
    return {"flood_scope": body.flood_scope or None}


@router.put("/channels/{slot}")
async def put_channel(
    slot: int,
    body: ChannelIn,
    ctx: AuthContext = Depends(require_session),
    db: AsyncSession = Depends(get_db),
):
    current = await _config()
    if not 0 <= slot < int(current.get("max_channels") or 0):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such channel slot on this radio")
    if body.key_mode == "keep":
        secret, new_key = await supervisor.current_channel_secret(slot), None
        if secret is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "This slot has no key to keep; choose a key")
    else:
        secret, new_key = _secret_for(body)
    await _apply(
        db,
        "channel",
        {"slot": slot, "name": body.name, "secret": secret},
        {"slot": slot, "key_mode": body.key_mode},
    )
    if "flood_scope" in body.model_fields_set:
        await supervisor.set_channel_scope(slot, body.flood_scope or None)
    result: dict[str, Any] = {"config": await _config()}
    if new_key is not None:
        # Shown once so it can be shared with other members; never stored or returned again.
        result["new_key"] = _key_formats(new_key)
    return result


@router.delete("/channels/{slot}")
async def clear_channel(
    slot: int, ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)
):
    current = await _config()
    if not 0 <= slot < int(current.get("max_channels") or 0):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such channel slot on this radio")
    await _apply(db, "channel_clear", {"slot": slot}, {"slot": slot})
    return await _config()


@router.put("/custom-vars")
async def put_custom_var(
    body: CustomVarIn, ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)
):
    await _apply(db, "custom_var", body.model_dump(), {"key": body.key})
    return await _config()


@router.post("/actions/advert")
async def send_advert(
    body: AdvertIn, ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)
):
    await _apply(db, "advert", body.model_dump(), body.model_dump())
    return {"ok": True}


@router.post("/actions/sync-clock")
async def sync_clock(ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)):
    await _apply(db, "sync_clock", {"epoch": int(time.time())})
    return {"ok": True}


@router.post("/actions/reboot")
async def reboot(ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)):
    await _apply(db, "reboot", {})
    return {"ok": True, "detail": "Rebooting. The app will reconnect automatically."}
