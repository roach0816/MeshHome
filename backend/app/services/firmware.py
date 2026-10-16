"""Radio firmware: is a newer MeshCore companion release available for the connected radio?

MeshCore publishes companion firmware as GitHub releases tagged "companion-vX.Y.Z". The radio
reports its version ("v1.17.1") in its device info, so the two are compared to show whether the
radio is up to date. Nothing here changes the radio.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

from app.config import APP_VERSION, get_settings

log = logging.getLogger(__name__)

REFRESH_SECONDS = 6 * 3600  # GitHub allows 60 unauthenticated requests an hour
RETRY_SECONDS = 3600
TIMEOUT = 10.0
TAG = re.compile(r"^companion-v(\d+)\.(\d+)\.(\d+)$")
VERSION = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")


def version_tuple(text: str | None) -> tuple[int, int, int] | None:
    m = VERSION.search(text or "")
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def _release(raw: dict[str, Any]) -> dict[str, Any] | None:
    m = TAG.match(str(raw.get("tag_name") or ""))
    if not m or raw.get("draft") or raw.get("prerelease"):
        return None
    return {
        "version": f"{m[1]}.{m[2]}.{m[3]}",
        "published_at": raw.get("published_at"),
        "notes_url": raw.get("html_url"),
    }


class ReleaseCatalog:
    def __init__(self) -> None:
        self._latest: dict[str, Any] | None = None
        self._checked_at: float | None = None
        self._next_fetch = 0.0
        self._error: str | None = None

    def _url(self) -> str:
        s = get_settings()
        return f"{s.update_api_url.rstrip('/')}/repos/{s.firmware_repo}/releases?per_page=30"

    async def refresh(self) -> None:
        try:
            async with httpx.AsyncClient(
                timeout=TIMEOUT,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": f"MeshHome/{APP_VERSION} (+https://github.com/roach0816/MeshHome)",
                },
            ) as client:
                r = await client.get(self._url())
                r.raise_for_status()
                releases = [x for x in (_release(raw) for raw in r.json() if isinstance(raw, dict)) if x]
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            log.info("firmware releases unavailable: %s", type(exc).__name__)
            self._error = "Could not reach GitHub to check for MeshCore firmware releases."
            self._next_fetch = time.time() + RETRY_SECONDS
            return
        releases.sort(key=lambda x: version_tuple(x["version"]) or (0, 0, 0), reverse=True)
        self._latest = releases[0] if releases else None
        self._checked_at = time.time()
        self._error = None if releases else "No companion firmware releases were found."
        self._next_fetch = time.time() + REFRESH_SECONDS

    async def latest(self, force: bool = False) -> dict[str, Any] | None:
        if force or time.time() >= self._next_fetch:
            await self.refresh()
        return self._latest

    @property
    def checked_at(self) -> float | None:
        return self._checked_at

    @property
    def error(self) -> str | None:
        return self._error


catalog = ReleaseCatalog()


def status(*, model: str | None, version: str | None, latest: dict[str, Any] | None) -> dict[str, Any]:
    current, newest = version_tuple(version), version_tuple((latest or {}).get("version"))
    return {
        "model": model,
        "current_version": ".".join(map(str, current)) if current else None,
        "latest": latest,
        # None: unknown (no version from the radio, or GitHub could not be reached)
        "up_to_date": (newest <= current) if (current and newest) else None,
    }
