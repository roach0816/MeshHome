#!/usr/bin/env python3
"""`meshhome status` (or `meshcore-home status` before migrating): a diagnostic report for a native MeshHome install.

Run as root (the meshhome command takes care of that). It combines the app's own health
snapshot (<state dir>/diagnostics.json, written every 30 s) with system checks: the service, web
health, radio, database, HTTPS, updates, disk, memory, clock and recent log problems. Each line
is marked ✓ (fine), • (information), ! (warning) or ✗ (problem), and problems come with a
suggested next step.

The report contains no passwords, keys or tokens, so it can be shared when asking for help (it
does include network addresses). Exit status: 0 when no problems were found, 1 otherwise.
Standard library only; must keep working on Python 3.11 (Debian 12).
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

# Installs from before the rename to MeshHome keep the old names until `migrate`.
LEGACY = os.path.isdir("/opt/meshcore-home") and not os.path.isdir("/opt/meshhome")
SERVICE = "meshcore-home" if LEGACY else "meshhome"
PREFIX = f"/opt/{SERVICE}"
ENV_FILE = f"/etc/{SERVICE}/{SERVICE}.env"
STATE_DIR = f"/var/lib/{SERVICE}"
CLI = SERVICE
DB_NAME = "meshcore" if LEGACY else "meshhome"
NGINX_SITE = f"/etc/nginx/sites-available/{SERVICE}"
SNAPSHOT_STALE = 120  # seconds; the app writes every 30

COLOR = (
    sys.stdout.isatty()
    and "NO_COLOR" not in os.environ
    and "--no-color" not in sys.argv
)


def paint(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if COLOR else text


OK, INFO, WARN, BAD = "ok", "info", "warn", "bad"
MARK = {
    OK: paint("32", "✓"),
    INFO: paint("36", "•"),
    WARN: paint("33", "!"),
    BAD: paint("31", "✗"),
}
WIDTH = max(60, min(shutil.get_terminal_size((100, 24)).columns, 120))


# ---- small helpers ---------------------------------------------------------------------------


def run(*cmd: str, timeout: float = 5) -> tuple[int, str]:
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        return p.returncode, p.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""


def read_text(path: str) -> str:
    with open(path) as f:
        return f.read()


def now_text(fmt: str = "%Y-%m-%d %H:%M") -> str:
    return time.strftime(fmt)


def read_env() -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        with open(ENV_FILE) as f:
            for line in f:
                key, sep, value = line.strip().partition("=")
                if sep and not key.startswith("#"):
                    env[key] = value
    except OSError:
        pass
    return env


def read_json(path: str) -> dict | None:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def systemd(unit: str, *props: str) -> dict[str, str]:
    _, out = run(
        "systemctl", "show", unit, "--no-pager", *(f"--property={p}" for p in props)
    )
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def duration(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h {s % 3600 // 60}m"
    return f"{s // 86400}d {s % 86400 // 3600}h"


def ago(epoch: float | None) -> str:
    return f"{duration(time.time() - epoch)} ago" if epoch else "never"


def when(epoch: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch)) if epoch else "—"


def size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def http_get(url: str, timeout: float = 4) -> tuple[int | None, float, str]:
    start = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, (time.monotonic() - start) * 1000, ""
    except urllib.error.HTTPError as e:
        return e.code, (time.monotonic() - start) * 1000, ""
    except (urllib.error.URLError, OSError) as e:
        return None, (time.monotonic() - start) * 1000, str(getattr(e, "reason", e))


def tcp_reachable(host: str, port: int, timeout: float = 3) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, ""
    except OSError as e:
        return False, e.strerror or str(e)


# ---- report ----------------------------------------------------------------------------------


class Report:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.problems = 0
        self.warnings = 0

    def section(self, title: str) -> None:
        self.lines.append("")
        self.lines.append(paint("1", title))

    def row(self, level: str, label: str, text: str, hint: str | None = None) -> None:
        if level == BAD:
            self.problems += 1
        elif level == WARN:
            self.warnings += 1
        indent = 4 + 2 + 18
        avail = WIDTH - indent
        first, *rest = wrap(text, avail) or [""]
        self.lines.append(f"  {MARK[level]} {label:<18}{first}")
        for line in rest:
            self.lines.append(" " * indent + line)
        if hint:
            for i, line in enumerate(wrap(hint, avail - 2)):
                self.lines.append(
                    " " * indent + (paint("2", "→ ") if i == 0 else "  ") + line
                )

    def text(self, line: str) -> None:
        self.lines.append(line)


def wrap(text: str, width: int) -> list[str]:
    out: list[str] = []
    for para in text.split("\n"):
        line = ""
        for word in para.split(" "):
            if line and len(line) + 1 + len(word) > width:
                out.append(line)
                line = word
            else:
                line = f"{line} {word}" if line else word
        out.append(line)
    return out


# ---- checks ----------------------------------------------------------------------------------


def check_app(r: Report, env: dict[str, str], snap: dict | None) -> dict[str, str]:
    r.section("App")
    version = ""
    try:
        with open(f"{PREFIX}/current/VERSION") as f:
            version = f.read().strip()
    except OSError:
        pass
    if version:
        r.row(OK, "Version", f"v{version} (native install, {PREFIX}/current)")
    else:
        r.row(
            BAD,
            "Version",
            "not installed",
            'Install it with install.sh (see the README, "Raspberry Pi or Debian").',
        )

    svc = systemd(
        SERVICE,
        "ActiveState",
        "SubState",
        "ActiveEnterTimestampMonotonic",
        "NRestarts",
        "MemoryCurrent",
        "MainPID",
    )
    active = svc.get("ActiveState") == "active" and svc.get("SubState") == "running"
    if active:
        try:
            uptime = float(read_text("/proc/uptime").split()[0])
            running_for = (
                uptime - int(svc.get("ActiveEnterTimestampMonotonic", "0")) / 1e6
            )
        except (OSError, ValueError):
            running_for = 0
        mem = svc.get("MemoryCurrent", "")
        mem_txt = f" · memory {size(int(mem))}" if mem.isdigit() else ""
        restarts = svc.get("NRestarts", "?")
        if running_for < 30 and restarts.isdigit() and int(restarts) >= 3:
            # Caught between crashes: systemd restarts a failing app every few seconds.
            r.row(
                BAD,
                "Service",
                f"restarting repeatedly (just started again; {restarts} restarts)",
                'The app keeps crashing. The cause is listed under "Recent log problems" below.',
            )
        else:
            r.row(
                OK,
                "Service",
                f"running for {duration(running_for)} · restarts {restarts}{mem_txt}",
            )
    else:
        state = f"{svc.get('ActiveState', 'unknown')} ({svc.get('SubState', '?')})"
        restarts = svc.get("NRestarts", "0")
        if svc.get("SubState") == "auto-restart" or (
            restarts.isdigit() and int(restarts) >= 3
        ):
            state = f"crash-looping (systemd has restarted it {restarts} times)"
        r.row(
            BAD,
            "Service",
            f"not running: {state}",
            f"Start it with: sudo systemctl restart {SERVICE} — if it stops again, the errors are "
            f'listed under "Recent log problems" below (full log: sudo {CLI} logs).',
        )

    port = env.get("PORT", "8080")
    code, ms, err = http_get(f"http://127.0.0.1:{port}/health/live")
    if code == 200:
        rcode, _, _ = http_get(f"http://127.0.0.1:{port}/health/ready")
        if rcode == 200:
            r.row(OK, "Web interface", f"answering on port {port} ({ms:.0f} ms)")
        else:
            r.row(
                BAD,
                "Web interface",
                f"answering on port {port}, but not ready (HTTP {rcode})",
                "The app cannot reach its database; see Database below.",
            )
    elif active:
        r.row(
            BAD,
            "Web interface",
            f"not answering on port {port} ({err or f'HTTP {code}'})",
            f"The service runs but does not answer: sudo systemctl restart {SERVICE}",
        )
    else:
        r.row(BAD, "Web interface", f"not answering on port {port}")

    if snap is None:
        r.row(
            WARN if active else INFO,
            "Health snapshot",
            f"none yet ({STATE_DIR}/diagnostics.json)",
            "The app writes it within 30 s of starting. Radio details below need it."
            if active
            else None,
        )
    else:
        age = time.time() - snap.get("written_at", 0)
        if active and age > SNAPSHOT_STALE:
            r.row(
                BAD,
                "Health snapshot",
                f"last written {duration(age)} ago — the app may be stuck",
                f"Restart it: sudo systemctl restart {SERVICE}",
            )
        elif not active:
            r.row(
                INFO,
                "Health snapshot",
                f"from before the service stopped ({duration(age)} ago)",
            )
        else:
            r.row(OK, "Health snapshot", f"updated {duration(age)} ago")
        if snap.get("setup_complete") is False:
            r.row(
                WARN,
                "First-run setup",
                "not completed yet",
                f"Open the web interface and enter the setup token: sudo {CLI} setup-token",
            )
    return svc


def check_radio(r: Report, snap: dict | None) -> None:
    r.section("Radio")
    if snap is None:
        r.row(INFO, "Connection", "unknown (no health snapshot from the app)")
        return
    radio, cfg = snap.get("radio") or {}, snap.get("radio_config") or {}
    mode, state = cfg.get("mode") or radio.get("mode"), radio.get("state", "?")
    host, port = cfg.get("host") or "", cfg.get("port") or 5000
    if mode == "hat":
        host, port = (
            "",
            5000,
        )  # 127.0.0.1:5000; checked in the Radio HAT section instead of probed
        target = "radio HAT on 127.0.0.1:5000"
    else:
        target = f"{host}:{port}" if host else "no address set"
    name = radio.get("radio_name") or (
        "the radio HAT" if mode == "hat" else "the gateway"
    )

    if radio.get("storage_warning"):
        r.row(
            BAD,
            "Storage",
            radio["storage_warning"],
            "Check disk space below and the Database section.",
        )
    if mode == "none":
        r.row(
            INFO,
            "Connection",
            "no radio selected (mode: none)",
            "Choose one in Settings → Radio connection.",
        )
    elif mode == "simulated":
        r.row(
            INFO,
            "Connection",
            f"simulated radio ({state}) — sample traffic, not a real mesh",
            "When your gateway is ready, choose MeshCore TCP in Settings → Radio connection.",
        )
    elif state == "connected":
        since = radio.get("connected_since")
        r.row(
            OK,
            "Connection",
            (
                f"connected to {name} ({target})"
                if mode == "hat"
                else f"connected to {name} at {target}"
            )
            + (f" for {duration(time.time() - since)}" if since else ""),
        )
    elif state == "paused":
        r.row(
            WARN,
            "Connection",
            f"paused for maintenance ({target})",
            "Resume it in Settings → Radio connection.",
        )
    elif state == "not_configured":
        r.row(
            WARN,
            "Connection",
            "MeshCore TCP is selected but no address is set",
            "Enter the gateway's address in Settings → Radio connection.",
        )
    elif state == "lock_unavailable":
        r.row(
            BAD,
            "Connection",
            "another MeshHome instance using this database owns the radio",
            "Run only one instance per database (stop the other one).",
        )
    elif state == "disabled":
        r.row(
            INFO, "Connection", "radio disabled by configuration (RADIO_ENABLED=false)"
        )
    else:
        detail = radio.get("last_error") or radio.get("detail") or state
        retry = radio.get("next_retry_at")
        extra = (
            f" · retrying in {duration(retry - time.time())}"
            if retry and retry > time.time()
            else ""
        )
        # A first connection attempt is normal; repeated failures are a problem.
        failing = (
            state == "backoff"
            or radio.get("reconnects", 0) > 0
            or bool(radio.get("last_error"))
        )
        r.row(
            BAD if failing else INFO,
            "Connection",
            f"not connected ({state}): {detail}{extra} · reconnects {radio.get('reconnects', 0)}",
        )
        if mode == "hat":
            r.row(
                INFO,
                "Radio HAT",
                "see the Radio HAT section below for the radio service and SPI",
            )
        if host:
            # Only probe while disconnected: a companion radio may serve one client at a time.
            reachable, why = tcp_reachable(host, int(port))
            if reachable:
                r.row(
                    WARN,
                    "Gateway reachable",
                    f"yes, {target} accepts connections",
                    "The network path is fine, so the MeshCore handshake is failing: make sure the gateway runs "
                    "MeshCore companion firmware with TCP (WiFi/Ethernet) enabled, and that no other app is "
                    "connected to it.",
                )
            else:
                r.row(
                    BAD,
                    "Gateway reachable",
                    f"no, {target}: {why}",
                    f"Check that the gateway is powered on and on the network, and that the address in "
                    f"Settings → Radio connection is right (try: ping {host}). If it is on another VLAN, allow "
                    f"this Pi to reach TCP port {port}.",
                )
    if mode != "none" and (
        radio.get("connected_since") or radio.get("received") or radio.get("sent")
    ):
        r.row(
            INFO,
            "Traffic",
            f"received {radio.get('received', 0)} · sent {radio.get('sent', 0)} since the app started"
            f" · last radio activity {ago(radio.get('last_interaction_at'))}",
        )
    db = snap.get("database") or {}
    gap = db.get("open_gap")
    if gap and mode != "none":
        r.row(
            WARN,
            "Collection",
            f"messages are not being collected since {when(gap['started_at'])} ({gap.get('reason', '')})",
            "Messages sent while disconnected may be missed; the gap closes when the radio reconnects.",
        )
    elif db.get("ok") and mode != "none":
        n = db.get("gaps_24h", 0)
        r.row(
            OK if n == 0 else INFO,
            "Collection",
            "no gaps in the last 24 h"
            if n == 0
            else f"{n} gap(s) in the last 24 h (now collecting)",
        )


HAT_SERVICE = f"{SERVICE}-radio"
HAT_DIR = f"/opt/{SERVICE}-radio"


def check_radio_hat(r: Report, snap: dict | None) -> None:
    """Only shown when the radio HAT is set up or selected."""
    mode = ((snap or {}).get("radio_config") or {}).get("mode")
    unit = systemd(
        HAT_SERVICE,
        "LoadState",
        "ActiveState",
        "SubState",
        "NRestarts",
        "ConditionResult",
        "ActiveEnterTimestampMonotonic",
        "IPAddressDeny",
    )
    installed = unit.get("LoadState") not in (None, "", "not-found")
    if not installed and mode != "hat":
        return
    r.section("Radio HAT")
    st = read_json(f"{STATE_DIR}/radio-hat.json") or {}
    model = ""
    try:
        model = read_text("/proc/device-tree/model").replace("\0", "").strip()
    except OSError:
        pass
    product = ""
    try:
        product = read_text("/proc/device-tree/hat/product").replace("\0", "").strip()
    except OSError:
        pass
    r.row(
        INFO,
        "Hardware",
        f"{model or 'unknown computer'} · HAT: {product or 'not reported'}",
    )
    if model and not product:
        r.row(
            WARN,
            "HAT identity",
            "the Pi did not report a HAT at boot",
            "Check that the RAK6421 is seated on all 40 pins, with the radio in IO slot 1 and the antenna attached.",
        )
    if not installed:
        r.row(
            BAD,
            "Software",
            "the radio HAT is selected but its software is not set up",
            f"Set it up: sudo {CLI} radio-hat (or Settings → Radio connection).",
        )
        return

    current = ""
    try:
        current = os.path.basename(
            os.path.realpath(f"{HAT_DIR}/zephcore")
        ).removeprefix("zephcore-")
    except OSError:
        pass
    pinned = st.get("pinned_version")
    text = f"ZephCore {current or '?'}"
    if pinned and current and pinned != current:
        text += f" (this release pins {pinned}: sudo {CLI} update)"
    r.row(OK if current else BAD, "Software", text)

    spi = os.path.exists("/dev/spidev0.0")
    if spi:
        r.row(OK, "SPI", "/dev/spidev0.0 present")
    else:
        r.row(
            BAD,
            "SPI",
            "no SPI device (/dev/spidev0.0)",
            "SPI is off, or the Pi has not restarted since it was turned on. Restart it: sudo reboot",
        )

    active = unit.get("ActiveState") == "active" and unit.get("SubState") == "running"
    restarts = unit.get("NRestarts", "0")
    try:
        up = float(read_text("/proc/uptime").split()[0])
        running_for = up - int(unit.get("ActiveEnterTimestampMonotonic", "0")) / 1e6
    except (OSError, ValueError):
        running_for = 0
    if unit.get("ConditionResult") == "no":
        r.row(
            WARN,
            "Service",
            "waiting: systemd skipped the start because SPI is not available yet",
            "Restart the Pi to finish the setup: sudo reboot",
        )
    elif active and running_for < 30 and restarts.isdigit() and int(restarts) >= 3:
        r.row(
            BAD,
            "Service",
            f"restarting repeatedly ({restarts} restarts)",
            f"The radio software keeps stopping; its errors are listed below. Logs: sudo {CLI} radio-hat logs",
        )
    elif active:
        r.row(
            OK, "Service", f"running for {duration(running_for)} · restarts {restarts}"
        )
    else:
        r.row(
            BAD,
            "Service",
            f"not running: {unit.get('ActiveState', '?')} ({unit.get('SubState', '?')})",
            f"Restart it: sudo {CLI} radio-hat restart — logs: sudo {CLI} radio-hat logs",
        )

    _, listen = run("ss", "-Hltn", "sport = :5000")
    if active:
        if listen:
            limited = "0.0.0.0/0" in unit.get("IPAddressDeny", "") or "any" in unit.get(
                "IPAddressDeny", ""
            )
            r.row(
                OK if limited else WARN,
                "Companion port",
                "listening on 5000 · this Pi only"
                if limited
                else "listening on 5000, open to the network",
                None
                if limited
                else f"Other devices could connect to the radio: reinstall it with sudo {CLI} radio-hat",
            )
        else:
            r.row(
                WARN,
                "Companion port",
                "the service runs but port 5000 is not listening yet",
            )

    _, out = run(
        "journalctl",
        "-u",
        HAT_SERVICE,
        "--since",
        "24 hours ago",
        "--no-pager",
        "-o",
        "cat",
        timeout=15,
    )
    errors: dict[str, int] = {}
    for line in out.splitlines():
        if "<err>" in line or "Failed with result" in line:
            msg = re.sub(r"^\[[^]]*\]\s*", "", line).replace("<err> ", "").strip()
            errors[msg] = errors.get(msg, 0) + 1
    if errors:
        r.text(f"  {MARK[INFO]} radio errors in the last 24 h (most recent):")
        for msg, n in list(errors.items())[-5:]:
            text = msg + (f" (×{n})" if n > 1 else "")
            r.text(
                "      " + (text if len(text) <= WIDTH - 6 else text[: WIDTH - 7] + "…")
            )


def check_database(r: Report, snap: dict | None) -> None:
    r.section("Database")
    rc, _ = run("runuser", "-u", "postgres", "--", "pg_isready", "-q")
    if rc != 0:
        r.row(
            BAD,
            "PostgreSQL",
            "not accepting connections",
            "Start it: sudo systemctl restart postgresql — then: sudo systemctl status postgresql",
        )
    else:
        _, ver = run(
            "runuser", "-u", "postgres", "--", "psql", "-tAc", "SHOW server_version"
        )
        _, dbsize = run(
            "runuser",
            "-u",
            "postgres",
            "--",
            "psql",
            "-tAc",
            f"SELECT pg_size_pretty(pg_database_size('{DB_NAME}'))",
        )
        r.row(
            OK,
            "PostgreSQL",
            f"{(ver.split() or ['?'])[0]} running · database {DB_NAME} {dbsize or '?'}",
        )
    if snap:
        db = snap.get("database") or {}
        if db.get("ok"):
            r.row(
                INFO,
                "Archive",
                f"{db.get('messages', 0):,} messages · {db.get('conversations', 0)} conversations · "
                f"{db.get('contacts', 0)} contacts"
                + (f" · {snap['api_keys']} API key(s)" if snap.get("api_keys") else ""),
            )
        else:
            r.row(
                BAD,
                "App → database",
                db.get("error", "the app cannot query its database"),
                f"Check PostgreSQL above, then restart the app: sudo systemctl restart {SERVICE}",
            )


def check_https(r: Report, env: dict[str, str]) -> None:
    r.section("Network & HTTPS")
    host = env.get("HTTPS_HOST", "")
    enabled = env.get("HTTPS_ENABLED") == "1" or (
        "HTTPS_ENABLED" not in env and bool(host) and os.path.exists(NGINX_SITE)
    )
    port = env.get("PORT", "8080")
    _, ips = run("hostname", "-I")
    lan = [ip for ip in ips.split() if "." in ip][:3]
    if not enabled:
        urls = ", ".join(f"http://{ip}:{port}" for ip in lan) or f"port {port}"
        r.row(
            INFO,
            "Address",
            f"{urls} (plain HTTP)",
            f"Add HTTPS in Settings → Network & HTTPS, or: sudo {CLI} https",
        )
        return
    hport = env.get("HTTPS_PORT", "443") or "443"
    url = f"https://{host}" + ("" if hport == "443" else f":{hport}")
    r.row(INFO, "Address", f"{url}  (this Pi: {', '.join(lan) or '?'})")
    rc, _ = run("systemctl", "is-active", "--quiet", "nginx")
    if rc != 0:
        r.row(
            BAD,
            "nginx",
            "not running, so HTTPS is down",
            "Start it: sudo systemctl restart nginx — check: sudo nginx -t",
        )
    else:
        rc, code = run(
            "curl",
            "-sk",
            "-o",
            "/dev/null",
            "-w",
            "%{http_code}",
            "--max-time",
            "5",
            "--resolve",
            f"{host}:{hport}:127.0.0.1",
            f"https://{host}:{hport}/health/live",
        )
        if code == "200":
            r.row(OK, "nginx", f"running · {url} answers on this Pi")
        else:
            r.row(
                BAD,
                "nginx",
                f"running, but {url} does not answer locally (HTTP {code or 'no response'})",
                "Check: sudo nginx -t, and the app's port above.",
            )
    tls = read_json(f"{STATE_DIR}/tls-status.json")
    if tls and tls.get("not_after"):
        try:
            expires = datetime.fromisoformat(tls["not_after"])
            days = (expires - datetime.now(expires.tzinfo)).days
            level = BAD if days < 0 else WARN if days < 14 else OK
            text = (
                f"expired {-days} days ago"
                if days < 0
                else f"valid until {expires:%Y-%m-%d} ({days} days)"
            )
            r.row(
                level,
                "Certificate",
                f"{text} · {tls.get('issuer', '')}".strip(" ·"),
                None
                if level == OK
                else "Renew now in Settings → Network & HTTPS (Renew now)",
            )
        except ValueError:
            r.row(INFO, "Certificate", f"expires {tls['not_after']}")
    if "tls-test" in env.get("TLS_CERT", ""):
        return  # self-signed test certificate: nothing renews it
    if env.get("ACME_CLIENT") == "lego":
        provider = env.get("ACME_PROVIDER", "?")
        try:
            names = {
                p["id"]: p["name"]
                for p in json.loads(
                    read_text(f"{PREFIX}/current/deploy/native/dns-providers.json")
                )["providers"]
            }
            provider = names.get(provider, provider)
        except (OSError, ValueError, KeyError):
            pass
        r.row(INFO, "DNS validation", f"{provider} (lego)")
        timer = systemd(f"{SERVICE}-acme-renew.timer", "ActiveState")
        last = systemd(
            f"{SERVICE}-acme-renew.service", "Result", "ExecMainExitTimestamp"
        )
        if timer.get("ActiveState") != "active":
            r.row(
                WARN,
                "Renewal",
                "the renewal timer is not active, so the certificate will not renew automatically",
                f"Re-apply the HTTPS settings in Settings → Network & HTTPS, or: sudo {CLI} https",
            )
        elif last.get("Result") not in (None, "", "success"):
            r.row(
                WARN,
                "Renewal",
                f"the last renewal check failed ({last.get('ExecMainExitTimestamp') or 'recently'})",
                f"Check the DNS provider credentials. Details: sudo journalctl -u {SERVICE}-acme-renew",
            )
        else:
            r.row(
                OK,
                "Renewal",
                "checked twice a day by lego; renews about 30 days before expiry",
            )
        if not os.path.exists(f"/opt/{SERVICE}-acme/lego"):
            r.row(
                INFO,
                "Renewal client",
                "lego isn't downloaded yet; the next renewal check fetches it (checksum verified)",
            )
        return
    rc, _ = run("systemctl", "is-active", "--quiet", "certbot.timer")
    r.row(
        INFO,
        "DNS validation",
        "Cloudflare (certbot, from before v0.7.4)",
        "Saving the HTTPS settings once moves renewals to lego, reusing the saved token.",
    )
    if rc != 0:
        r.row(
            WARN,
            "Renewal",
            "certbot.timer is not active, so the certificate will not renew automatically",
            "Enable it: sudo systemctl enable --now certbot.timer",
        )


def check_updates(r: Report, snap: dict | None) -> None:
    r.section("Updates")
    u = (snap or {}).get("updates") or {}
    current = (snap or {}).get("version", "?")
    if not u:
        r.row(INFO, "Release", f"v{current}")
    elif not u.get("checks_enabled"):
        r.row(INFO, "Release", f"v{current} · update checks are turned off")
    elif u.get("error"):
        r.row(
            WARN,
            "Release",
            f"v{current} · could not check for updates: {u['error']}",
            "Check this Pi's internet access and DNS.",
        )
    elif u.get("update_available"):
        r.row(
            INFO,
            "Release",
            f"v{current} · v{u.get('latest_version')} is available",
            f"Install it from Settings → Software updates, or: sudo {CLI} update",
        )
    else:
        r.row(
            OK,
            "Release",
            f"v{current} is the latest ("
            + (
                f"checked {ago(u['checked_at'])}"
                if u.get("checked_at")
                else "not checked yet"
            )
            + ")",
        )
    st = read_json(f"{STATE_DIR}/update-status.json")
    if st:
        state, msg = st.get("state"), st.get("message") or st.get("state")
        level = (
            OK
            if state == "done"
            else WARN
            if state in ("failed", "rolled_back")
            else INFO
        )
        r.row(
            level,
            "Last update",
            f"{msg} ({when(st.get('updated_at'))})",
            f"Details: sudo cat /var/log/{SERVICE}-install.log"
            if level == WARN
            else None,
        )
    _, conf = run("apt-config", "dump")
    on = 'APT::Periodic::Unattended-Upgrade "1";' in conf
    r.row(
        INFO,
        "Security updates",
        "installed automatically (unattended-upgrades)" if on else "manual",
        None
        if on
        else f"Turn on automatic security updates: sudo {CLI} security-updates",
    )


def check_system(r: Report) -> None:
    r.section("System")
    osname = platform.platform()
    try:
        with open("/etc/os-release") as f:
            osname = (
                dict(line.rstrip().split("=", 1) for line in f if "=" in line)
                .get("PRETTY_NAME", osname)
                .strip('"')
            )
    except OSError:
        pass
    try:
        up = float(read_text("/proc/uptime").split()[0])
        load = " ".join(read_text("/proc/loadavg").split()[:3])
    except (OSError, ValueError):
        up, load = 0, "?"
    r.row(
        INFO,
        "Host",
        f"{socket.gethostname()} · {osname} · {platform.machine()} · up {duration(up)} · load {load}",
    )

    for path in sorted({"/", STATE_DIR if os.path.isdir(STATE_DIR) else "/"}):
        du = shutil.disk_usage(path)
        pct = du.used / du.total * 100 if du.total else 0
        level = BAD if du.free < 1024**3 else WARN if pct > 90 else OK
        r.row(
            level,
            f"Disk {path}"[:17],
            f"{size(du.free)} free of {size(du.total)} ({pct:.0f}% used)",
            "Free up space: the database and backups need room to grow."
            if level != OK
            else None,
        )
        if path == "/":
            break  # STATE_DIR is normally on the same filesystem

    try:
        mem = {
            k: int(v.split()[0]) * 1024
            for k, v in (
                line.split(":", 1) for line in read_text("/proc/meminfo").splitlines()
            )
        }
        avail, total = mem.get("MemAvailable", 0), mem.get("MemTotal", 0)
        r.row(
            WARN if avail < 150 * 1024**2 else OK,
            "Memory",
            f"{size(avail)} available of {size(total)}",
        )
    except (OSError, ValueError):
        pass

    _, sync = run("timedatectl", "show", "--property=NTPSynchronized", "--value")
    _, tz = run("timedatectl", "show", "--property=Timezone", "--value")
    if sync == "yes":
        r.row(OK, "Clock", f"synchronized · {now_text()} {tz}".rstrip())
    elif sync == "no":
        r.row(
            WARN,
            "Clock",
            f"not synchronized · {now_text()} {tz}",
            "Message times and the radio clock depend on it. Check internet access and: timedatectl status",
        )

    try:
        temp = int(read_text("/sys/class/thermal/thermal_zone0/temp")) / 1000
        r.row(
            WARN if temp >= 80 else OK,
            "Temperature",
            f"{temp:.0f} °C",
            "Improve cooling: the Pi slows down when it is this hot."
            if temp >= 80
            else None,
        )
    except (OSError, ValueError):
        pass
    if shutil.which("vcgencmd"):
        _, out = run("vcgencmd", "get_throttled")
        m = re.search(r"0x([0-9a-fA-F]+)", out)
        if m:
            flags = int(m.group(1), 16)
            if flags == 0:
                r.row(OK, "Power", "no under-voltage or throttling detected")
            else:
                now = "now" if flags & 0xF else "since boot"
                r.row(
                    WARN,
                    "Power",
                    f"under-voltage or throttling detected {now} (get_throttled=0x{flags:x})",
                    "Use the official Pi power supply (or a PoE HAT rated for the Pi); low voltage causes crashes and data errors.",
                )


LOG_PROBLEM = re.compile(
    # App warnings/errors that have a message, the final line of a Python traceback, and
    # systemd's own failure notes. (The first-run setup banner is an empty WARNING: skipped.)
    r" (WARNING|ERROR|CRITICAL) \S+: \S|: [A-Za-z_.]*(Error|Exception): |Failed with result|Main process exited"
)


def check_logs(r: Report) -> None:
    r.section("Recent log problems (last 24 h)")
    _, out = run(
        "journalctl",
        "-u",
        SERVICE,
        "--since",
        "24 hours ago",
        "--no-pager",
        "-o",
        "short-iso",
        timeout=15,
    )
    hits = [line for line in out.splitlines() if LOG_PROBLEM.search(line)]
    if not hits:
        r.row(OK, "Log", "no warnings or errors")
        return
    # Group repeats (a crash loop logs the same lines over and over): message → (count, last time).
    groups: dict[str, list] = {}
    for line in hits:
        # "2026-10-04T09:01:12+0000 host python[123]: <message>" → ("10-04 09:01:12", "<message>")
        m = re.match(r"\d{4}-(\d\d-\d\dT\d\d:\d\d:\d\d)\S*\s+\S+\s+[^:]+:\s?(.*)", line)
        stamp, msg = (m.group(1).replace("T", " "), m.group(2)) if m else ("", line)
        msg = re.sub(
            r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+ ", "", msg
        )  # the app's own timestamp
        entry = groups.pop(msg, [0, ""])
        groups[msg] = [
            entry[0] + 1,
            stamp,
        ]  # re-inserted, so the order follows the latest time
    r.text(
        f"  {MARK[INFO]} {len(hits)} warning/error line(s), {len(groups)} distinct; the most recent:"
    )
    for msg, (count, stamp) in list(groups.items())[-8:]:
        times = f" (×{count})" if count > 1 else ""
        text = f"{stamp}  {msg}{times}"
        r.text("      " + (text if len(text) <= WIDTH - 6 else text[: WIDTH - 7] + "…"))
    r.text(f"      {paint('2', f'Full log: sudo {CLI} logs')}")


def main() -> int:
    if "-h" in sys.argv or "--help" in sys.argv:
        print(__doc__.strip())
        return 0
    if os.geteuid() != 0:
        print(f"Run as root: sudo {CLI} status", file=sys.stderr)
        return 2
    env = read_env()
    snap = read_json(f"{STATE_DIR}/diagnostics.json")
    r = Report()
    r.text(
        paint("1", "MeshHome status")
        + f" · {socket.gethostname()} · {now_text('%Y-%m-%d %H:%M:%S')}"
    )
    check_app(r, env, snap)
    check_radio(r, snap)
    check_radio_hat(r, snap)
    check_database(r, snap)
    check_https(r, env)
    check_updates(r, snap)
    if LEGACY:
        r.row(
            INFO,
            "Install names",
            "from before the rename to MeshHome (meshcore-home); they keep working",
            "To move to the new names: sudo meshcore-home migrate",
        )
    check_system(r)
    check_logs(r)
    r.text("")
    if r.problems:
        summary = paint("31", f"✗ {r.problems} problem(s)") + (
            f", {r.warnings} warning(s)" if r.warnings else ""
        )
    elif r.warnings:
        summary = paint("33", f"! {r.warnings} warning(s), no problems")
    else:
        summary = paint("32", "✓ Everything looks healthy")
    r.text(paint("1", "Summary: ") + summary)
    r.text(
        paint(
            "2",
            "This report contains no passwords, keys or tokens, but it does show network addresses.",
        )
    )
    r.text(paint("2", "Share it when asking for help."))
    print("\n".join(r.lines))
    return 1 if r.problems else 0


if __name__ == "__main__":
    sys.exit(main())
