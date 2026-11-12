"""Meshtastic trackers (services/meshtastic.py): where they're heard, the nodes heard, and the team
channel the trackers share with this node."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, require_auth, require_session
from app.api.tracking import MeshtasticStatus, meshtastic_status
from app.db import get_db
from app.meshtastic import proto as mt
from app.meshtastic.client import MeshtasticError
from app.models import AuditEvent, MeshNode, TrackedDevice
from app.realtime import hub
from app.services import app_settings, meshtastic

router = APIRouter(prefix="/api", tags=["tracking"])


@router.get("/settings/meshtastic", response_model=app_settings.MeshtasticConfig)
async def get_meshtastic_settings(
    ctx: AuthContext = Depends(require_auth), db: AsyncSession = Depends(get_db)
):
    return await app_settings.get_meshtastic_config(db)


@router.put("/settings/meshtastic", response_model=app_settings.MeshtasticConfig)
async def put_meshtastic_settings(
    body: app_settings.MeshtasticConfig,
    ctx: AuthContext = Depends(require_session),
    db: AsyncSession = Depends(get_db),
):
    """Where Meshtastic trackers are heard: off, the radio HAT on this Pi (meshtasticd), a node on
    the network (host, port 4403) or a simulated node. Runs while tracking is on."""
    if body.source == "network" and not body.host:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Enter the Meshtastic node's address")
    await app_settings.put_meshtastic_config(db, body)
    db.add(AuditEvent(kind="settings.meshtastic", detail=body.model_dump()))
    await db.commit()
    meshtastic.source.reload()
    hub.publish("settings-updated")
    hub.publish("meshtastic-updated")
    return body


class Choice(BaseModel):
    value: int
    name: str


class MeshtasticOut(BaseModel):
    status: MeshtasticStatus | None
    regions: list[Choice]
    presets: list[Choice]


@router.get("/tracking/meshtastic", response_model=MeshtasticOut)
async def get_meshtastic(ctx: AuthContext = Depends(require_auth)):
    """The Meshtastic node MeshHome is connected to (None while Meshtastic is off), and the LoRa
    regions and presets to choose from when setting up the team channel."""
    return MeshtasticOut(
        status=meshtastic_status(),
        regions=[Choice(value=k, name=v) for k, v in mt.REGIONS.items() if k],
        presets=[Choice(value=k, name=v) for k, v in mt.PRESETS.items()],
    )


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


@router.get("/tracking/meshtastic/nodes", response_model=list[MeshNodeOut])
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
    snap = meshtastic.source.snapshot
    me = snap.my_num if snap is not None else None
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


class TeamChannelIn(BaseModel):
    name: str = Field(min_length=1, max_length=11, pattern=r"^[A-Za-z0-9_-]+$")
    # LoRa region and modem preset (values from GET /api/tracking/meshtastic); null keeps the
    # node's. A region must be set for the radio to transmit.
    region: int | None = None
    preset: int | None = None


@router.post("/tracking/meshtastic/channel", status_code=202)
async def setup_team_channel(
    body: TeamChannelIn, ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)
):
    """Give the Meshtastic node a new private primary channel (a random key, positions at full
    precision) and set its region and preset. Scan the channel's QR code (GET …/channel-url) into
    each tracker to join it. Changing the region or preset restarts the node."""
    if body.region is not None and body.region not in mt.REGIONS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown region")
    if body.preset is not None and body.preset not in mt.PRESETS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown preset")
    try:
        await meshtastic.source.setup_team_channel(body.name, body.region, body.preset)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    except (MeshtasticError, ConnectionError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, f"The node didn't take the settings: {exc}") from None
    db.add(
        AuditEvent(
            kind="meshtastic.team_channel",
            detail={"name": body.name, "region": body.region, "preset": body.preset},
        )
    )
    await db.commit()
    hub.publish("meshtastic-updated")
    return {"queued": True}


class ChannelUrlOut(BaseModel):
    # The Meshtastic share link for the node's primary channel and LoRa settings, as its QR code
    # holds it. It contains the channel's key: anyone with it can read the channel.
    url: str


@router.get("/tracking/meshtastic/channel-url", response_model=ChannelUrlOut)
async def get_channel_url(ctx: AuthContext = Depends(require_session)):
    url = meshtastic.source.channel_url() if meshtastic.source.connected else None
    if url is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "MeshHome isn't connected to the Meshtastic node")
    return ChannelUrlOut(url=url)
