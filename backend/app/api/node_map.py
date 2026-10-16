"""Node map: positions that nodes chose to include in their adverts."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, require_auth
from app.db import get_db
from app.models import AuditEvent, Contact, Conversation, Radio
from app.radio.supervisor import supervisor
from app.services import app_settings, places

router = APIRouter(prefix="/api", tags=["map"])


class MapNode(BaseModel):
    id: uuid.UUID
    public_key: str
    name: str
    alias: str | None
    kind: int
    lat: float
    lon: float
    last_advert_at: datetime | None
    on_radio: bool
    is_simulated: bool
    conversation_id: uuid.UUID | None


class MapGateway(BaseModel):
    name: str
    lat: float
    lon: float
    is_simulated: bool
    live: bool


class MapData(BaseModel):
    gateways: list[MapGateway]
    nodes: list[MapNode]
    without_location: int
    tiles: app_settings.MapConfig


@router.get("/map", response_model=MapData)
async def map_data(ctx: AuthContext = Depends(require_auth), db: AsyncSession = Depends(get_db)):
    rows = (
        await db.execute(
            select(Contact, Radio.is_simulated, Conversation.id)
            .join(Radio, Radio.id == Contact.radio_id)
            .outerjoin(Conversation, Conversation.contact_id == Contact.id)
        )
    ).all()
    nodes, without = [], 0
    for c, sim, conv_id in rows:
        if c.lat is None or c.lon is None:
            without += 1
            continue
        nodes.append(
            MapNode(
                id=c.id,
                public_key=c.public_key,
                name=c.name,
                alias=c.alias,
                kind=c.kind,
                lat=c.lat,
                lon=c.lon,
                last_advert_at=c.last_advert_at,
                on_radio=c.on_radio,
                is_simulated=sim,
                conversation_id=conv_id,
            )
        )
    live_id = supervisor.radio.id if supervisor.connected and supervisor.radio else None
    gateways = []
    for r in (await db.execute(select(Radio))).scalars():
        loc = (r.self_info or {}).get("location") or {}
        if loc.get("lat") is not None and loc.get("lon") is not None:
            gateways.append(
                MapGateway(
                    name=r.name,
                    lat=loc["lat"],
                    lon=loc["lon"],
                    is_simulated=r.is_simulated,
                    live=r.id == live_id,
                )
            )
    return MapData(
        gateways=gateways, nodes=nodes, without_location=without, tiles=await app_settings.get_map_config(db)
    )


@router.get("/settings/map", response_model=app_settings.MapConfig)
async def get_map_settings(ctx: AuthContext = Depends(require_auth), db: AsyncSession = Depends(get_db)):
    return await app_settings.get_map_config(db)


@router.put("/settings/map", response_model=app_settings.MapConfig)
async def put_map_settings(
    body: app_settings.MapConfig, ctx: AuthContext = Depends(require_auth), db: AsyncSession = Depends(get_db)
):
    await app_settings.put_map_config(db, body)
    db.add(AuditEvent(kind="map.settings_changed", detail={}))
    await db.commit()
    return body


class Place(BaseModel):
    # None when lookups are off (Settings → Map), the position is outside any named place, or the
    # lookup failed.
    name: str | None
    enabled: bool
    source: str = "OpenStreetMap Nominatim"


@router.get("/places", response_model=Place)
async def place(
    lat: float = Query(ge=-90, le=90),
    lon: float = Query(ge=-180, le=180),
    ctx: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """The place name for a node's advertised position, e.g. "Springfield, Illinois, US"."""
    if not (await app_settings.get_map_config(db)).place_names:
        return Place(name=None, enabled=False)
    await db.rollback()  # don't hold a database transaction open across the lookup
    return Place(name=await places.place_name(lat, lon), enabled=True)
