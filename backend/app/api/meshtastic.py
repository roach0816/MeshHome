"""Meshtastic: the nodes MeshHome's Meshtastic radio has heard (for Tracking), and that radio's
own settings (Settings → Node settings): its name, LoRa region and preset, role, position and
channels, with share links (QR codes) to put the same channels on trackers and other nodes."""

from __future__ import annotations

import base64
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, require_auth, require_session
from app.db import get_db
from app.meshtastic import proto as mt
from app.models import AuditEvent, MeshNode, TrackedDevice
from app.radio.base import NotSupported, RadioError
from app.radio.meshtastic_radio import MeshtasticRadio
from app.radio.supervisor import supervisor
from app.realtime import hub
from app.services import meshtastic

router = APIRouter(prefix="/api", tags=["meshtastic"])

ROLE_CHOICES = (
    "CLIENT",
    "CLIENT_MUTE",
    "CLIENT_BASE",
    "CLIENT_HIDDEN",
    "ROUTER",
    "ROUTER_LATE",
    "TRACKER",
    "SENSOR",
)
CHANNEL_NAME = r"^[A-Za-z0-9_-]{1,11}$"


class MeshNodeOut(BaseModel):
    num: int
    node_id: str  # "!a1b2c3d4"
    long_name: str | None
    short_name: str | None
    role: str | None  # CLIENT, ROUTER, TRACKER, ...
    last_heard_at: datetime | None
    has_position: bool
    position_at: datetime | None
    battery_level: int | None  # 101: on external power
    hops_away: int | None
    snr: float | None
    simulated: bool
    tracked: bool
    # This is the node MeshHome is connected to.
    is_self: bool


@router.get("/tracking/meshtastic/nodes", response_model=list[MeshNodeOut], tags=["tracking"])
async def list_mesh_nodes(ctx: AuthContext = Depends(require_auth), db: AsyncSession = Depends(get_db)):
    """The nodes the Meshtastic radio has heard, most recently heard first (to add as trackers)."""
    tracked = set(
        (
            await db.execute(
                select(TrackedDevice.mesh_node_num).where(TrackedDevice.mesh_node_num.is_not(None))
            )
        )
        .scalars()
        .all()
    )
    me = meshtastic.source.adapter.my_num if meshtastic.source.adapter is not None else None
    rows = (
        (await db.execute(select(MeshNode).order_by(MeshNode.last_heard_at.desc().nulls_last()).limit(300)))
        .scalars()
        .all()
    )
    return [
        MeshNodeOut(
            num=n.num,
            node_id=n.node_id,
            long_name=n.long_name,
            short_name=n.short_name,
            role=n.role,
            last_heard_at=n.last_heard_at,
            has_position=n.lat is not None,
            position_at=n.position_at,
            battery_level=n.battery_level,
            hops_away=n.hops_away,
            snr=n.snr,
            simulated=n.simulated,
            tracked=n.num in tracked,
            is_self=n.num == me,
        )
        for n in rows
    ]


# ---- the radio's own settings ------------------------------------------------------------------


def _radio() -> MeshtasticRadio:
    adapter = supervisor.adapter
    if not isinstance(adapter, MeshtasticRadio) or not supervisor.connected:
        raise HTTPException(status.HTTP_409_CONFLICT, "MeshHome isn't connected to a Meshtastic radio")
    return adapter


async def _configure(db: AsyncSession, op: str, params: dict, audit: dict) -> dict:
    _radio()
    try:
        result = await supervisor.configure_node(op, params)
    except NotSupported as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    except (RadioError, TimeoutError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"The radio didn't take the setting: {exc}"
        ) from None
    db.add(AuditEvent(kind=f"meshtastic.{op}", detail=audit))
    await db.commit()
    hub.publish("meshtastic-updated")
    return result


class MtNode(BaseModel):
    num: int
    node_id: str
    long_name: str
    short_name: str
    firmware: str | None
    role: str
    lat: float | None
    lon: float | None


class MtLora(BaseModel):
    region: str  # "US", "EU_868", ... "UNSET": the radio doesn't transmit until it's set
    preset: str  # "LONG_FAST", ... "CUSTOM"
    hop_limit: int
    tx_power_dbm: int  # 0: the region's maximum
    tx_enabled: bool


class MtChannel(BaseModel):
    index: int  # 0 is the primary channel
    name: str  # "" for an unnamed primary channel
    display_name: str  # what Meshtastic shows: an unnamed primary channel goes by its preset ("LongFast")
    primary: bool
    # "default": Meshtastic's well-known key (anyone can read it); "private": its own key; "none": unencrypted
    key: Literal["default", "private", "none"]
    # Bits of position precision on this channel: 32 is exact, 13 about 1.5 km, 0 none.
    position_precision: int


class MtConfig(BaseModel):
    node: MtNode
    lora: MtLora
    channels: list[MtChannel]
    regions: list[str]
    presets: list[str]
    roles: list[str]


@router.get("/node/meshtastic", response_model=MtConfig)
async def get_meshtastic_node(ctx: AuthContext = Depends(require_auth)):
    """The Meshtastic radio's settings (channel keys never leave the server)."""
    s = _radio().settings()
    return MtConfig(
        **s,
        regions=[v for k, v in mt.REGIONS.items() if k],
        presets=list(mt.PRESETS.values()),
        roles=list(ROLE_CHOICES),
    )


class OwnerIn(BaseModel):
    long_name: str = Field(min_length=1, max_length=24)
    short_name: str = Field(min_length=1, max_length=4)


@router.put("/node/meshtastic/owner", status_code=204)
async def put_meshtastic_owner(
    body: OwnerIn, ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)
):
    """The name other nodes see (long name, up to 24 bytes) and its short form (up to 4)."""
    await _configure(db, "owner", body.model_dump(), body.model_dump())


class LoraIn(BaseModel):
    region: str | None = None
    preset: str | None = None
    hop_limit: int | None = Field(default=None, ge=1, le=7)
    tx_power_dbm: int | None = Field(default=None, ge=0, le=30)


class LoraOut(BaseModel):
    # A region or preset change restarts the radio: it reconnects in a few seconds.
    restarts: bool


@router.put("/node/meshtastic/lora", response_model=LoraOut)
async def put_meshtastic_lora(
    body: LoraIn, ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)
):
    """LoRa settings. Every node on the mesh needs the same region and modem preset."""
    if body.region is not None and body.region not in mt.REGIONS.values():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown region")
    if body.preset is not None and body.preset not in mt.PRESETS.values():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown modem preset")
    result = await _configure(db, "radio", body.model_dump(exclude_none=True), body.model_dump())
    return LoraOut(restarts=bool(result.get("restarts")))


class RoleIn(BaseModel):
    role: str


@router.put("/node/meshtastic/role", status_code=204)
async def put_meshtastic_role(
    body: RoleIn, ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)
):
    """CLIENT is right for a base station; ROUTER only for a well-placed relay (it rebroadcasts
    everything)."""
    if body.role not in ROLE_CHOICES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown role")
    await _configure(db, "role", body.model_dump(), body.model_dump())


class PositionIn(BaseModel):
    # Both set: a fixed position (where the radio is); both null: no fixed position.
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)


@router.put("/node/meshtastic/position", status_code=204)
async def put_meshtastic_position(
    body: PositionIn, ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)
):
    if (body.lat is None) != (body.lon is None):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Give both latitude and longitude, or neither"
        )
    name = _radio().settings()["node"]["long_name"]
    await _configure(db, "identity", {"name": name, **body.model_dump()}, {"fixed": body.lat is not None})


class ChannelIn(BaseModel):
    name: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{0,11}$")
    # "random": a new private key (everyone on the channel needs the new QR code); "default":
    # Meshtastic's well-known key. Null keeps the key (a new channel gets a random one).
    key: Literal["random", "default"] | None = None
    position_precision: int | None = Field(default=None, ge=0, le=32)


class ChannelOut(BaseModel):
    index: int


@router.post("/node/meshtastic/channels", response_model=ChannelOut, status_code=201)
async def add_meshtastic_channel(
    body: ChannelIn, ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)
):
    """A new secondary channel in the first free slot (a private key unless `key` says otherwise)."""
    if not body.name:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Give the channel a name")
    result = await _configure(
        db, "channel", {**body.model_dump(), "index": None}, {"name": body.name, "key": body.key or "random"}
    )
    return ChannelOut(index=int(result["index"]))


@router.put("/node/meshtastic/channels/{index}", response_model=ChannelOut)
async def put_meshtastic_channel(
    body: ChannelIn,
    index: int,
    ctx: AuthContext = Depends(require_session),
    db: AsyncSession = Depends(get_db),
):
    if not 0 <= index <= 7:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Channels are numbered 0 to 7")
    if not any(c["index"] == index for c in _radio().settings()["channels"]) and index != 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No channel in that slot")
    await _configure(
        db,
        "channel",
        {**body.model_dump(), "index": index},
        {"index": index, "name": body.name, "key": body.key},
    )
    return ChannelOut(index=index)


@router.delete("/node/meshtastic/channels/{index}", status_code=204)
async def delete_meshtastic_channel(
    index: int, ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)
):
    if index == 0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "The primary channel can't be removed")
    if not any(c["index"] == index for c in _radio().settings()["channels"]):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No channel in that slot")
    await _configure(db, "channel_clear", {"index": index}, {"index": index})


class ShareUrlOut(BaseModel):
    # A Meshtastic share link (https://meshtastic.org/e/#…) with the channels and LoRa settings, as
    # its QR code holds it. It contains the channels' keys: anyone with it can read them.
    url: str


@router.get("/node/meshtastic/share-url", response_model=ShareUrlOut)
async def get_meshtastic_share_url(
    channels: str | None = Query(default=None, pattern=r"^[0-7](,[0-7])*$"),
    ctx: AuthContext = Depends(require_session),
):
    """The share link for the given channels (comma-separated indexes; all of them by default)."""
    picked = [int(x) for x in channels.split(",")] if channels else None
    url = _radio().channel_url(picked)
    if url is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such channel")
    return ShareUrlOut(url=url)


class LinkIn(BaseModel):
    url: str = Field(min_length=10, max_length=2000)


class LinkChannel(BaseModel):
    name: str
    key: Literal["default", "private", "none"]


class LinkPreview(BaseModel):
    channels: list[LinkChannel]
    region: str | None
    preset: str | None


def _channel_set(url: str) -> dict:
    url = url.strip()
    _, sep, frag = url.partition("#")
    if not sep or "/e/" not in url:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "That isn't a Meshtastic channel link")
    try:
        cs = mt.decode(base64.urlsafe_b64decode(frag + "=" * (-len(frag) % 4)), mt.CHANNEL_SET)
    except (ValueError, mt.ProtoError):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "The channel link is damaged") from None
    if not cs.get("settings"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "The link has no channels")
    return cs


def _preview(cs: dict) -> LinkPreview:
    lora = cs.get("lora_config") or {}
    out = []
    for i, s in enumerate(cs.get("settings") or []):
        psk = s.get("psk") or b""
        name = s.get("name") or ("" if i else "(primary)")
        out.append(
            LinkChannel(name=name, key="none" if not psk else "default" if len(psk) == 1 else "private")
        )
    return LinkPreview(
        channels=out,
        region=mt.REGIONS.get(int(lora.get("region") or 0)) if lora else None,
        preset=mt.PRESETS.get(int(lora.get("modem_preset") or 0)) if lora else None,
    )


@router.post("/node/meshtastic/link-preview", response_model=LinkPreview)
async def preview_meshtastic_link(body: LinkIn, ctx: AuthContext = Depends(require_session)):
    """What a Meshtastic share link holds (channel names, key kinds, region and preset)."""
    return _preview(_channel_set(body.url))


class ImportIn(LinkIn):
    # False: add its channels in free slots. True: replace all channels and take its LoRa settings
    # (as the Meshtastic apps' "Replace" does).
    replace: bool = False


class ImportOut(BaseModel):
    added: int
    restarts: bool


@router.post("/node/meshtastic/import", response_model=ImportOut)
async def import_meshtastic_link(
    body: ImportIn, ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)
):
    """Put a Meshtastic share link's channels on the radio (joining another group's channels)."""
    cs = _channel_set(body.url)
    preview = _preview(cs)
    result = await _configure(
        db,
        "channels_import",
        {"channel_set": cs, "replace": body.replace},
        {"replace": body.replace, "channels": [c.name for c in preview.channels]},
    )
    return ImportOut(added=int(result.get("added") or 0), restarts=bool(result.get("restarts")))
