"""The optional radio HAT on a native Raspberry Pi install (RAK6421 + RAK13300, run by ZephCore).

The root helper (`install.sh`) installs ZephCore as the `meshhome-radio` service and
reports what it did in <state dir>/radio-hat.json. This module combines that file with what any
user can read live: the Pi model, the HAT's EEPROM identity, the SPI device and the service
state. The app itself never changes system configuration; setup, removal, restart and the Pi
restart go through the same request file as network settings (see system_config).

ZephCore serves the MeshCore companion protocol on 127.0.0.1:5000, so radio mode "hat" is the
TCP adapter with a fixed local address.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from app.config import get_settings

HOST = "127.0.0.1"
PORT = 5000
# The radio service's name: meshhome-radio, or meshcore-home-radio on an install from before the
# rename to MeshHome that hasn't been migrated yet (its state directory still has the old name).
SERVICE = (
    "meshcore-home-radio"
    if get_settings().state_dir.rstrip("/").endswith("meshcore-home")
    else "meshhome-radio"
)
STATUS_FILE = "radio-hat.json"
SPIDEV = "/dev/spidev0.0"
# The app runs with PrivateDevices=true, so its /dev never has the SPI device; sysfs still shows it.
SPIDEV_SYSFS = "/sys/class/spidev/spidev0.0"
ACTIVE_STATES = {"checking", "installing", "rebooting"}
STALE_AFTER = 15 * 60


def native() -> bool:
    s = get_settings()
    return s.install_kind == "native" and bool(s.state_dir)


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text().replace("\0", "").strip() or None
    except OSError:
        return None


def _test_mode() -> bool:
    # Container tests only (same knob as install.sh): no SPI device or HAT EEPROM exists there.
    return bool(os.environ.get("MESHHOME_HAT_TEST") or os.environ.get("MESHCORE_HOME_HAT_TEST"))


def pi_model() -> str | None:
    return (
        os.environ.get("MESHHOME_PI_MODEL")
        or os.environ.get("MESHCORE_HOME_PI_MODEL")
        or _read("/proc/device-tree/model")
    )


def board(model: str | None) -> str | None:
    if not model:
        return None
    if "Raspberry Pi 5" in model or "Compute Module 5" in model:
        return "pi5"
    if "Raspberry Pi 4" in model or "Compute Module 4" in model:
        return "pi4"
    return None


def hat_product() -> str | None:
    product, vendor = _read("/proc/device-tree/hat/product"), _read("/proc/device-tree/hat/vendor")
    return f"{product} ({vendor})" if product and vendor else product


# Release layout: <release>/app and <release>/deploy; repository layout: <repo>/backend/app.
_HERE = Path(__file__).resolve()
LOCK_CANDIDATES = (
    _HERE.parents[2] / "deploy" / "native" / "zephcore.lock",
    _HERE.parents[3] / "deploy" / "native" / "zephcore.lock",
)


def pinned() -> dict[str, str]:
    """The ZephCore release this MeshHome release installs (deploy/native/zephcore.lock)."""
    out: dict[str, str] = {}
    lock = next((p for p in LOCK_CANDIDATES if p.is_file()), None)
    if lock is None:
        return out
    try:
        for line in lock.read_text().splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and not key.startswith("#"):
                out[key] = value
    except OSError:
        pass
    return out


def _version(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split(".") if x.isdigit())


def glibc_version() -> str | None:
    try:
        return os.confstr("CS_GNU_LIBC_VERSION").split()[-1]  # "glibc 2.41"
    except (ValueError, OSError, AttributeError, IndexError):
        return None


def boot_time() -> float | None:
    """When this machine last started (seconds since the epoch), from /proc/stat."""
    try:
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def status_file() -> dict[str, Any] | None:
    d = get_settings().state_dir
    if not d:
        return None
    try:
        data = json.loads((Path(d) / STATUS_FILE).read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


async def service_state() -> dict[str, Any]:
    """systemd's view of the radio service (readable without root)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl",
            "show",
            SERVICE,
            "--property=LoadState,ActiveState,SubState,NRestarts,ConditionResult",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), 5)
    except (OSError, TimeoutError):
        return {}
    props = dict(line.split("=", 1) for line in out.decode().splitlines() if "=" in line)
    if props.get("LoadState") in (None, "not-found"):
        return {"installed": False}
    return {
        "installed": True,
        "active": props.get("ActiveState") == "active",
        "state": f"{props.get('ActiveState', '?')} ({props.get('SubState', '?')})",
        "restarts": int(props["NRestarts"]) if props.get("NRestarts", "").isdigit() else None,
        # "no" when systemd skipped the start because the SPI device does not exist yet.
        "condition_met": props.get("ConditionResult") != "no",
    }


async def info() -> dict[str, Any]:
    model = pi_model()
    st = status_file() or {}
    svc = await service_state() if native() else {}
    spi = os.path.exists(SPIDEV_SYSFS) or os.path.exists(SPIDEV) or _test_mode()
    lock = pinned()
    glibc, min_glibc = glibc_version(), lock.get("ZEPHCORE_MIN_GLIBC")
    if not native():
        reason = "The radio HAT is available on Raspberry Pi installs made with install.sh."
    elif not board(model):
        reason = "The RAK6421 radio HAT works on a Raspberry Pi 4 or 5."
    elif glibc and min_glibc and _version(glibc) < _version(min_glibc):
        reason = (
            "The radio software (ZephCore) needs Raspberry Pi OS 13 “Trixie” or Debian 13 "
            f"(glibc {min_glibc} or newer); this Pi has glibc {glibc}. Re-image the SD card with the "
            "current 64-bit Raspberry Pi OS, then install MeshHome again."
        )
    elif st.get("state") == "unsupported":
        reason = st.get("message")
    else:
        reason = None
    updated = float(st.get("updated_at") or 0)
    busy = st.get("state") in ACTIVE_STATES and time.time() - updated < STALE_AFTER
    if busy and st.get("state") == "rebooting":
        # Nothing rewrites the file after the restart: once the Pi has booted since, it is done.
        booted = boot_time()
        busy = booted is None or booted < updated
    installed = bool(svc.get("installed"))
    if busy:
        phase = st.get("state")
    elif not installed:
        phase = "absent"
    elif svc.get("active"):
        phase = "ready"
    elif not spi or svc.get("condition_met") is False or st.get("state") == "needs_reboot":
        phase = "needs_reboot"
    else:
        phase = "stopped"
    return {
        "available": reason is None,
        "unavailable_reason": reason,
        "phase": phase,  # absent | checking | installing | rebooting | needs_reboot | ready | stopped
        "model": model,
        "board": board(model),
        "hat_product": hat_product(),
        "spi_device": spi,
        "service": svc,
        "installed_version": st.get("installed_version"),
        "pinned_version": st.get("pinned_version") or lock.get("ZEPHCORE_VERSION"),
        "last_message": st.get("message"),
        "last_state": st.get("state"),
        "updated_at": st.get("updated_at"),
        "host": HOST,
        "port": PORT,
    }
