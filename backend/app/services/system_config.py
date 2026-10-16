"""Native installs: network/HTTPS settings applied by a root-owned helper.

The app never changes system configuration itself. It writes a request file into the state
directory; the meshhome-config path unit runs `install.sh --apply-config` as root, which
re-validates everything, applies it (nginx, certbot, the app's port), restores the previous
configuration on failure, and reports progress in a status file. The current configuration is
published as a non-secret snapshot (network.json) — secrets such as the Cloudflare token are
never written there.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from app.config import get_settings

REQUEST_FILE = "config-request.json"
STATUS_FILE = "config-status.json"
SNAPSHOT_FILE = "network.json"
TLS_FILE = "tls-status.json"
ACTIVE_STATES = {"queued", "applying", "installing", "certificate", "restarting"}
STALE_AFTER = 30 * 60


def _path(name: str) -> Path | None:
    d = get_settings().state_dir
    return Path(d) / name if d else None


def _read(name: str) -> dict[str, Any] | None:
    p = _path(name)
    if p is None or not p.is_file():
        return None
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


# Release layout: <release>/app and <release>/deploy; repository layout: <repo>/backend/app.
_HERE = Path(__file__).resolve()
_CATALOG_CANDIDATES = (
    _HERE.parents[2] / "deploy" / "native" / "dns-providers.json",
    _HERE.parents[3] / "deploy" / "native" / "dns-providers.json",
)


def dns_providers() -> list[dict[str, Any]]:
    """The DNS providers offered for HTTPS (deploy/native/dns-providers.json; static, no secrets)."""
    for path in _CATALOG_CANDIDATES:
        try:
            return json.loads(path.read_text())["providers"]
        except (OSError, ValueError, KeyError):
            continue
    return []


def dns_provider(provider_id: str) -> dict[str, Any] | None:
    return next((p for p in dns_providers() if p["id"] == provider_id), None)


def snapshot() -> dict[str, Any] | None:
    return _read(SNAPSHOT_FILE)


def certificate() -> dict[str, Any] | None:
    return _read(TLS_FILE)


def status() -> dict[str, Any] | None:
    return _read(STATUS_FILE)


def in_progress() -> bool:
    st = status()
    pending = _path(REQUEST_FILE)
    if pending is not None and pending.exists():
        return True
    return bool(
        st
        and st.get("state") in ACTIVE_STATES
        and time.time() - float(st.get("updated_at") or 0) < STALE_AFTER
    )


def write_request(payload: dict[str, Any]) -> None:
    """Atomically write a request readable only by the app user (root reads it, then deletes it)."""
    p = _path(REQUEST_FILE)
    if p is None:
        raise RuntimeError("no state directory configured")
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({**payload, "requested_at": time.time()}, f)
    os.replace(tmp, p)
