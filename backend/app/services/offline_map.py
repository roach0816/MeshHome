"""Offline map: tiles saved on this server for an area, e.g. a race venue with no internet.

The owner picks an area (by default the courses on the tracking map, plus a margin) and zoom
levels; this server downloads those tiles once, while it has internet, and serves them to the
maps afterwards (Settings → Map → "Use the downloaded map"). Tiles live in the database, so they
work the same on a Raspberry Pi and in a container. They aren't included in backups: they can be
downloaded again.

Only sources that allow saving tiles are offered. The USGS National Map is public domain (United
States only). Another tile server can be used when its terms allow it. OpenStreetMap's own tile
servers are refused: their usage policy forbids downloading areas in bulk.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.config import APP_VERSION
from app.models import AuditEvent, MapTile, utcnow
from app.realtime import hub
from app.services import app_settings

log = logging.getLogger(__name__)

META_KEY = "offline_map"
MAX_TILES = 20_000
MAX_TILE_BYTES = 1_000_000
CONCURRENCY = 4
TIMEOUT = 15.0
ATTEMPTS = 3
BATCH = 50
# Stop when this many tiles in a row couldn't be fetched at all (no internet, server down).
GIVE_UP_AFTER = 25
PROGRESS_SECONDS = 1.0
MAX_LAT = 85.05112878  # Web Mercator
# Tile servers whose terms don't allow bulk downloads (OpenStreetMap's tile usage policy).
REFUSED_HOSTS = ("openstreetmap.org", "openstreetmap.fr", "osm.org")


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    url: str
    max_zoom: int
    attribution: str
    note: str


SOURCES = {
    s.id: s
    for s in (
        Source(
            "usgs-topo",
            "USGS Topo",
            "https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}",
            16,
            "USGS The National Map",
            "Topographic map with trails and contours. United States only; public domain.",
        ),
        Source(
            "usgs-imagery",
            "USGS Imagery Topo",
            "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryTopo/MapServer/tile/{z}/{y}/{x}",
            16,
            "USGS The National Map",
            "Aerial imagery with topographic labels. United States only; public domain.",
        ),
    )
}


class OfflineMapError(ValueError):
    """A download that can't start; the message says why."""


@dataclass
class Bounds:
    south: float
    west: float
    north: float
    east: float

    def valid(self) -> bool:
        return (
            -90 <= self.south < self.north <= 90
            and -180 <= self.west < self.east <= 180
            and all(map(math.isfinite, (self.south, self.west, self.north, self.east)))
        )

    def union(self, other: Bounds) -> Bounds:
        return Bounds(
            min(self.south, other.south),
            min(self.west, other.west),
            max(self.north, other.north),
            max(self.east, other.east),
        )


def tile_xy(lat: float, lon: float, z: int) -> tuple[int, int]:
    n = 1 << z
    lat = max(-MAX_LAT, min(MAX_LAT, lat))
    x = int((lon + 180.0) / 360.0 * n)
    r = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(r)) / math.pi) / 2.0 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def tile_ranges(b: Bounds, min_zoom: int, max_zoom: int) -> list[tuple[int, int, int, int, int]]:
    """(z, x0, x1, y0, y1) per zoom level, inclusive."""
    out = []
    for z in range(min_zoom, max_zoom + 1):
        x0, y0 = tile_xy(b.north, b.west, z)
        x1, y1 = tile_xy(b.south, b.east, z)
        out.append((z, x0, x1, y0, y1))
    return out


def count_tiles(b: Bounds, min_zoom: int, max_zoom: int) -> int:
    return sum((x1 - x0 + 1) * (y1 - y0 + 1) for _, x0, x1, y0, y1 in tile_ranges(b, min_zoom, max_zoom))


def check_custom_url(url: str) -> str:
    url = url.strip()
    parts = urlsplit(url.replace("{", "").replace("}", ""))
    if parts.scheme != "https" or not parts.hostname:
        raise OfflineMapError("The tile URL must start with https://")
    if not all(p in url for p in ("{z}", "{x}", "{y}")):
        raise OfflineMapError("The tile URL must contain {z}, {x} and {y}")
    host = parts.hostname.lower()
    if any(host == h or host.endswith("." + h) for h in REFUSED_HOSTS):
        raise OfflineMapError(
            "OpenStreetMap's tile servers don't allow downloading areas for offline use. "
            "Choose USGS (United States) or a tile server whose terms allow it."
        )
    return url


@dataclass
class Job:
    state: str = "idle"  # idle | running | done | cancelled | error
    source: str | None = None
    total: int = 0
    done: int = 0  # saved (or already saved)
    missing: int = 0  # the server has no tile there (outside its coverage)
    failed: int = 0  # couldn't be fetched
    error: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    skipped: int = 0  # already saved from an earlier download
    extra: dict[str, Any] = field(default_factory=dict, repr=False)


class OfflineMap:
    def __init__(self) -> None:
        self.job = Job()
        self._task: asyncio.Task | None = None
        self._published = 0.0

    def status(self) -> dict[str, Any]:
        d = asdict(self.job)
        d.pop("extra", None)
        return d

    async def summary(self) -> dict[str, Any]:
        async with db.session_factory()() as s:
            meta = await app_settings.get_raw(s, META_KEY) or {}
            count, size, zmin, zmax = (
                await s.execute(
                    select(
                        func.count(),
                        func.coalesce(func.sum(func.length(MapTile.data)), 0),
                        func.min(MapTile.z),
                        func.max(MapTile.z),
                    )
                )
            ).one()
        return {
            "tiles": int(count),
            "bytes": int(size),
            "min_zoom": zmin,
            "max_zoom": zmax,
            "source": meta.get("source") if count else None,
            "source_name": meta.get("source_name") if count else None,
            "attribution": meta.get("attribution") if count else None,
            "bounds": meta.get("bounds") if count else None,
            "downloaded_at": meta.get("downloaded_at") if count else None,
        }

    def _publish(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._published >= PROGRESS_SECONDS:
            self._published = now
            hub.publish("offline-map-updated")

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(
        self,
        *,
        source: str,
        bounds: Bounds,
        min_zoom: int,
        max_zoom: int,
        custom_url: str | None = None,
        attribution: str | None = None,
    ) -> dict[str, Any]:
        if self.running:
            raise OfflineMapError("A download is already running.")
        if not bounds.valid():
            raise OfflineMapError(
                "Choose an area with its north above its south and its east beyond its west."
            )
        if source == "custom":
            url = check_custom_url(custom_url or "")
            name = urlsplit(url.replace("{", "").replace("}", "")).hostname or "Tile server"
            attrib, top = (attribution or "").strip()[:200], 19
        elif source in SOURCES:
            src = SOURCES[source]
            url, name, attrib, top = src.url, src.name, src.attribution, src.max_zoom
        else:
            raise OfflineMapError("Unknown map source.")
        if not (0 <= min_zoom <= max_zoom <= top):
            raise OfflineMapError(f"Choose zoom levels from 0 to {top}, the smallest first.")
        total = count_tiles(bounds, min_zoom, max_zoom)
        if total > MAX_TILES:
            raise OfflineMapError(
                f"That's {total:,} tiles; the limit is {MAX_TILES:,}. Choose a smaller area or a lower top zoom."
            )
        self.job = Job(state="running", source=source, total=total, started_at=time.time())
        self._task = asyncio.create_task(
            self._run(url, source, name, attrib, bounds, min_zoom, max_zoom), name="offline-map"
        )
        self._publish(force=True)
        return self.status()

    def cancel(self) -> None:
        if self.running and self._task is not None:
            self._task.cancel()

    async def stop(self) -> None:
        self.cancel()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (asyncio.CancelledError, TimeoutError):
                pass

    async def clear(self) -> None:
        if self.running:
            raise OfflineMapError("Cancel the download first.")
        async with db.session_factory()() as s:
            await s.execute(delete(MapTile))
            await app_settings.put_raw(s, META_KEY, {})
            cfg = await app_settings.get_map_config(s)
            if cfg.offline:
                await app_settings.put_map_config(s, cfg.model_copy(update={"offline": False}))
            s.add(AuditEvent(kind="map.offline_cleared", detail={}))
            await s.commit()
        self.job = Job()
        self._publish(force=True)

    @staticmethod
    async def tile(s: AsyncSession, z: int, x: int, y: int) -> tuple[bytes, str] | None:
        """One saved tile, read on the caller's session: a map asks for many at once, and a second
        connection per request would exhaust the pool."""
        row = (
            await s.execute(
                select(MapTile.data, MapTile.content_type).where(
                    MapTile.z == z, MapTile.x == x, MapTile.y == y
                )
            )
        ).first()
        return (bytes(row[0]), row[1]) if row else None

    # ---- the download ----------------------------------------------------------------

    async def _run(
        self, url: str, source: str, name: str, attrib: str, bounds: Bounds, min_zoom: int, max_zoom: int
    ) -> None:
        job = self.job
        try:
            async with db.session_factory()() as s:
                meta = await app_settings.get_raw(s, META_KEY) or {}
                if meta.get("source") != source or meta.get("url") != url:
                    # One map at a time: tiles of another map would mix with these.
                    await s.execute(delete(MapTile))
                    meta = {}
                await s.commit()
            todo = await self._missing_tiles(bounds, min_zoom, max_zoom)
            job.skipped = job.total - len(todo)
            job.done = job.skipped
            self._publish()
            await self._fetch_all(url, todo)
            async with db.session_factory()() as s:
                old = meta.get("bounds")
                area = bounds.union(Bounds(**old)) if old else bounds
                await app_settings.put_raw(
                    s,
                    META_KEY,
                    {
                        "source": source,
                        "source_name": name,
                        "url": url,
                        "attribution": attrib,
                        "bounds": asdict(area),
                        "downloaded_at": utcnow().isoformat(),
                    },
                )
                if job.done:
                    cfg = await app_settings.get_map_config(s)
                    if not cfg.offline:
                        await app_settings.put_map_config(s, cfg.model_copy(update={"offline": True}))
                s.add(
                    AuditEvent(
                        kind="map.offline_downloaded",
                        detail={
                            "source": source,
                            "tiles": job.done,
                            "failed": job.failed,
                            "missing": job.missing,
                        },
                    )
                )
                await s.commit()
            if job.state == "running":
                job.state = "done"
        except asyncio.CancelledError:
            job.state = "cancelled"
        except Exception as exc:  # noqa: BLE001 - report it on the page
            log.warning("offline map download failed: %s", exc)
            job.state, job.error = "error", str(exc) or type(exc).__name__
        finally:
            job.finished_at = time.time()
            self._publish(force=True)

    async def _missing_tiles(
        self, bounds: Bounds, min_zoom: int, max_zoom: int
    ) -> list[tuple[int, int, int]]:
        out: list[tuple[int, int, int]] = []
        async with db.session_factory()() as s:
            for z, x0, x1, y0, y1 in tile_ranges(bounds, min_zoom, max_zoom):
                rows = await s.execute(
                    select(MapTile.x, MapTile.y).where(
                        MapTile.z == z, MapTile.x.between(x0, x1), MapTile.y.between(y0, y1)
                    )
                )
                have = {(r[0], r[1]) for r in rows}
                out.extend(
                    (z, x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1) if (x, y) not in have
                )
        return out

    async def _fetch_all(self, url: str, todo: list[tuple[int, int, int]]) -> None:
        job = self.job
        queue: asyncio.Queue[tuple[int, int, int]] = asyncio.Queue()
        for t in todo:
            queue.put_nowait(t)
        batch: list[dict[str, Any]] = []
        lock = asyncio.Lock()
        unreachable = 0
        last_error = ""

        async def flush() -> None:
            nonlocal batch
            if not batch:
                return
            rows, batch = batch, []
            async with db.session_factory()() as s:
                stmt = insert(MapTile).values(rows)
                stmt = stmt.on_conflict_do_update(
                    index_elements=[MapTile.z, MapTile.x, MapTile.y],
                    set_={"data": stmt.excluded.data, "content_type": stmt.excluded.content_type},
                )
                await s.execute(stmt)
                await s.commit()

        async def worker(client: httpx.AsyncClient) -> None:
            nonlocal unreachable, last_error
            while True:
                try:
                    z, x, y = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                result = await self._fetch(client, url, z, x, y)
                async with lock:
                    if isinstance(result, tuple):
                        unreachable = 0
                        batch.append({"z": z, "x": x, "y": y, "content_type": result[1], "data": result[0]})
                        job.done += 1
                        if len(batch) >= BATCH:
                            await flush()
                    elif result == "missing":
                        unreachable = 0
                        job.missing += 1
                    else:
                        unreachable += 1
                        last_error = result
                        job.failed += 1
                        if unreachable >= GIVE_UP_AFTER:
                            raise OfflineMapError(f"Couldn't reach the tile server ({last_error}).")
                    self._publish()

        headers = {"User-Agent": f"MeshHome/{APP_VERSION} (+https://github.com/roach0816/MeshHome)"}
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=headers, follow_redirects=True) as client:
            workers = [asyncio.create_task(worker(client)) for _ in range(CONCURRENCY)]
            try:
                await asyncio.gather(*workers)
            except BaseException:
                for w in workers:
                    w.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
                async with lock:
                    await flush()  # keep what was fetched
                raise
            await flush()

    async def _fetch(
        self, client: httpx.AsyncClient, url: str, z: int, x: int, y: int
    ) -> tuple[bytes, str] | str:
        """The tile and its type, "missing" when the server has none there, or why it failed."""
        target = url.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))
        why = "no answer"
        for attempt in range(ATTEMPTS):
            if attempt:
                await asyncio.sleep(attempt * 1.5)
            try:
                r = await client.get(target)
            except httpx.HTTPError as exc:
                why = type(exc).__name__
                continue
            if r.status_code in (204, 404):
                return "missing"
            if r.status_code == 200:
                ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                if not ctype.startswith("image/") or not r.content:
                    return "missing"  # e.g. an HTML "no tile here" page
                if len(r.content) > MAX_TILE_BYTES:
                    return "tile too large"
                return r.content, ctype[:32]
            why = f"HTTP {r.status_code}"
            if r.status_code not in (408, 429) and r.status_code < 500:
                return why
        return why


manager = OfflineMap()
