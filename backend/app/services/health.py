"""Settings → Health & logs: what is working and what isn't, from MeshHome's point of view.

Each check reports a status (ok | info | warn | bad | off), a one-line summary, and, when something
is wrong, what it means and what to do. Checks never raise: a check that fails to run reports that.
The log history (services/app_log.py) supplies the recent problems per component.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text

from app import db
from app.config import APP_VERSION, get_settings
from app.models import CollectionGap, Contact, MeshNode, Message, TrackedDevice, WebPushSubscription, utcnow
from app.radio.supervisor import supervisor
from app.services import app_log, app_settings, radio_hat, system_config, updates

log = logging.getLogger(__name__)

STARTED_AT = time.time()
TECH_NAME = {"meshcore": "MeshCore", "meshtastic": "Meshtastic"}
DEFAULT_PORT = {"meshcore": 5000, "meshtastic": 4403}
QUIET_WARN_SECONDS = 6 * 3600  # connected but nothing heard from the mesh for this long
STUCK_QUEUE_SECONDS = 600
ORDER = {"bad": 0, "warn": 1, "info": 2, "ok": 3, "off": 4}


def _item(
    id: str,
    title: str,
    status: str,
    summary: str,
    *,
    detail: str | None = None,
    fix: list[str] | None = None,
    facts: list[tuple[str, str]] | None = None,
    link: str | None = None,
    link_label: str | None = None,
    logs: str | None = None,
    since: float | None = None,
) -> dict[str, Any]:
    return {
        "id": id,
        "title": title,
        "status": status,
        "summary": summary,
        "detail": detail,
        "fix": fix or [],
        "facts": [{"label": k, "value": v} for k, v in (facts or []) if v not in (None, "")],
        "link": link,
        "link_label": link_label,
        "logs": logs,
        "since": since,
    }


def ago(epoch: float | None, now: float | None = None) -> str:
    if not epoch:
        return "never"
    s = max(0, int((now or time.time()) - epoch))
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        h, m = divmod(s // 60, 60)
        return f"{h} h {m} min ago" if m and h < 6 else f"{h} h ago"
    return f"{s // 86400} d ago"


def span(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        h, m = divmod(s // 60, 60)
        return f"{h} h {m} min" if m else f"{h} h"
    d, h = divmod(s // 3600, 24)
    return f"{d} d {h} h" if h else f"{d} d"


def _ts(dt: datetime | None) -> float | None:
    return dt.timestamp() if dt else None


# ---- background tasks ------------------------------------------------------------------------

_watched: dict[str, tuple[str, str, Callable[[], asyncio.Task | None]]] = {}


def watch(
    key: str,
    label: str,
    component: str,
    get_task: Callable[[], asyncio.Task | None],
    *,
    log_errors: bool = True,
) -> None:
    """Report this background task in the health check, and log it if it ever stops with an error
    (log_errors=False: it logs that itself)."""
    _watched[key] = (label, component, get_task)
    t = get_task()
    if t is not None and log_errors:
        t.add_done_callback(lambda task: _on_done(label, component, task))


def _on_done(label: str, component: str, task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error(
            "%s stopped unexpectedly: %s",
            label,
            f"{type(exc).__name__}: {exc}",
            exc_info=(type(exc), exc, exc.__traceback__),
            extra={"component": component, "event": "task"},
        )


def _services() -> dict[str, Any]:
    stopped, running = [], []
    for label, _comp, get in _watched.values():
        t = get()
        if t is None:
            continue  # not used on this install (e.g. no sensors)
        if t.done():
            exc = None if t.cancelled() else t.exception()
            stopped.append((label, f"{type(exc).__name__}: {exc}" if exc else "ended"))
        else:
            running.append(label)
    if stopped:
        return _item(
            "services",
            "Background services",
            "bad",
            "Stopped: " + ", ".join(label for label, _ in stopped),
            detail="These parts of MeshHome stopped after an error and won't run again until MeshHome restarts.",
            fix=[
                "Restart MeshHome: sudo meshhome restart (or restart the container)",
                "Send the log below with a bug report",
            ],
            facts=[(label, why) for label, why in stopped],
            logs="app",
        )
    return _item(
        "services",
        "Background services",
        "ok",
        f"All running ({', '.join(running)})" if running else "All running",
    )


# ---- the radio -------------------------------------------------------------------------------


def _mode_text(cfg: app_settings.RadioConfig, tech: str) -> str:
    if cfg.mode == "hat":
        return f"Radio HAT on this Pi ({'meshtasticd' if tech == 'meshtastic' else 'ZephCore'})"
    if cfg.mode == "simulated":
        return "Simulated"
    if cfg.mode == "tcp":
        return f"Network, {cfg.host}:{cfg.port}" if cfg.host else "Network (no address yet)"
    return "None"


def explain(error: str, cfg: app_settings.RadioConfig, tech: str) -> tuple[str, str, list[str]]:
    """A radio connection error, in words: (what's wrong, why, what to do)."""
    e = (error or "").lower()
    mesh = TECH_NAME[tech]
    hat = cfg.mode == "hat"
    where = "the radio HAT" if hat else f"{cfg.host}:{cfg.port}"
    sw = "meshtasticd" if tech == "meshtastic" else "ZephCore"
    hat_fix = [
        "Check the Radio HAT line below: its software should be running",
        "Restart it: Settings → Radio → Radio HAT → Restart",
        "Its own log: sudo meshhome radio-hat logs",
    ]
    if "database" in e or "errno 2]" in e or "no such file or directory" in e:
        return (
            "MeshHome's database isn't answering",
            "MeshHome needs its database (PostgreSQL) to connect to the radio. It's usually away only briefly, "
            "for example while an automatic security update restarts it, and MeshHome retries until it's back.",
            [
                "Wait a minute: it normally recovers by itself",
                "If it doesn't: sudo systemctl status postgresql",
            ],
        )
    if "another app instance" in e:
        return (
            "Another copy of MeshHome is using the radio",
            "Two copies of MeshHome share this database, and only one may use the radio. This one shows history only.",
            ["Stop the other copy (an old container, a second replica, or a test install)"],
        )
    if "refused" in e or "errno 111" in e or "errno 61" in e:
        if hat:
            return (
                f"The radio HAT's software ({sw}) isn't accepting connections",
                f"{sw} isn't running, or is still starting.",
                hat_fix,
            )
        return (
            f"Nothing is accepting connections at {where}",
            f"The device answers at that address, but nothing listens on port {cfg.port}. "
            f"A {mesh} radio's network API is usually on port {DEFAULT_PORT[tech]}.",
            [
                f"Check the port (usually {DEFAULT_PORT[tech]} for {mesh})",
                f"Check the device runs {mesh} with its network (Wi-Fi or Ethernet) API turned on",
                "Check Mesh in Settings → Radio matches the radio (MeshCore or Meshtastic)",
            ],
        )
    if "timed out" in e or "timeout" in e:
        if hat:
            return (
                f"The radio HAT's software ({sw}) didn't answer in time",
                f"{sw} may be stuck or restarting.",
                hat_fix,
            )
        return (
            f"No answer from {where}",
            "Nothing answered at that address: the radio may be off, out of Wi-Fi range, or have a new address "
            "(routers can hand out a different one after a restart).",
            [
                "Check the radio is on and connected to the network",
                "Check its address, and give it a fixed one in your router",
            ],
        )
    if "no route" in e or "unreachable" in e or "errno 113" in e or "errno 101" in e or "errno 65" in e:
        return (
            f"The network can't reach {cfg.host}",
            "This computer has no way to that address: a different network, or the device is gone from it.",
            [
                "Check the address is on this computer's network",
                "Check the radio is connected to the network",
            ],
        )
    if (
        "name or service not known" in e
        or "nodename nor servname" in e
        or "getaddrinfo" in e
        or "errno -2" in e
    ):
        return (
            f"The name {cfg.host} can't be found",
            "The radio's name isn't known on this network.",
            ["Use the radio's IP address instead", "Check the name for typos"],
        )
    if "connection lost" in e or "reset by peer" in e or "broken pipe" in e or "eof" in e or "closed" in e:
        if tech == "meshtastic":
            why = (
                "The radio closed the connection. A Meshtastic node talks to one app at a time over the network: "
                "when the Meshtastic app on a phone or computer connects to it over Wi-Fi, MeshHome is cut off."
            )
            fix = [
                "Connect the Meshtastic app over Bluetooth instead of Wi-Fi",
                "Check whether the radio restarted (power, or a settings change)",
            ]
        else:
            why = "The radio closed the connection: it restarted, or another app connected to it."
            fix = [
                "Check whether the radio restarted (power, or a firmware update)",
                "Close other apps connected to it over the network",
            ]
        if hat:
            fix = [*hat_fix[:1], *fix[1:]]
        return ("The radio dropped the connection", why, fix)
    if "handshake" in e or "unexpected" in e or "protocol" in e or "decode" in e:
        return (
            f"Something answered at {where}, but not as a {mesh} radio",
            f"MeshHome is set to {mesh}; the device may run the other one, or the port belongs to something else.",
            [
                "Check Mesh in Settings → Radio matches the radio",
                f"Check the port (usually {DEFAULT_PORT[tech]} for {mesh})",
            ],
        )
    return (error or "Not connected", "", ["See the radio's lines in the log below"])


async def _radio(cfg: app_settings.RadioConfig) -> dict[str, Any]:
    st = supervisor.status
    tech = app_settings.effective_tech(cfg)
    mesh = TECH_NAME[tech]
    facts: list[tuple[str, str]] = [("Mesh", mesh), ("Connection", _mode_text(cfg, tech))]
    if st.radio_name:
        facts.append(("Radio", st.radio_name))
    if st.reconnects:
        facts.append(("Reconnect attempts", f"{st.reconnects} since MeshHome started"))
    title = "Radio connection"
    link = "/settings/radio"
    s = st.state
    if s == "disabled":
        return _item(
            "radio", title, "off", "Turned off for this installation (RADIO_ENABLED=false)", facts=facts
        )
    if s == "not_configured":
        return _item(
            "radio",
            title,
            "warn",
            "No radio is set up",
            detail="MeshHome isn't connected to a radio, so it can't receive or send messages.",
            fix=["Choose how MeshHome reaches the radio in Settings → Radio"],
            facts=facts,
            link=link,
            link_label="Radio settings",
            logs="radio",
        )
    if s == "paused":
        return _item(
            "radio",
            title,
            "warn",
            "Paused for maintenance",
            detail="The connection is released so another app can use the radio. Nothing is received meanwhile.",
            fix=["Resume it in Settings → Radio"],
            facts=facts,
            link=link,
            link_label="Radio settings",
            logs="radio",
        )
    if s == "lock_unavailable":
        what, why, fix = explain("another app instance", cfg, tech)
        return _item("radio", title, "bad", what, detail=why, fix=fix, facts=facts, logs="radio")
    if s in ("starting", "connecting"):
        return _item("radio", title, "info", st.detail or "Connecting…", facts=facts, logs="radio")
    if s == "connected":
        if st.connected_since:
            facts.append(("Connected", f"since {ago(st.connected_since)}"))
        if st.is_simulated:
            return _item(
                "radio",
                title,
                "info",
                f"Connected to a simulated {mesh} radio",
                detail="Nothing is really sent or received over the air. Choose a real radio in Settings → Radio.",
                facts=facts,
                link=link,
                link_label="Radio settings",
                logs="radio",
                since=st.connected_since,
            )
        if st.storage_warning:
            return _item(
                "radio",
                title,
                "warn",
                st.storage_warning,
                facts=facts,
                link=link,
                logs="radio",
                since=st.connected_since,
            )
        return _item(
            "radio",
            title,
            "ok",
            st.detail or "Connected",
            facts=facts,
            logs="radio",
            since=st.connected_since,
        )
    # backoff
    what, why, fix = explain(st.last_error or "", cfg, tech)
    if st.last_error:
        facts.append(("Error", st.last_error))
    if st.next_retry_at:
        facts.append(("Next attempt", f"in {span(st.next_retry_at - time.time())}"))
    try:
        offline_since = await _offline_since()
    except Exception:  # noqa: BLE001
        offline_since = None
    summary = f"Offline: {what}"
    if offline_since:
        summary += f" (since {ago(offline_since)})"
    return _item(
        "radio",
        title,
        "bad",
        summary,
        detail=why or None,
        fix=fix,
        facts=facts,
        link=link,
        link_label="Radio settings",
        logs="radio",
        since=offline_since,
    )


async def _offline_since() -> float | None:
    async with db.session_factory()() as s:
        gap = (
            await s.execute(
                select(CollectionGap.started_at)
                .where(CollectionGap.ended_at.is_(None))
                .order_by(CollectionGap.started_at)
                .limit(1)
            )
        ).scalar()
    return _ts(gap)


async def _radio_hat(cfg: app_settings.RadioConfig) -> dict[str, Any] | None:
    if not radio_hat.native():
        return None
    info = await radio_hat.info()
    svc = info.get("service") or {}
    if info.get("phase") == "absent" and cfg.mode != "hat":
        return None
    fw = info.get("firmware") or "meshcore"
    sw = "meshtasticd" if fw == "meshtastic" else "ZephCore"
    facts = [
        ("Runs", f"{sw} ({TECH_NAME.get(fw, fw)})"),
        ("Version", info.get("meshtastic_version") if fw == "meshtastic" else info.get("installed_version")),
        ("Service", svc.get("state")),
        ("Pi", info.get("model")),
    ]
    if fw == "meshtastic" and info.get("meshtastic_module"):
        facts.append(
            ("LoRa module", f"RAK{info['meshtastic_module']} in IO slot {info.get('meshtastic_slot') or 1}")
        )
    restarts = svc.get("restarts")
    if restarts:
        facts.append(("Restarts", f"{restarts} since the Pi started"))
    phase = info.get("phase")
    title = "Radio HAT"
    link = "/settings/radio"
    logs_fix = "Its own log: sudo meshhome radio-hat logs"
    if phase == "absent":
        return _item(
            "radio_hat",
            title,
            "bad",
            "MeshHome is set to use the radio HAT, but it isn't set up",
            fix=["Set it up in Settings → Radio → Radio HAT"],
            facts=facts,
            link=link,
            link_label="Radio settings",
            logs="radio_hat",
        )
    if phase in ("checking", "installing", "rebooting"):
        return _item(
            "radio_hat",
            title,
            "info",
            info.get("last_message") or "Being set up…",
            facts=facts,
            logs="radio_hat",
        )
    if phase == "needs_reboot":
        return _item(
            "radio_hat",
            title,
            "warn",
            "Restart the Pi to finish setting it up",
            detail="The LoRa module's SPI bus appears only after a restart, and the radio software waits for it.",
            fix=["Restart the Pi: Settings → Radio → Radio HAT → Restart the Pi, or sudo reboot"],
            facts=facts,
            link=link,
            link_label="Radio settings",
            logs="radio_hat",
        )
    if phase == "stopped" or not svc.get("active"):
        return _item(
            "radio_hat",
            title,
            "bad",
            f"{sw} isn't running",
            detail=f"{sw} drives the HAT's LoRa module, and MeshHome connects to it. "
            "It may have stopped after an error, for example if the module isn't detected.",
            fix=[
                "Restart it: Settings → Radio → Radio HAT → Restart",
                logs_fix,
                "Check the HAT is seated and the module is in the IO slot chosen at setup",
            ],
            facts=facts,
            link=link,
            link_label="Radio settings",
            logs="radio_hat",
        )
    if restarts and restarts >= 3:
        return _item(
            "radio_hat",
            title,
            "warn",
            f"{sw} is running, but has restarted {restarts} times",
            detail="systemd restarts it when it stops with an error, so it may be crashing.",
            fix=[logs_fix],
            facts=facts,
            logs="radio_hat",
        )
    return _item("radio_hat", title, "ok", f"{sw} is running", facts=facts, logs="radio_hat")


async def _mesh(cfg: app_settings.RadioConfig) -> dict[str, Any] | None:
    st = supervisor.status
    if st.state != "connected" or st.is_simulated:
        return None
    tech = app_settings.effective_tech(cfg)
    heard = [st.last_heard_at]
    adapter = supervisor.adapter
    if adapter is not None:
        heard.append(getattr(adapter, "last_heard_at", None))
    async with db.session_factory()() as s:
        if tech == "meshtastic":
            heard.append(_ts((await s.execute(select(func.max(MeshNode.last_heard_at)))).scalar()))
        elif supervisor.radio is not None:
            heard.append(
                _ts(
                    (
                        await s.execute(
                            select(func.max(Contact.last_advert_at)).where(
                                Contact.radio_id == supervisor.radio.id
                            )
                        )
                    ).scalar()
                )
            )
    now = time.time()
    last = max((h for h in heard if h and h <= now + 60), default=None)
    facts = [
        ("Last heard", ago(last) if last else "nothing yet"),
        ("Messages received", f"{st.received} since connecting"),
        ("Messages sent", f"{st.sent} since connecting"),
    ]
    connected_for = now - (st.connected_since or now)
    quiet = (now - last) if last else connected_for
    if quiet > QUIET_WARN_SECONDS and connected_for > QUIET_WARN_SECONDS:
        return _item(
            "mesh",
            "Hearing the mesh",
            "warn",
            f"Nothing heard from other nodes for {span(quiet)}",
            detail="The radio is connected, but hasn't heard another node. On a busy mesh that points at the "
            "antenna or the radio settings; on a quiet one it can be normal.",
            fix=[
                "Check the antenna is attached",
                "Check the LoRa settings (region, preset or frequency) match your other radios: Settings → Radio → Configure node settings",
                "Check another node is in range and switched on",
            ],
            facts=facts,
            link="/settings/node",
            link_label="Node settings",
            logs="radio",
        )
    if last is None:
        return _item(
            "mesh",
            "Hearing the mesh",
            "info",
            "Nothing heard from other nodes yet",
            facts=facts,
            logs="radio",
        )
    return _item(
        "mesh", "Hearing the mesh", "ok", f"Last heard another node {ago(last)}", facts=facts, logs="radio"
    )


async def _sending() -> dict[str, Any]:
    since = utcnow() - timedelta(hours=24)
    async with db.session_factory()() as s:
        waiting = (
            await s.execute(
                select(func.count(), func.min(Message.created_at)).where(
                    Message.direction == "out", Message.state.in_(("queued", "sending"))
                )
            )
        ).one()
        by_state = dict(
            (
                await s.execute(
                    select(Message.state, func.count())
                    .where(Message.direction == "out", Message.created_at >= since)
                    .group_by(Message.state)
                )
            ).all()
        )
    n_wait, oldest = int(waiting[0] or 0), _ts(waiting[1])
    sent = sum(by_state.values())
    delivered = by_state.get("acknowledged", 0)
    unconfirmed = sum(by_state.get(k, 0) for k in ("no_ack", "failed", "expired", "uncertain"))
    facts = [
        ("Last 24 hours", f"{sent} sent" + (f", {delivered} confirmed delivered" if delivered else "")),
        ("Not delivered", str(unconfirmed) if unconfirmed else None),
        ("Waiting to send", f"{n_wait} (oldest {ago(oldest)})" if n_wait else None),
    ]
    title = "Sending messages"
    connected = supervisor.status.state == "connected"
    if n_wait and not connected:
        return _item(
            "sending",
            title,
            "warn",
            f"{n_wait} message{'s' if n_wait != 1 else ''} waiting for the radio",
            detail="They're sent once the radio is connected again.",
            facts=facts,
            logs="radio",
        )
    if n_wait and oldest and time.time() - oldest > STUCK_QUEUE_SECONDS:
        return _item(
            "sending",
            title,
            "bad",
            f"Messages are stuck: the oldest has waited {span(time.time() - oldest)}",
            detail="The radio is connected, but messages aren't leaving the queue.",
            fix=["Look for radio errors in the log below", "Restart MeshHome: sudo meshhome restart"],
            facts=facts,
            logs="radio",
        )
    tried = delivered + unconfirmed
    if tried >= 4 and unconfirmed * 2 >= tried:
        return _item(
            "sending",
            title,
            "warn",
            f"{unconfirmed} of {tried} direct messages in the last 24 hours weren't confirmed delivered",
            detail="The recipients may be out of range, or switched off. Messages still sent may have arrived without "
            "the confirmation coming back.",
            fix=[
                "Check the recipients are in range (Contacts shows when each was last heard)",
                "MeshCore: reset a contact's route so the next message finds a new one",
            ],
            facts=facts,
            logs="radio",
        )
    return _item(
        "sending",
        title,
        "ok",
        f"{sent} sent in the last 24 hours" if sent else "Nothing sent in the last 24 hours",
        facts=facts,
    )


async def _collection() -> dict[str, Any]:
    since = utcnow() - timedelta(hours=24)
    async with db.session_factory()() as s:
        gaps = (
            (
                await s.execute(
                    select(CollectionGap)
                    .where((CollectionGap.ended_at.is_(None)) | (CollectionGap.started_at >= since))
                    .order_by(CollectionGap.started_at.desc())
                    .limit(6)
                )
            )
            .scalars()
            .all()
        )
    open_gap = next((g for g in gaps if g.ended_at is None), None)
    closed = [g for g in gaps if g.ended_at is not None]
    facts = [
        (
            g.started_at.astimezone().strftime("%H:%M" if g.started_at > since else "%b %d %H:%M"),
            f"{span((g.ended_at - g.started_at).total_seconds())} · {g.reason}",
        )
        for g in closed[:5]
    ]
    title = "Message collection"
    if open_gap is not None:
        return _item(
            "collection",
            title,
            "warn",
            f"Not collecting since {ago(open_gap.started_at.timestamp())}: {open_gap.reason}",
            detail="Messages sent on the mesh meanwhile aren't received. The radio keeps some for MeshHome to fetch "
            "when it reconnects (MeshCore keeps direct messages; channels only while it's on).",
            facts=facts,
            logs="radio",
            since=open_gap.started_at.timestamp(),
        )
    if closed:
        total = sum((g.ended_at - g.started_at).total_seconds() for g in closed)
        return _item(
            "collection",
            title,
            "info",
            f"Collecting. {len(closed)} interruption{'s' if len(closed) != 1 else ''} in the last 24 hours ({span(total)} in all)",
            facts=facts,
            logs="radio",
        )
    return _item("collection", title, "ok", "Collecting, with no interruptions in the last 24 hours")


async def _trackers(tech: str) -> dict[str, Any] | None:
    async with db.session_factory()() as s:
        cfg = await app_settings.get_tracking_config(s)
        rows = (
            await s.execute(
                select(TrackedDevice, Contact.name, MeshNode.long_name, MeshNode.last_heard_at)
                .outerjoin(Contact, Contact.id == TrackedDevice.contact_id)
                .outerjoin(MeshNode, MeshNode.num == TrackedDevice.mesh_node_num)
            )
        ).all()
    devices = [
        (d, d.label or cname or mname or "Tracker", heard)
        for d, cname, mname, heard in rows
        if (d.mesh_node_num is not None) == (tech == "meshtastic")
    ]
    if not devices:
        return None
    title = "Trackers"
    if not cfg.enabled:
        return _item(
            "trackers",
            title,
            "off",
            "Tracking is turned off",
            link="/settings/tracking",
            link_label="Tracking settings",
        )
    now = time.time()
    facts, silent, failing = [], [], []
    for d, name, heard in devices:
        last = max((t for t in (_ts(d.last_reply_at), _ts(heard)) if t), default=None)
        note = (
            f"position {ago(last)}"
            if d.last_reply_at
            else ("never answered" if d.status == "new" else d.status.replace("_", " "))
        )
        if d.status in ("no_answer", "error") and d.failures >= 3:
            failing.append(name)
            note = f"not answering ({d.failures} times in a row)" + (
                f", last position {ago(_ts(d.last_reply_at))}" if d.last_reply_at else ""
            )
        elif d.last_reply_at and now - d.last_reply_at.timestamp() > max(3 * d.interval_s, 900):
            silent.append(name)
        facts.append((name, note))
    connected = supervisor.status.state == "connected"
    if connected and failing:
        return _item(
            "trackers",
            title,
            "warn",
            ("Not answering: " if tech == "meshcore" else "Not heard: ") + ", ".join(failing),
            detail="A tracker that's switched off, out of range, or out of battery doesn't answer.",
            fix=["Check it's on and charged", "Check it's within range of the radio or a repeater"],
            facts=facts,
            link="/tracking",
            link_label="Tracking",
            logs="tracking",
        )
    if connected and silent:
        return _item(
            "trackers",
            title,
            "info",
            "No recent position from " + ", ".join(silent),
            detail=(
                "Meshtastic trackers send their position on their own schedule, on the channel they share with this radio."
                if tech == "meshtastic"
                else None
            ),
            fix=["Check it's on and charged", "Check it's within range"]
            + (
                ["Check it's on the same primary channel as this radio (its QR code in Node settings)"]
                if tech == "meshtastic"
                else []
            ),
            facts=facts,
            link="/tracking",
            link_label="Tracking",
            logs="tracking",
        )
    return _item(
        "trackers",
        title,
        "ok",
        f"{len(devices)} tracker{'s' if len(devices) != 1 else ''}",
        facts=facts,
        link="/tracking",
        link_label="Tracking",
        logs="tracking",
    )


async def _database() -> dict[str, Any]:
    title = "Database"
    stats = app_log.stats()
    started = time.monotonic()
    try:
        async with db.session_factory()() as s:
            await s.execute(text("SELECT 1"))
            ms = (time.monotonic() - started) * 1000
            size = (await s.execute(text("SELECT pg_database_size(current_database())"))).scalar()
            version = (await s.execute(text("SHOW server_version"))).scalar()
    except Exception as exc:  # noqa: BLE001
        return _item(
            "database",
            title,
            "bad",
            "PostgreSQL isn't answering",
            detail="MeshHome keeps everything in PostgreSQL. Without it, nothing is saved and the radio waits.",
            fix=[
                "It usually comes back within a minute (e.g. after an automatic update)",
                "If not: sudo systemctl status postgresql",
            ],
            facts=[("Error", f"{type(exc).__name__}: {exc}"[:200])],
            logs="database",
        )
    facts = [
        ("PostgreSQL", str(version).split(" ")[0]),
        ("Size", f"{(size or 0) / 1e6:.0f} MB"),
        ("Response", f"{ms:.0f} ms"),
    ]
    if stats["dropped"]:
        facts.append(("Log lines lost", f"{stats['dropped']} (while the database was away)"))
    if ms > 2000:
        return _item(
            "database",
            title,
            "warn",
            f"Slow: it took {ms / 1000:.1f} s to answer",
            detail="A slow SD card or a busy Pi slows everything down.",
            facts=facts,
            logs="database",
        )
    return _item("database", title, "ok", "Answering", facts=facts, logs="database")


async def _notifications() -> dict[str, Any]:
    async with db.session_factory()() as s:
        subs = (await s.execute(select(WebPushSubscription))).scalars().all()
    title = "Notifications"
    link = "/settings/notifications"
    if not subs:
        return _item(
            "notifications",
            title,
            "off",
            "No device has notifications turned on",
            link=link,
            link_label="Notifications",
        )
    failing = [x for x in subs if x.last_error]
    facts = [
        (
            _device(x.user_agent),
            f"error: {x.last_error}"
            if x.last_error
            else (f"last sent {ago(_ts(x.last_sent_at))}" if x.last_sent_at else "nothing sent yet"),
        )
        for x in subs
    ]
    if failing:
        return _item(
            "notifications",
            title,
            "warn",
            f"Not reaching {len(failing)} of {len(subs)} device{'s' if len(subs) != 1 else ''}",
            detail="The device's push service refused the last notification. Turning notifications off and on again on "
            "that device usually fixes it.",
            fix=["On that device: Settings → Notifications → turn them off, then on"],
            facts=facts,
            link=link,
            link_label="Notifications",
            logs="notifications",
        )
    return _item(
        "notifications",
        title,
        "ok",
        f"On for {len(subs)} device{'s' if len(subs) != 1 else ''}",
        facts=facts,
        link=link,
        link_label="Notifications",
    )


def _device(ua: str | None) -> str:
    u = (ua or "").lower()
    os_ = next(
        (
            n
            for k, n in (
                ("iphone", "iPhone"),
                ("ipad", "iPad"),
                ("android", "Android"),
                ("mac os", "Mac"),
                ("windows", "Windows"),
                ("linux", "Linux"),
            )
            if k in u
        ),
        "A device",
    )
    br = next(
        (
            n
            for k, n in (("edg/", "Edge"), ("firefox", "Firefox"), ("chrome", "Chrome"), ("safari", "Safari"))
            if k in u
        ),
        "",
    )
    return f"{os_}{' · ' + br if br else ''}"


def _updates() -> dict[str, Any]:
    u = updates.checker.summary()
    title = "Software"
    link = "/settings/updates"
    facts = [("Version", APP_VERSION)]
    if not u["checks_enabled"]:
        return _item("updates", title, "off", f"MeshHome {APP_VERSION} (update checks are off)", facts=facts)
    latest = (u.get("latest") or {}).get("version")
    if u.get("update_available") and latest:
        return _item(
            "updates",
            title,
            "info",
            f"MeshHome {latest} is available (this is {APP_VERSION})",
            facts=facts,
            link=link,
            link_label="Software",
        )
    if u.get("error"):
        facts.append(("Last check", ago(u.get("checked_at"))))
        return _item(
            "updates",
            title,
            "warn",
            "Couldn't check for updates",
            detail=str(u["error"]),
            fix=["Check this computer can reach the internet (github.com)"],
            facts=facts,
            link=link,
            link_label="Software",
            logs="updates",
        )
    return _item(
        "updates",
        title,
        "ok",
        f"MeshHome {APP_VERSION} is up to date",
        facts=facts,
        link=link,
        link_label="Software",
    )


def _https() -> dict[str, Any] | None:
    cert = system_config.certificate()
    st = system_config.status() or {}
    if not cert and not st:
        return None
    title = "HTTPS certificate"
    link = "/settings/network"
    facts = [("Name", cert.get("host") if cert else None), ("Issuer", cert.get("issuer") if cert else None)]
    if st.get("state") == "failed":
        return _item(
            "https",
            title,
            "warn",
            "The last network or HTTPS change failed",
            detail=st.get("message"),
            fix=["Open Settings → Network & HTTPS for details"],
            facts=facts,
            link=link,
            link_label="Network & HTTPS",
            logs="network",
        )
    if not cert or not cert.get("not_after"):
        return None
    try:
        expires = datetime.fromisoformat(str(cert["not_after"]).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    days = (expires - time.time()) / 86400
    facts.append(("Valid until", datetime.fromtimestamp(expires).strftime("%Y-%m-%d")))
    if days < 0:
        return _item(
            "https",
            title,
            "bad",
            "The certificate has expired",
            detail="Browsers warn about the site, and installed apps stop working.",
            fix=["Renew it in Settings → Network & HTTPS"],
            facts=facts,
            link=link,
            link_label="Network & HTTPS",
            logs="network",
        )
    if days < 14:
        return _item(
            "https",
            title,
            "warn",
            f"The certificate expires in {int(days)} days",
            detail="It should renew itself about 30 days before; it hasn't.",
            fix=["Renew it in Settings → Network & HTTPS"],
            facts=facts,
            link=link,
            link_label="Network & HTTPS",
            logs="network",
        )
    return _item("https", title, "ok", f"Valid for {int(days)} more days", facts=facts)


def _read(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


async def _system() -> dict[str, Any] | None:
    if not sys.platform.startswith("linux"):
        return None
    facts: list[tuple[str, str]] = []
    problems: list[tuple[str, str]] = []  # (status, text)
    path = get_settings().state_dir or "/"
    try:
        du = shutil.disk_usage(path if os.path.isdir(path) else "/")
        used = du.used / du.total
        facts.append(("Disk", f"{used:.0%} used, {du.free / 1e9:.1f} GB free"))
        if used > 0.97:
            problems.append(("bad", "The disk is full"))
        elif used > 0.90:
            problems.append(("warn", f"The disk is {used:.0%} full"))
    except OSError:
        pass
    mem = _read("/proc/meminfo")
    if mem:
        kv = {line.split(":")[0]: line.split(":")[1].strip() for line in mem.splitlines() if ":" in line}
        try:
            total, avail = int(kv["MemTotal"].split()[0]), int(kv["MemAvailable"].split()[0])
            facts.append(("Memory", f"{avail / 1024:.0f} MB free of {total / 1024:.0f} MB"))
            if avail / total < 0.05:
                problems.append(("warn", "Memory is nearly full"))
        except (KeyError, ValueError, ZeroDivisionError):
            pass
    t = _read("/sys/class/thermal/thermal_zone0/temp")
    if t and t.lstrip("-").isdigit():
        c = int(t) / 1000
        facts.append(("Temperature", f"{c:.0f} °C"))
        if c >= 85:
            problems.append(("bad", f"Very hot ({c:.0f} °C): the Pi slows itself down"))
        elif c >= 80:
            problems.append(("warn", f"Hot ({c:.0f} °C)"))
    hwmon = "/sys/class/hwmon"
    if os.path.isdir(hwmon):
        for d in os.listdir(hwmon):
            if _read(f"{hwmon}/{d}/name") == "rpi_volt":
                alarm = _read(f"{hwmon}/{d}/in0_lcrit_alarm")
                facts.append(("Power supply", "under-voltage now" if alarm == "1" else "OK"))
                if alarm == "1":
                    problems.append(("warn", "Under-voltage: the power supply is too weak"))
    up = _read("/proc/uptime")
    if up:
        facts.append(("Running for", span(float(up.split()[0]))))
    sync = await _ntp_synced()
    if sync is not None:
        facts.append(("Clock", "synchronized" if sync else "not synchronized"))
        if not sync:
            problems.append(("warn", "The clock isn't synchronized"))
    if not facts:
        return None
    title = "This computer"
    if problems:
        status = "bad" if any(p[0] == "bad" for p in problems) else "warn"
        fixes = {
            "disk": "Free space: sudo meshhome status shows what's using it; old backups can go",
            "Under": "Use the official Raspberry Pi power supply (5 V, 3 A or more; 5 A for a Pi 5)",
            "hot": "Give the Pi airflow or a heatsink/fan",
            "clock": "Check the Pi can reach the internet (it sets its clock from time servers)",
        }
        fix = [v for k, v in fixes.items() if any(k.lower() in p[1].lower() for p in problems)]
        return _item("system", title, status, "; ".join(p[1] for p in problems), fix=fix, facts=facts)
    return _item("system", title, "ok", "Disk, memory and temperature are fine", facts=facts)


async def _ntp_synced() -> bool | None:
    if not shutil.which("timedatectl"):
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "timedatectl",
            "show",
            "-p",
            "NTPSynchronized",
            "--value",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), 3)
    except (OSError, TimeoutError):
        return None
    v = out.decode().strip()
    return {"yes": True, "no": False}.get(v)


# ---- the report ------------------------------------------------------------------------------


async def report() -> dict[str, Any]:
    try:
        async with db.session_factory()() as s:
            cfg = await app_settings.get_radio_config(s)
    except Exception:  # noqa: BLE001 - the database check reports it
        cfg = app_settings.RadioConfig(mode=supervisor.status.mode, tech=supervisor.status.tech)  # type: ignore[arg-type]
    tech = app_settings.effective_tech(cfg)
    checks = [
        ("radio", "Radio connection", lambda: _radio(cfg)),
        ("radio_hat", "Radio HAT", lambda: _radio_hat(cfg)),
        ("mesh", "Hearing the mesh", lambda: _mesh(cfg)),
        ("sending", "Sending messages", _sending),
        ("collection", "Message collection", _collection),
        ("trackers", "Trackers", lambda: _trackers(tech)),
        ("database", "Database", _database),
        ("services", "Background services", _services),
        ("notifications", "Notifications", _notifications),
        ("https", "HTTPS certificate", _https),
        ("updates", "Software", _updates),
        ("system", "This computer", _system),
    ]
    items = []
    for id, title, check in checks:
        try:
            result = check()
            r = await result if asyncio.iscoroutine(result) else result
        except Exception as exc:  # noqa: BLE001 - one broken check mustn't hide the others
            log.warning("health check %s failed: %s", id, exc)
            r = _item(
                id,
                title,
                "warn",
                "This check couldn't run",
                facts=[("Error", f"{type(exc).__name__}: {exc}"[:200])],
            )
        if r is not None:
            items.append(r)
    try:
        problems = await app_log.problem_summary()
    except Exception:  # noqa: BLE001
        problems = []
    for p in problems:
        p["label"] = app_log.COMPONENTS.get(p["component"], p["component"])
    worst = min((i["status"] for i in items), key=lambda s: ORDER[s], default="ok")
    attention = [i for i in items if i["status"] in ("bad", "warn")]
    return {
        "checked_at": time.time(),
        "version": APP_VERSION,
        "started_at": STARTED_AT,
        "install_kind": get_settings().install_kind,
        "overall": worst if worst in ("bad", "warn") else "ok",
        "summary": (
            "Everything is working"
            if not attention
            else f"{len(attention)} thing{'s' if len(attention) != 1 else ''} need{'s' if len(attention) == 1 else ''} attention"
        ),
        "items": items,
        "problems": problems,
    }


def _fmt_time(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")


async def text_report() -> str:
    """The health report and recent problems as plain text, to paste when asking for help."""
    r = await report()
    mark = {"ok": "✓", "info": "•", "warn": "!", "bad": "✗", "off": "-"}
    out = [
        f"MeshHome {r['version']} health report · {_fmt_time(r['checked_at'])}",
        f"Install: {r['install_kind']} · running for {span(r['checked_at'] - r['started_at'])}",
        f"Overall: {r['summary']}",
        "",
    ]
    for i in r["items"]:
        out.append(f"{mark[i['status']]} {i['title']}: {i['summary']}")
        if i["detail"] and i["status"] in ("warn", "bad"):
            out.append(f"    {i['detail']}")
        for f in i["facts"]:
            out.append(f"    {f['label']}: {f['value']}")
        for n, step in enumerate(i["fix"] if i["status"] in ("warn", "bad") else [], 1):
            out.append(f"    {n}. {step}")
    hist = await app_log.history(problems=True, component=None, q=None, offset=0, limit=40)
    out += ["", "Recent problems (newest first):"]
    if not hist["items"]:
        out.append("    none")
    for e in hist["items"]:
        when = _fmt_time(e["last_at"])
        rep = f" (×{e['count']} since {_fmt_time(e['first_at'])})" if e["count"] > 1 else ""
        comp = app_log.COMPONENTS.get(e["component"], e["component"])
        out.append(f"    {when} {e['level']:<7} [{comp}] {e['message']}{rep}")
    radio = [
        e
        for e in (await app_log.history(problems=False, component="radio", q=None, offset=0, limit=60))[
            "items"
        ]
        if e["event"] == "radio"
    ][:15]
    if radio:
        out += ["", "Radio connection history (newest first):"]
        for e in radio:
            rep = f" (×{e['count']} since {_fmt_time(e['first_at'])})" if e["count"] > 1 else ""
            out.append(f"    {_fmt_time(e['last_at'])} {e['message']}{rep}")
    out += ["", "This report contains no passwords or keys; it does include network addresses and names."]
    return "\n".join(out) + "\n"
