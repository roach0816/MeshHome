"""Release update checks (GitHub Releases) and, on native installs, in-place upgrade requests.

The web app never upgrades itself. On a native install it writes an *update request* file into
the state directory; a root-owned systemd unit (meshhome-update.path/.service, installed by
deploy/native/install.sh) notices it, re-validates the version against the official releases,
verifies the download checksum, backs up the database, installs, and restarts the app — rolling
back if the new version doesn't come up. Progress is reported through a status file this module
reads back.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app.config import APP_VERSION, get_settings

log = logging.getLogger(__name__)

CHECK_INTERVAL = 6 * 3600
FAILURE_RETRY = 15 * 60
SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
REQUEST_FILE = "update-request.json"
STATUS_FILE = "update-status.json"


def parse_version(v: str) -> tuple[int, int, int] | None:
    m = SEMVER.match(v.strip())
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def is_newer(candidate: str, current: str = APP_VERSION) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    return bool(a and b and a > b)


@dataclass
class ReleaseInfo:
    version: str
    url: str
    notes: str
    published_at: str | None
    has_native_package: bool


class UpdateChecker:
    def __init__(self) -> None:
        self._latest: ReleaseInfo | None = None
        # Release notes for the installed version (shown even when it is up to date).
        self._installed: ReleaseInfo | None = None
        self._checked_at: float = 0
        self._error: str | None = None
        self._lock = asyncio.Lock()

    async def _fetch(self, which: str = "latest") -> ReleaseInfo:
        """which: "latest", or "tags/vX.Y.Z" for a specific release."""
        s = get_settings()
        url = f"{s.update_api_url.rstrip('/')}/repos/{s.update_repo}/releases/{which}"
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=10,
            headers={"Accept": "application/vnd.github+json", "User-Agent": f"MeshHome/{APP_VERSION}"},
        ) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json()
        tag = str(data.get("tag_name") or "")
        if not parse_version(tag):
            raise ValueError(f"unexpected release tag {tag!r}")
        version = tag.lstrip("v")
        assets = {a.get("name") for a in data.get("assets") or [] if isinstance(a, dict)}
        return ReleaseInfo(
            version=version,
            url=str(data.get("html_url") or ""),
            notes=str(data.get("body") or "")[:20000],
            published_at=data.get("published_at"),
            has_native_package=bool(
                {f"meshhome-{version}.tar.gz", f"meshcore-home-{version}.tar.gz"} & assets
            )
            and "SHA256SUMS" in assets,
        )

    async def check(self, force: bool = False) -> None:
        s = get_settings()
        if not s.update_repo:
            return
        async with self._lock:
            age = time.time() - self._checked_at
            if not force and age < (FAILURE_RETRY if self._error else CHECK_INTERVAL):
                return
            try:
                self._latest = await self._fetch()
                self._error = None
            except Exception as exc:  # noqa: BLE001 - offline Pi, rate limit, DNS block...
                self._error = f"{type(exc).__name__}: {str(exc)[:160]}"
                log.info("update check failed: %s", self._error)
            await self._load_installed()
            self._checked_at = time.time()

    async def _load_installed(self) -> None:
        """Notes for the running version: reuse the latest release if it matches, else fetch once."""
        if self._installed is not None and self._installed.version == APP_VERSION:
            return
        if self._latest is not None and self._latest.version == APP_VERSION:
            self._installed = self._latest
            return
        try:
            self._installed = await self._fetch(f"tags/v{APP_VERSION}")
        except Exception as exc:  # noqa: BLE001 - e.g. a development build with no release
            log.debug("no release notes for v%s: %s", APP_VERSION, exc)

    def summary(self) -> dict[str, Any]:
        s = get_settings()
        latest = self._latest
        available = bool(latest and is_newer(latest.version))
        return {
            "current_version": APP_VERSION,
            "install_kind": s.install_kind,
            "checks_enabled": bool(s.update_repo),
            "checked_at": self._checked_at or None,
            "error": self._error,
            "latest": None
            if latest is None
            else {
                "version": latest.version,
                "url": latest.url,
                "notes": latest.notes,
                "published_at": latest.published_at,
                "has_native_package": latest.has_native_package,
            },
            "installed": None
            if self._installed is None
            else {
                "version": self._installed.version,
                "url": self._installed.url,
                "notes": self._installed.notes,
                "published_at": self._installed.published_at,
            },
            "update_available": available,
            # The native command (`sudo meshhome …`); installs from before the rename keep the old name
            # until `sudo meshcore-home migrate`.
            "command": "meshcore-home" if legacy_layout() else "meshhome",
            "legacy_layout": legacy_layout(),
            "can_install": available
            and s.install_kind == "native"
            and bool(s.state_dir)
            and latest.has_native_package,
        }


checker = UpdateChecker()


def legacy_layout() -> bool:
    """A native install still using the names from before the rename (/var/lib/meshcore-home, …)."""
    s = get_settings()
    return s.install_kind == "native" and s.state_dir.rstrip("/").endswith("meshcore-home")


# ---- native upgrade request / status files ------------------------------------------------


def _state_path(name: str) -> Path | None:
    d = get_settings().state_dir
    return Path(d) / name if d else None


def read_status() -> dict[str, Any] | None:
    p = _state_path(STATUS_FILE)
    if p is None or not p.is_file():
        return None
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def write_request(version: str, requested_by: str) -> None:
    p = _state_path(REQUEST_FILE)
    if p is None:
        raise RuntimeError("no state directory configured")
    tmp = p.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"version": version, "requested_by": requested_by, "requested_at": time.time()})
    )
    os.replace(tmp, p)  # atomic: the path unit sees one complete file
