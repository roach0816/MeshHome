"""MeshHome's own log, for Settings → Health & logs.

A logging handler sees every record from INFO up and keeps:
- **recent activity**: the last RECENT_MAX lines since MeshHome started, in memory;
- **history**: warnings, errors and events (the radio connecting and going offline, the app
  starting, settings changed), in the database (LogEntry) for RETAIN_DAYS. The same line repeated
  within GROUP_SECONDS is one entry with a count, so an outage that retries all night is one line.

Records are written by a background task in small batches. While the database is unavailable
they wait in memory (up to PENDING_MAX) and are written once it is back, so the history covers
database outages too. Each line is put under a component (radio, database, tracking, ...) from
the logger's name, or from `extra={"component": ...}`. A record with `extra={"private": True}` (the
first-run setup token) is never kept.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, or_, select, update

from app import db
from app.models import LogEntry, utcnow
from app.realtime import hub

log = logging.getLogger(__name__)

RECENT_MAX = 2000
PENDING_MAX = 5000
FLUSH_DELAY = 1.0  # seconds: gather a burst of lines into one write
RETRY_SECONDS = 10.0  # after a failed write (the database unavailable)
GROUP_SECONDS = 3600  # the same line again within this long adds to its entry
RETAIN_DAYS = 30
RETAIN_ROWS = 20000
PRUNE_SECONDS = 3600
MESSAGE_MAX = 2000
DETAIL_MAX = 8000

# Components, as shown in the log view, in this order.
COMPONENTS: dict[str, str] = {
    "radio": "Radio",
    "radio_hat": "Radio HAT",
    "tracking": "Tracking",
    "database": "Database",
    "notifications": "Notifications",
    "bot": "Bot",
    "sensors": "Sensors",
    "maps": "Maps",
    "updates": "Updates",
    "network": "Network & HTTPS",
    "backup": "Backup",
    "web": "Web & API",
    "app": "MeshHome",
}

# Logger name prefixes → component (the longest matching prefix wins).
_PREFIXES: dict[str, str] = {
    "app.radio": "radio",
    "app.meshtastic": "radio",
    "app.services.messaging": "radio",
    "app.services.msg_paths": "radio",
    "app.services.preferred_route": "radio",
    "app.services.remote_admin": "radio",
    "app.services.firmware": "radio",
    "app.services.radio_backup": "radio",
    "app.services.radio_presets": "radio",
    "app.services.signed_messages": "radio",
    "app.services.radio_hat": "radio_hat",
    "app.services.tracking": "tracking",
    "app.services.meshtastic": "tracking",
    "app.services.courses": "tracking",
    "app.db": "database",
    "sqlalchemy": "database",
    "asyncpg": "database",
    "alembic": "database",
    "app.services.web_push": "notifications",
    "app.services.push_inbox": "notifications",
    "app.services.bot": "bot",
    "app.services.bot_apps": "bot",
    "app.services.bot_recipes": "bot",
    "app.services.sensors": "sensors",
    "app.services.offline_map": "maps",
    "app.services.places": "maps",
    "app.services.updates": "updates",
    "app.services.system_config": "network",
    "app.services.backup": "backup",
    "app.services.backup_crypto": "backup",
    "app.api": "web",
    "app.realtime": "web",
    "uvicorn": "web",
}

LEVELS = ("INFO", "WARNING", "ERROR", "CRITICAL")
PROBLEM_LEVELS = ("WARNING", "ERROR", "CRITICAL")


def component_for(logger_name: str) -> str:
    best, comp = -1, "app"
    for prefix, c in _PREFIXES.items():
        if (logger_name == prefix or logger_name.startswith(prefix + ".")) and len(prefix) > best:
            best, comp = len(prefix), c
    return comp


def _level_name(levelno: int) -> str:
    if levelno >= logging.CRITICAL:
        return "CRITICAL"
    if levelno >= logging.ERROR:
        return "ERROR"
    if levelno >= logging.WARNING:
        return "WARNING"
    return "INFO"


class _State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.recent: deque[dict[str, Any]] = deque(maxlen=RECENT_MAX)
        self.pending: deque[dict[str, Any]] = deque(maxlen=PENDING_MAX)
        self.seq = 0
        self.dropped = 0  # history lines lost while the database was unavailable too long
        # (level, component, event, logger, message) -> (entry id, last_at epoch)
        self.groups: dict[tuple, tuple[int, float]] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        self.wake: asyncio.Event | None = None
        self.task: asyncio.Task | None = None
        self.started_at = time.time()
        self.last_write_error: str | None = None


_state = _State()


def _capture(record: logging.LogRecord) -> None:
    if record.levelno < logging.INFO or getattr(record, "private", False):
        return
    if record.name == __name__ or record.name.startswith("uvicorn.access"):
        return  # this module's own trouble (it would feed itself), and every web request
    try:
        message = record.getMessage()
    except Exception:  # noqa: BLE001 - a malformed log call must not break logging
        message = str(record.msg)
    detail = None
    if record.exc_info and record.exc_info[0] is not None:
        detail = logging.Formatter().formatException(record.exc_info)[-DETAIL_MAX:]
    event = getattr(record, "event", None)
    entry = {
        "at": record.created,
        "level": _level_name(record.levelno),
        "component": getattr(record, "component", None) or component_for(record.name),
        "event": event,
        "logger": record.name[:80],
        "message": message[:MESSAGE_MAX],
        "detail": detail,
    }
    st = _state
    with st.lock:
        st.seq += 1
        entry["seq"] = st.seq
        st.recent.append(entry)
        if entry["level"] != "INFO" or event:
            if len(st.pending) == PENDING_MAX:
                st.dropped += 1
            st.pending.append(entry)
            loop, wake = st.loop, st.wake
        else:
            loop = wake = None
    if loop is not None and wake is not None:
        try:
            loop.call_soon_threadsafe(wake.set)
        except RuntimeError:  # the loop is closed (shutting down)
            pass


class CaptureHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            _capture(record)
        except Exception:  # noqa: BLE001 - logging must never raise
            pass


_handler = CaptureHandler(level=logging.INFO)


def install() -> None:
    """Attach the handler (idempotent). Uvicorn's own loggers don't reach the root logger."""
    for name in ("", "uvicorn"):
        lg = logging.getLogger(name)
        if _handler not in lg.handlers:
            lg.addHandler(_handler)


def event(
    logger: logging.Logger, message: str, *, component: str, kind: str, level: int = logging.INFO
) -> None:
    """Log an event for the history: kept even when LOG_LEVEL hides INFO from the journal."""
    extra = {"component": component, "event": kind}
    if logger.isEnabledFor(level):
        logger.log(level, "%s", message, extra=extra)
        return
    record = logger.makeRecord(logger.name, level, "", 0, "%s", (message,), None, extra=extra)
    _capture(record)


# ---- writing the history ---------------------------------------------------------------------


def _key(e: dict[str, Any]) -> tuple:
    return (e["level"], e["component"], e["event"], e["logger"], e["message"])


def _dt(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, UTC)


async def flush() -> bool:
    """Write waiting history lines. False if the database is unavailable (they keep waiting)."""
    st = _state
    with st.lock:
        batch = list(st.pending)
        st.pending.clear()
    if not batch:
        return True
    # Lines repeated within this batch first: one write each.
    merged: dict[tuple, dict[str, Any]] = {}
    for e in batch:
        k = _key(e)
        m = merged.get(k)
        if m is None or e["at"] - m["last"] > GROUP_SECONDS:
            if m is not None:  # far apart: a separate entry (rare within one batch)
                merged[(*k, m["first"])] = m
            merged[k] = {**e, "first": e["at"], "last": e["at"], "n": 1}
        else:
            m["last"], m["n"] = e["at"], m["n"] + 1
            if e["detail"]:
                m["detail"] = e["detail"]
    written: dict[tuple, tuple[int, float]] = {}
    try:
        async with db.session_factory()() as s:
            for k, m in merged.items():
                key = k[:5]
                prev = st.groups.get(key)
                if prev is not None and m["first"] - prev[1] <= GROUP_SECONDS:
                    values: dict[str, Any] = {"last_at": _dt(m["last"]), "count": LogEntry.count + m["n"]}
                    if m["detail"]:
                        values["detail"] = m["detail"]
                    res = await s.execute(update(LogEntry).where(LogEntry.id == prev[0]).values(**values))
                    if res.rowcount:
                        written[key] = (prev[0], m["last"])
                        continue
                row = LogEntry(
                    first_at=_dt(m["first"]),
                    last_at=_dt(m["last"]),
                    count=m["n"],
                    level=m["level"],
                    component=m["component"],
                    event=m["event"],
                    logger=m["logger"],
                    message=m["message"],
                    detail=m["detail"],
                )
                s.add(row)
                await s.flush()
                if key not in written or written[key][1] <= m["last"]:
                    written[key] = (row.id, m["last"])
            await s.commit()
    except Exception as exc:  # noqa: BLE001 - keep them for the next try
        with st.lock:
            room = PENDING_MAX - len(st.pending)
            keep = batch[-room:] if room > 0 else []
            st.dropped += len(batch) - len(keep)
            st.pending.extendleft(reversed(keep))
        st.last_write_error = f"{type(exc).__name__}: {exc}"[:200]
        return False
    st.groups.update(written)
    if len(st.groups) > 5000:  # forget the oldest
        for k, _ in sorted(st.groups.items(), key=lambda kv: kv[1][1])[:2500]:
            st.groups.pop(k, None)
    st.last_write_error = None
    hub.publish("log-updated")
    return True


async def prune() -> None:
    async with db.session_factory()() as s:
        await s.execute(delete(LogEntry).where(LogEntry.last_at < utcnow() - timedelta(days=RETAIN_DAYS)))
        keep = select(LogEntry.id).order_by(LogEntry.last_at.desc()).offset(RETAIN_ROWS).limit(100000)
        await s.execute(delete(LogEntry).where(LogEntry.id.in_(keep.scalar_subquery())))
        await s.commit()


async def _run() -> None:
    st = _state
    assert st.wake is not None
    pruned_at = 0.0
    while True:
        try:
            await asyncio.wait_for(st.wake.wait(), PRUNE_SECONDS)
            await asyncio.sleep(FLUSH_DELAY)
        except TimeoutError:
            pass
        st.wake.clear()
        ok = await flush()
        if ok and time.monotonic() - pruned_at > PRUNE_SECONDS:
            pruned_at = time.monotonic()
            try:
                await prune()
            except Exception:  # noqa: BLE001
                pass
        if not ok:
            await asyncio.sleep(RETRY_SECONDS)
            st.wake.set()


def start() -> None:
    st = _state
    install()
    st.loop = asyncio.get_running_loop()
    st.wake = asyncio.Event()
    st.groups.clear()  # the database may be a different one (tests); entries are found again by id
    st.started_at = time.time()
    st.task = asyncio.create_task(_run(), name="log-history")
    with st.lock:
        if st.pending:
            st.wake.set()


async def stop() -> None:
    st = _state
    if st.task:
        st.task.cancel()
        try:
            await st.task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        st.task = None
    try:
        await asyncio.wait_for(flush(), 5)  # the last lines (e.g. "MeshHome stopping")
    except Exception:  # noqa: BLE001
        pass
    st.loop = st.wake = None


def task() -> asyncio.Task | None:
    return _state.task


def stats() -> dict[str, Any]:
    st = _state
    with st.lock:
        return {
            "waiting": len(st.pending),
            "dropped": st.dropped,
            "write_error": st.last_write_error,
            "started_at": st.started_at,
        }


# ---- reading ---------------------------------------------------------------------------------


def _match(e: dict[str, Any], *, problems: bool, component: str | None, q: str | None) -> bool:
    if problems and e["level"] not in PROBLEM_LEVELS:
        return False
    if component and e["component"] != component:
        return False
    return not (q and q not in e["message"].lower() and q not in (e["detail"] or "").lower())


def _out_pending(e: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": None,
        "first_at": e["at"],
        "last_at": e["at"],
        "count": 1,
        "level": e["level"],
        "component": e["component"],
        "event": e["event"],
        "message": e["message"],
        "detail": e["detail"],
        "saved": False,
    }


def _out_row(r: LogEntry) -> dict[str, Any]:
    return {
        "id": r.id,
        "first_at": r.first_at.timestamp(),
        "last_at": r.last_at.timestamp(),
        "count": r.count,
        "level": r.level,
        "component": r.component,
        "event": r.event,
        "message": r.message,
        "detail": r.detail,
        "saved": True,
    }


async def history(
    *, problems: bool, component: str | None, q: str | None, offset: int, limit: int
) -> dict[str, Any]:
    """The kept history, newest first, with lines still waiting to be written on top."""
    q = (q or "").strip().lower() or None
    with _state.lock:
        waiting = [
            _out_pending(e)
            for e in reversed(_state.pending)
            if _match(e, problems=problems, component=component, q=q)
        ]
    items: list[dict[str, Any]] = waiting if offset == 0 else []
    more = False
    error = None
    try:
        async with db.session_factory()() as s:
            stmt = select(LogEntry)
            if problems:
                stmt = stmt.where(LogEntry.level.in_(PROBLEM_LEVELS))
            if component:
                stmt = stmt.where(LogEntry.component == component)
            if q:
                like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                stmt = stmt.where(or_(LogEntry.message.ilike(like), LogEntry.detail.ilike(like)))
            rows = (
                (
                    await s.execute(
                        stmt.order_by(LogEntry.last_at.desc(), LogEntry.id.desc())
                        .offset(offset)
                        .limit(limit + 1)
                    )
                )
                .scalars()
                .all()
            )
        more = len(rows) > limit
        items += [_out_row(r) for r in rows[:limit]]
    except Exception as exc:  # noqa: BLE001 - show what is in memory
        error = f"The database is unavailable, so only lines not yet saved are shown ({type(exc).__name__})."
    return {"items": items, "more": more, "error": error}


def recent(
    *, problems: bool, component: str | None, q: str | None, before: int | None, limit: int
) -> dict[str, Any]:
    """Everything from INFO up since MeshHome started (the last RECENT_MAX lines), newest first."""
    q = (q or "").strip().lower() or None
    with _state.lock:
        lines = list(_state.recent)
    out: list[dict[str, Any]] = []
    more = False
    for e in reversed(lines):
        if before is not None and e["seq"] >= before:
            continue
        if not _match(e, problems=problems, component=component, q=q):
            continue
        if len(out) == limit:
            more = True
            break
        out.append({**_out_pending(e), "seq": e["seq"], "saved": None})
    return {"items": out, "more": more, "error": None, "since": _state.started_at, "kept": RECENT_MAX}


async def problem_summary(hours: int = 24) -> list[dict[str, Any]]:
    """Warnings and errors in the last `hours`, per component: how many, the latest, when."""
    since = utcnow() - timedelta(hours=hours)
    async with db.session_factory()() as s:
        rows = (
            await s.execute(
                select(LogEntry.component, func.sum(LogEntry.count), func.max(LogEntry.last_at))
                .where(LogEntry.level.in_(PROBLEM_LEVELS), LogEntry.last_at >= since)
                .group_by(LogEntry.component)
            )
        ).all()
        out = []
        for comp, n, last in rows:
            latest = (
                await s.execute(
                    select(LogEntry.message, LogEntry.level)
                    .where(
                        LogEntry.component == comp,
                        LogEntry.level.in_(PROBLEM_LEVELS),
                        LogEntry.last_at >= since,
                    )
                    .order_by(LogEntry.last_at.desc())
                    .limit(1)
                )
            ).first()
            out.append(
                {
                    "component": comp,
                    "count": int(n or 0),
                    "last_at": last.timestamp(),
                    "latest": latest[0] if latest else "",
                    "level": latest[1] if latest else "WARNING",
                }
            )
    order = list(COMPONENTS)
    return sorted(out, key=lambda r: order.index(r["component"]) if r["component"] in order else 99)


async def events(component: str, *, limit: int = 20) -> list[dict[str, Any]]:
    """The latest events of one kind (e.g. the radio's connection history), newest first."""
    async with db.session_factory()() as s:
        rows = (
            (
                await s.execute(
                    select(LogEntry)
                    .where(LogEntry.event == component)
                    .order_by(LogEntry.last_at.desc(), LogEntry.id.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
    return [_out_row(r) for r in rows]
