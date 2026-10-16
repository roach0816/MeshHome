"""MeshCore's suggested radio presets (frequency, bandwidth, spreading factor, coding rate).

The MeshCore project publishes them from its app API server so they can change without app
releases (docs/radio_presets.md in the MeshCore repository). MeshHome ships a snapshot
(app/data/radio_presets.json) and refreshes it from the server at most once a day; if the server
cannot be reached, the snapshot (or the last good copy) is used. Every key may be missing from
the server's answer, so entries are validated one by one and bad ones skipped.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import httpx

from app.config import APP_VERSION, get_settings

log = logging.getLogger(__name__)

BUNDLED = Path(__file__).resolve().parents[1] / "data" / "radio_presets.json"
REFRESH_SECONDS = 24 * 3600
RETRY_SECONDS = 3600
TIMEOUT = 8.0


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def parse(entries: Any) -> list[dict[str, Any]]:
    """Validated presets from the server's `suggested_radio_settings.entries`."""
    out: list[dict[str, Any]] = []
    for e in entries if isinstance(entries, list) else []:
        try:
            title = str(e["title"]).strip()
            freq = float(e["frequency"])
            bw = float(e["bandwidth"])
            sf = int(e["spreading_factor"])
            cr = int(e["coding_rate"])
        except (KeyError, TypeError, ValueError):
            continue
        if not title or not (150 <= freq <= 2500 and 5 <= sf <= 12 and 5 <= cr <= 8 and 0 < bw <= 500):
            continue
        preset: dict[str, Any] = {
            "id": _slug(title),
            "title": title,
            "freq_mhz": freq,
            "bw_khz": bw,
            "sf": sf,
            "cr": cr,
            "path_hash_size": None,
        }
        net = e.get("network_settings")
        if (
            isinstance(net, dict)
            and isinstance(net.get("path_hash_size"), int)
            and 1 <= net["path_hash_size"] <= 3
        ):
            preset["path_hash_size"] = net["path_hash_size"]
        out.append(preset)
    return out


class PresetCatalog:
    def __init__(self) -> None:
        self._presets: list[dict[str, Any]] = []
        self._info: str | None = None
        self._source = "bundled"
        self._updated: str | None = None
        self._next_fetch = 0.0
        self._load_bundled()

    def _load_bundled(self) -> None:
        try:
            data = json.loads(BUNDLED.read_text())
            self._presets = parse(data.get("entries"))
            self._info = data.get("info_message")
            self._updated = data.get("fetched_at")
        except (OSError, ValueError) as exc:
            log.warning("could not read the bundled radio presets: %s", exc)

    async def _refresh(self) -> None:
        url = get_settings().radio_presets_url
        if not url:
            self._next_fetch = float("inf")
            return
        try:
            async with httpx.AsyncClient(
                timeout=TIMEOUT,
                headers={"User-Agent": f"MeshHome/{APP_VERSION} (+https://github.com/roach0816/MeshHome)"},
            ) as client:
                r = await client.get(url)
                r.raise_for_status()
                settings = ((r.json().get("config") or {}).get("suggested_radio_settings")) or {}
            presets = parse(settings.get("entries"))
            if not presets:
                raise ValueError("no usable presets in the response")
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            log.info("radio presets: keeping the %s list (%s)", self._source, exc)
            self._next_fetch = time.time() + RETRY_SECONDS
            return
        self._presets, self._source = presets, "live"
        self._info = settings.get("info_message") or self._info
        self._updated = time.strftime("%Y-%m-%d")
        self._next_fetch = time.time() + REFRESH_SECONDS

    async def get(self) -> dict[str, Any]:
        if time.time() >= self._next_fetch:
            await self._refresh()
        return {
            "source": self._source,  # "live" (from MeshCore's server) or "bundled" (shipped snapshot)
            "updated": self._updated,
            "info_message": self._info,
            "presets": self._presets,
        }


catalog = PresetCatalog()
