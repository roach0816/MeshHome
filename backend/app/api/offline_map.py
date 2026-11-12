"""Offline map (services/offline_map.py): download tiles for an area, and serve them."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, require_auth, require_session
from app.db import get_db
from app.services import offline_map
from app.services.offline_map import Bounds, OfflineMapError

router = APIRouter(prefix="/api", tags=["map"])


class SourceOut(BaseModel):
    id: str
    name: str
    max_zoom: int
    attribution: str
    note: str


class BoundsIn(BaseModel):
    south: float = Field(ge=-90, le=90)
    west: float = Field(ge=-180, le=180)
    north: float = Field(ge=-90, le=90)
    east: float = Field(ge=-180, le=180)


class AreaIn(BaseModel):
    bounds: BoundsIn
    min_zoom: int = Field(default=10, ge=0, le=19)
    max_zoom: int = Field(default=16, ge=0, le=19)


class DownloadIn(AreaIn):
    source: Literal["usgs-topo", "usgs-imagery", "custom"] = "usgs-topo"
    # source "custom": an https:// XYZ template, and confirmation that its terms allow saving tiles.
    url: str | None = Field(default=None, max_length=500)
    attribution: str | None = Field(default=None, max_length=200)
    permitted: bool = False


class OfflineMapOut(BaseModel):
    sources: list[SourceOut]
    max_tiles: int
    # What's saved: tiles, bytes, min_zoom, max_zoom, source, source_name, attribution, bounds
    # ({south, west, north, east}), downloaded_at. tiles is 0 when nothing is saved.
    saved: dict
    # The current or last download: state (idle | running | done | cancelled | error), total, done,
    # skipped (already saved), missing (none at the source), failed, error.
    job: dict


@router.get("/offline-map", response_model=OfflineMapOut)
async def get_offline_map(ctx: AuthContext = Depends(require_auth)):
    return OfflineMapOut(
        sources=[
            SourceOut(**{k: getattr(s, k) for k in SourceOut.model_fields})
            for s in offline_map.SOURCES.values()
        ],
        max_tiles=offline_map.MAX_TILES,
        saved=await offline_map.manager.summary(),
        job=offline_map.manager.status(),
    )


@router.post("/offline-map/estimate")
async def estimate(body: AreaIn, ctx: AuthContext = Depends(require_auth)):
    """How many tiles an area and zoom range is (each is roughly 10–30 kB)."""
    b = Bounds(**body.bounds.model_dump())
    if not b.valid() or body.min_zoom > body.max_zoom:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Choose a valid area and zoom range")
    return {"tiles": offline_map.count_tiles(b, body.min_zoom, body.max_zoom)}


@router.post("/offline-map/download", status_code=202)
async def download(body: DownloadIn, ctx: AuthContext = Depends(require_session)):
    """Start downloading. Progress arrives as offline-map-updated events (GET /api/offline-map)."""
    if body.source == "custom" and not body.permitted:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Confirm that this tile server's terms allow saving its tiles for offline use",
        )
    try:
        return offline_map.manager.start(
            source=body.source,
            bounds=Bounds(**body.bounds.model_dump()),
            min_zoom=body.min_zoom,
            max_zoom=body.max_zoom,
            custom_url=body.url,
            attribution=body.attribution,
        )
    except OfflineMapError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None


@router.post("/offline-map/cancel", status_code=204)
async def cancel(ctx: AuthContext = Depends(require_session)):
    offline_map.manager.cancel()
    return Response(status_code=204)


@router.delete("/offline-map", status_code=204)
async def clear(ctx: AuthContext = Depends(require_session)):
    """Delete the saved tiles (the maps go back to the tile server)."""
    try:
        await offline_map.manager.clear()
    except OfflineMapError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    return Response(status_code=204)


@router.get("/tiles/{z}/{x}/{y}", include_in_schema=False)
async def tile(
    z: int = Path(ge=0, le=22),
    x: int = Path(ge=0),
    y: int = Path(ge=0),
    ctx: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    found = await offline_map.manager.tile(db, z, x, y)
    if found is None:
        return Response(status_code=404, headers={"Cache-Control": "no-store"})
    data, ctype = found
    # Tiles don't change once saved; let the browser keep them (the API default is no-store).
    return Response(data, media_type=ctype, headers={"Cache-Control": "private, max-age=86400"})
