"""Health snapshot for `meshhome status` on native installs.

Every 30 seconds the app writes <state dir>/diagnostics.json with its own view of things that
only it knows: the live radio connection and its last error, collection gaps, the database as
the app sees it, and update checks. The root-run status command reads the file, so diagnostics
need no network endpoint and no credentials. The file is readable only by root and the app, and
it contains no secrets.
"""

import asyncio
import json
import logging
import os
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from app import db
from app.config import APP_VERSION, get_settings
from app.models import ApiKey, CollectionGap, Contact, Conversation, Message, User, utcnow
from app.radio.supervisor import supervisor
from app.realtime import hub
from app.services import app_settings, updates

log = logging.getLogger(__name__)

INTERVAL = 30
FILENAME = "diagnostics.json"
STARTED_AT = time.time()


async def snapshot() -> dict[str, Any]:
    database: dict[str, Any] = {"ok": False}
    setup_complete = None
    radio_config: dict[str, Any] = {}
    api_keys = None
    try:
        async with db.session_factory()() as s:
            database["messages"] = (await s.execute(select(func.count()).select_from(Message))).scalar_one()
            database["conversations"] = (
                await s.execute(select(func.count()).select_from(Conversation))
            ).scalar_one()
            database["contacts"] = (await s.execute(select(func.count()).select_from(Contact))).scalar_one()
            open_gap = (
                await s.execute(
                    select(CollectionGap)
                    .where(CollectionGap.ended_at.is_(None))
                    .order_by(CollectionGap.started_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            database["open_gap"] = (
                {"started_at": open_gap.started_at.timestamp(), "reason": open_gap.reason}
                if open_gap
                else None
            )
            database["gaps_24h"] = (
                await s.execute(
                    select(func.count())
                    .select_from(CollectionGap)
                    .where(CollectionGap.started_at > utcnow() - timedelta(hours=24))
                )
            ).scalar_one()
            setup_complete = (await s.execute(select(User.id).limit(1))).first() is not None
            cfg = await app_settings.get_radio_config(s)
            radio_config = {"mode": cfg.mode, "host": cfg.host, "port": cfg.port, "paused": cfg.paused}
            api_keys = (await s.execute(select(func.count()).select_from(ApiKey))).scalar_one()
        database["ok"] = True
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        database["error"] = f"{type(exc).__name__}: {exc}"[:300]

    u = updates.checker.summary()
    return {
        "written_at": time.time(),
        "version": APP_VERSION,
        "pid": os.getpid(),
        "started_at": STARTED_AT,
        "setup_complete": setup_complete,
        "radio": supervisor.snapshot(),
        "radio_config": radio_config,
        "database": database,
        "realtime_clients": hub.client_count,
        "api_keys": api_keys,
        "updates": {
            "checks_enabled": u["checks_enabled"],
            "checked_at": u["checked_at"],
            "error": u["error"],
            "latest_version": (u["latest"] or {}).get("version"),
            "update_available": u["update_available"],
        },
    }


def write(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, default=str)
    os.chmod(tmp, 0o640)
    os.replace(tmp, path)


async def loop() -> None:
    state_dir = get_settings().state_dir
    if not state_dir or not os.path.isdir(state_dir):
        return  # container installs have no state directory and no status command
    path = Path(state_dir) / FILENAME
    while True:
        try:
            write(path, await snapshot())
        except Exception:  # noqa: BLE001
            log.debug("could not write %s", path, exc_info=True)
        await asyncio.sleep(INTERVAL)
