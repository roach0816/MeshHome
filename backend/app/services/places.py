"""Place names for advertised positions ("Springfield, Illinois, US"), from OpenStreetMap Nominatim.

Only coordinates leave this server: those a node chose to include in its public advert. Lookups
happen when someone opens a node's details, never in bulk. Nominatim's usage policy asks for at most
one request a second, an identifying User-Agent and caching; all three are done here. The owner can
turn lookups off (Settings → Map → Place names).
"""

import asyncio
import logging
import time
from collections import OrderedDict

import httpx

from app.config import APP_VERSION

log = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
TIMEOUT = 8.0
MIN_INTERVAL = 1.1  # seconds between requests (Nominatim allows one a second)
CACHE_SIZE = 2000
FAILURE_TTL = 300  # retry a failed lookup after five minutes
# The most specific settlement Nominatim reports, then wider areas when there is none.
LOCALITY_KEYS = ("city", "town", "village", "hamlet", "suburb", "municipality", "county")

_cache: OrderedDict[tuple[float, float], tuple[str | None, float]] = OrderedDict()
_lock = asyncio.Lock()
_last_request = 0.0


def _key(lat: float, lon: float) -> tuple[float, float]:
    return round(lat, 3), round(lon, 3)  # about 100 m: nearby positions share one lookup


def label(address: dict) -> str | None:
    """ "Town, State, CC" from a Nominatim address, leaving out parts it doesn't have."""
    locality = next((address[k] for k in LOCALITY_KEYS if address.get(k)), None)
    region = address.get("state") or address.get("region") or address.get("province")
    country = (address.get("country_code") or "").upper() or address.get("country")
    parts = [p for p in (locality, region if region != locality else None, country) if p]
    return ", ".join(parts) or None


async def _fetch(lat: float, lon: float) -> str | None:
    global _last_request
    wait = _last_request + MIN_INTERVAL - time.monotonic()
    if wait > 0:
        await asyncio.sleep(wait)
    _last_request = time.monotonic()
    async with httpx.AsyncClient(
        timeout=TIMEOUT,
        headers={"User-Agent": f"MeshHome/{APP_VERSION} (+https://github.com/roach0816/MeshHome)"},
    ) as client:
        r = await client.get(
            NOMINATIM_URL,
            params={
                "format": "jsonv2",
                "lat": f"{lat:.5f}",
                "lon": f"{lon:.5f}",
                "zoom": 10,
                "addressdetails": 1,
            },
        )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return label(r.json().get("address") or {})


async def place_name(lat: float, lon: float) -> str | None:
    """The place at these coordinates, or None (open sea, or the lookup failed)."""
    key = _key(lat, lon)
    async with _lock:  # one request at a time keeps to the rate limit and lets waiters share a result
        hit = _cache.get(key)
        if hit is not None:
            name, at = hit
            if name is not None or time.monotonic() - at < FAILURE_TTL:
                _cache.move_to_end(key)
                return name
        try:
            name = await _fetch(*key)
        except (httpx.HTTPError, ValueError) as exc:
            log.info("place name lookup failed: %s", type(exc).__name__)
            name = None
        _cache[key] = (name, time.monotonic())
        _cache.move_to_end(key)
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)
        return name
