"""Backup and restore of a whole installation.

A backup is one passphrase-encrypted file (see backup_crypto) holding a gzipped tar:

    manifest.json      format, app version, database schema revision, install kind, counts,
                       and a SHA-256 for every other member
    db/<table>.jsonl   every row of every table except sign-in sessions (one JSON object per line)
    system.tar.gz      native installs only: network/HTTPS settings, the HTTPS certificate and
                       key, the DNS provider credentials and the radio HAT's data, packed by the
                       installer's root helper (the app itself cannot read those files)
    radio.json         a TCP radio connected when the backup was made: its identity key,
                       settings, channels and contacts (format 2; see radio_backup). Restoring
                       the backup doesn't write it to a radio; that is a separate step.

Restoring replaces all data. Migrations only ever add tables and columns, so a backup from an
older MeshHome loads into a newer one (new columns take their defaults); a backup from a
newer one is refused. Before anything changes, the backup is decrypted and checked, and what will
and won't be restored here is listed (inspect()).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import gzip
import hashlib
import io
import json
import logging
import os
import re
import shutil
import tarfile
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import DateTime, LargeBinary, Table, Uuid, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.config import APP_VERSION, get_settings
from app.models import Base
from app.services import app_settings, backup_crypto, radio_backup

log = logging.getLogger(__name__)

FORMAT = "meshhome-backup"
# Backups made before the rename to MeshHome.
LEGACY_FORMATS = ("meshcore-home-backup",)
# 2: may hold radio.json. A backup without it is still written as 1, for older MeshHomes.
FORMAT_VERSION = 2
# Sign-ins, and the browsers' push subscriptions that belong to them, are not carried over
# (push_devices: unused since 0.12.0).
SKIP_TABLES = {"sessions", "web_push_subscriptions", "push_devices"}
INSERT_BATCH = 500
MAX_MEMBER = 2 * 1024**3
NAME = re.compile(r"^(meshhome|meshcore-home)-[0-9]{8}-[0-9]{6}(-[a-z0-9-]{1,40})?\.(mhb|mchb)$")
_MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


# ---- where backup files live ------------------------------------------------------------------


def backup_dir() -> Path:
    """Native installs keep backups in the state directory; containers have no persistent disk
    of their own, so their backups are kept briefly in a temporary folder for download."""
    d = get_settings().state_dir
    path = Path(d) / "user-backups" if d else Path(tempfile.gettempdir()) / "meshhome-backups"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def persistent() -> bool:
    return bool(get_settings().state_dir)


def staging_dir() -> Path:
    d = get_settings().state_dir
    path = Path(d) / "restore-staging" if d else Path(tempfile.gettempdir()) / "meshhome-restore"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def list_backups() -> list[dict[str, Any]]:
    out = []
    files = [*backup_dir().glob("*.mhb"), *backup_dir().glob("*.mchb")]
    # Newest first, by the time stamp in the name (old and new names mixed).
    for p in sorted(files, key=lambda f: re.sub(r"^(meshhome|meshcore-home)-", "", f.name), reverse=True):
        if NAME.match(p.name):
            st = p.stat()
            out.append(
                {"name": p.name, "size": st.st_size, "created_at": datetime.fromtimestamp(st.st_mtime, UTC)}
            )
    return out


def backup_path(name: str) -> Path:
    if not NAME.match(name):
        raise FileNotFoundError(name)
    p = backup_dir() / name
    if not p.is_file():
        raise FileNotFoundError(name)
    return p


def prune_temporary(max_age: float = 3600) -> None:
    """Containers: forget downloaded backups and abandoned uploads after an hour."""
    dirs = [staging_dir()] + ([] if persistent() else [backup_dir()])
    for d in dirs:
        for p in d.iterdir():
            with contextlib.suppress(OSError):
                if time.time() - p.stat().st_mtime > max_age:
                    shutil.rmtree(p) if p.is_dir() else p.unlink()


# ---- database rows <-> JSON -------------------------------------------------------------------


def _json_value(v: Any) -> Any:
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, datetime | date):
        return v.isoformat()
    if isinstance(v, bytes | bytearray | memoryview):
        return {"$b64": base64.b64encode(bytes(v)).decode()}
    if isinstance(v, Decimal):
        return str(v)
    return v


def _db_value(table: Table, column: str, v: Any) -> Any:
    if v is None:
        return None
    col_type = table.c[column].type
    if isinstance(col_type, Uuid):
        return uuid.UUID(v)
    if isinstance(col_type, DateTime):
        return datetime.fromisoformat(v)
    if isinstance(col_type, LargeBinary) and isinstance(v, dict):
        return base64.b64decode(v["$b64"])
    return v


def tables() -> list[Table]:
    """App tables in dependency order (parents before children)."""
    return [t for t in Base.metadata.sorted_tables if t.name not in SKIP_TABLES]


async def schema_revision(s: AsyncSession) -> str | None:
    try:
        return (await s.execute(text("SELECT version_num FROM alembic_version"))).scalar()
    except Exception:  # noqa: BLE001 - e.g. a test database created without alembic
        return None


def known_revisions() -> list[str]:
    """Revisions this version of MeshHome knows, oldest first."""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory(str(_MIGRATIONS))
    return [r.revision for r in reversed(list(script.walk_revisions()))]


# ---- making a backup --------------------------------------------------------------------------


async def write_database(tar: tarfile.TarFile, digests: dict[str, str], counts: dict[str, int]) -> str | None:
    async with db.session_factory()() as s:
        revision = await schema_revision(s)
        for table in tables():
            buf = io.BytesIO()
            n = 0
            result = await s.stream(select(table))
            async for row in result.mappings():
                line = {k: _json_value(v) for k, v in row.items()}
                buf.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
                n += 1
            _add(tar, f"db/{table.name}.jsonl", buf.getvalue(), digests)
            counts[table.name] = n
    return revision


def _add(tar: tarfile.TarFile, name: str, data: bytes, digests: dict[str, str]) -> None:
    info = tarfile.TarInfo(name)
    info.size, info.mtime, info.mode = len(data), int(time.time()), 0o600
    tar.addfile(info, io.BytesIO(data))
    digests[name] = hashlib.sha256(data).hexdigest()


async def create(
    passphrase: str,
    *,
    system_archive: Path | None = None,
    radio: radio_backup.Snapshot | None = None,
    label: str = "",
) -> Path:
    """Write an encrypted backup into backup_dir() and return its path."""
    if len(passphrase) < backup_crypto.MIN_PASSPHRASE:
        raise backup_crypto.BackupError(
            f"Use a passphrase of at least {backup_crypto.MIN_PASSPHRASE} characters."
        )
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    name = f"meshhome-{stamp}{'-' + label if label else ''}.mhb"
    target = backup_dir() / name
    digests: dict[str, str] = {}
    counts: dict[str, int] = {}
    with tempfile.TemporaryDirectory(dir=staging_dir()) as tmp:
        plain = Path(tmp) / "backup.tar.gz"
        with tarfile.open(plain, "w:gz") as tar:
            revision = await write_database(tar, digests, counts)
            if system_archive is not None:
                _add(tar, "system.tar.gz", system_archive.read_bytes(), digests)
            if radio is not None:
                _add(tar, "radio.json", radio.model_dump_json().encode(), digests)
            async with db.session_factory()() as s:
                home = (await app_settings.get_installation(s)).home_name
            manifest = {
                "format": FORMAT,
                "format_version": FORMAT_VERSION if radio is not None else 1,
                "app_version": APP_VERSION,
                "schema_revision": revision,
                "install_kind": get_settings().install_kind,
                "home_name": home,
                "created_at": datetime.now(UTC).isoformat(),
                "counts": counts,
                "system": system_archive is not None,
                "radio": radio is not None,
                "sha256": digests,
            }
            _add(tar, "manifest.json", json.dumps(manifest, indent=1).encode(), {})
        await asyncio.to_thread(_encrypt_file, plain, target, passphrase)
    return target


def _encrypt_file(plain: Path, target: Path, passphrase: str) -> None:
    """Encrypt in a worker thread: scrypt and AES over megabytes would stall the event loop."""
    partial = target.with_suffix(".partial")
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with open(plain, "rb") as src, os.fdopen(fd, "wb") as dst:
        backup_crypto.encrypt(src, dst, passphrase)
    os.replace(partial, target)


# ---- reading a backup -------------------------------------------------------------------------


@dataclass
class Opened:
    """A decrypted, checked backup in a private temporary folder."""

    folder: Path
    manifest: dict[str, Any]
    members: dict[str, Path] = field(default_factory=dict)

    def close(self) -> None:
        shutil.rmtree(self.folder, ignore_errors=True)


def open_backup(path: Path, passphrase: str) -> Opened:
    folder = Path(tempfile.mkdtemp(dir=staging_dir()))
    try:
        plain = folder / "backup.tar.gz"
        with open(path, "rb") as src, open(plain, "wb") as dst:
            backup_crypto.decrypt(src, dst, passphrase)
        members: dict[str, Path] = {}
        try:
            with tarfile.open(plain, "r:gz") as tar:
                for m in tar:
                    if (
                        not m.isfile()
                        or m.size > MAX_MEMBER
                        or not re.fullmatch(
                            r"(db/[a-z_]+\.jsonl|manifest\.json|system\.tar\.gz|radio\.json)", m.name
                        )
                    ):
                        raise backup_crypto.BackupError("The backup contains an unexpected file.")
                    out = folder / m.name.replace("/", "__")
                    with tar.extractfile(m) as f, open(out, "wb") as o:  # type: ignore[union-attr]
                        shutil.copyfileobj(f, o)
                    members[m.name] = out
        except (tarfile.TarError, gzip.BadGzipFile, EOFError) as exc:
            raise backup_crypto.BackupError("The backup's contents are damaged.") from exc
        plain.unlink()
        if "manifest.json" not in members:
            raise backup_crypto.BackupError("The backup has no manifest.")
        manifest = json.loads(members["manifest.json"].read_text())
        if manifest.get("format") not in (FORMAT, *LEGACY_FORMATS):
            raise backup_crypto.BackupError("This is not a MeshHome backup.")
        if int(manifest.get("format_version") or 0) > FORMAT_VERSION:
            raise backup_crypto.BackupError(
                f"This backup was made by a newer MeshHome (v{manifest.get('app_version')}). Update first."
            )
        for name, digest in (manifest.get("sha256") or {}).items():
            p = members.get(name)
            if p is None or hashlib.sha256(p.read_bytes()).hexdigest() != digest:
                raise backup_crypto.BackupError(f"Part of the backup is missing or damaged ({name}).")
        return Opened(folder, manifest, members)
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise


def _system_summary(opened: Opened) -> dict[str, Any] | None:
    """The non-secret description the root helper put in system.tar.gz (system-manifest.json)."""
    p = opened.members.get("system.tar.gz")
    if p is None:
        return None
    try:
        with tarfile.open(p, "r:gz") as tar:
            # The installer packs with "tar -C dir .", so names start with "./".
            for name in ("./system-manifest.json", "system-manifest.json"):
                try:
                    f = tar.extractfile(name)
                except KeyError:
                    continue
                return json.loads(f.read()) if f else {}
    except (tarfile.TarError, ValueError, OSError):
        pass
    return {}


def inspect(opened: Opened) -> dict[str, Any]:
    """What restoring this backup here will and won't do."""
    m = opened.manifest
    here = get_settings().install_kind
    errors: list[str] = []
    restored: list[str] = []
    not_restored: list[str] = []
    notes: list[str] = []

    revisions = known_revisions()
    rev = m.get("schema_revision")
    if rev and rev not in revisions:
        errors.append(
            f"This backup was made by a newer MeshHome (v{m.get('app_version')}). Update this installation first."
        )
    current = {t.name: t for t in Base.metadata.sorted_tables}
    for name in m.get("counts") or {}:
        if name not in current and not errors:
            errors.append(
                f"This backup holds data this version doesn't know ({name}). Update this installation first."
            )

    counts = m.get("counts") or {}
    restored.append(
        f"The owner account and API keys ({counts.get('users', 0)} account{'s' if counts.get('users', 0) != 1 else ''}, "
        f"{counts.get('api_keys', 0)} API key{'s' if counts.get('api_keys', 0) != 1 else ''}); everyone signs in again"
    )
    restored.append(
        f"The message archive: {counts.get('messages', 0)} messages in {counts.get('conversations', 0)} conversations, "
        f"{counts.get('contacts', 0)} contacts"
    )
    restored.append(
        "All settings: radio connection, map, notifications, bot, weather station, and repeater-admin history"
    )

    radio = _settings_row(opened, app_settings.RADIO_KEY) or {}
    mode = radio.get("mode")
    system = _system_summary(opened)
    if here == "native":
        if system is not None:
            if system.get("https"):
                restored.append(
                    f"HTTPS for {system.get('https_host')} with its certificate"
                    + (f" (valid until {system['cert_not_after']})" if system.get("cert_not_after") else "")
                    + (
                        f" and {system.get('provider_name')} credentials for renewals"
                        if system.get("provider_name")
                        else ""
                    )
                )
                notes.append(f"Point {system.get('https_host')} at this Pi's address if it moved.")
            else:
                restored.append("Network settings (HTTPS was off)")
            if system.get("hat_data"):
                restored.append("The radio HAT's identity, contacts and channels (ZephCore)")
                notes.append(
                    "If this Pi has no radio HAT set up yet, set it up in Settings; it will use the restored identity."
                )
        else:
            not_restored.append(
                "Network and HTTPS settings: this backup came from a container, so this Pi keeps its own."
            )
    else:  # container
        if system is not None:
            not_restored.append(
                "Network and HTTPS settings, the certificate and DNS credentials: in a container, HTTPS is handled "
                "by your cluster's Ingress."
            )
            if system.get("hat_data"):
                not_restored.append(
                    "The radio HAT's identity: radio HATs only work on a Raspberry Pi install."
                )
        if mode == "hat":
            notes.append(
                "The radio connection was the radio HAT, which isn't available here; choose a radio in Settings after restoring."
            )
    if mode == "tcp" and radio.get("host"):
        notes.append(
            f"The radio connection points to {radio['host']}:{radio.get('port', 5000)}; change it in Settings if that address differs here."
        )
    snapshot = radio_snapshot(opened)
    if snapshot is not None:
        notes.append(
            f"This backup also holds a snapshot of the radio {radio_backup.describe(snapshot)}. Restoring the "
            "backup doesn't change any radio; to put it back on a radio, use Restore a radio from a backup."
        )
    not_restored.append("Sign-in sessions (everyone signs in again with the restored account)")
    return {
        "created_at": m.get("created_at"),
        "app_version": m.get("app_version"),
        "install_kind": m.get("install_kind"),
        "home_name": m.get("home_name"),
        "counts": counts,
        "can_restore": not errors,
        "errors": errors,
        "restored": restored,
        "not_restored": not_restored,
        "notes": notes,
        "system_restore": here == "native" and system is not None,
        "radio": None if snapshot is None else radio_backup.summary(snapshot),
    }


def radio_snapshot(opened: Opened) -> radio_backup.Snapshot | None:
    """The radio snapshot in a backup, if it has one. BackupError if it's damaged."""
    p = opened.members.get("radio.json")
    if p is None:
        return None
    try:
        return radio_backup.parse(p.read_bytes())
    except ValueError as exc:
        raise backup_crypto.BackupError(str(exc)) from None


def _settings_row(opened: Opened, key: str) -> dict[str, Any] | None:
    p = opened.members.get("db/app_settings.jsonl")
    if p is None:
        return None
    with open(p, "rb") as f:
        for line in f:
            row = json.loads(line)
            if row.get("key") == key and isinstance(row.get("value"), dict):
                return row["value"]
    return None


# ---- restoring the database --------------------------------------------------------------------


async def restore_database(opened: Opened) -> None:
    """Replace every table's rows with the backup's, in one transaction."""
    here = get_settings().install_kind
    current = tables()
    async with db.session_factory()() as s:
        names = ", ".join(t.name for t in current)
        await s.execute(text("SET LOCAL lock_timeout = '30s'"))  # fail rather than hang behind a stuck query
        await s.execute(text(f"TRUNCATE {names} CASCADE"))  # noqa: S608 - names from our own metadata
        for table in current:
            p = opened.members.get(f"db/{table.name}.jsonl")
            if p is None:
                continue
            batch: list[dict[str, Any]] = []
            for line in (await asyncio.to_thread(p.read_bytes)).splitlines():
                if line:
                    row = json.loads(line)
                    unknown = set(row) - set(table.c.keys())
                    if unknown:
                        raise backup_crypto.BackupError(
                            f"This backup has data this version doesn't know ({table.name})."
                        )
                    if (
                        table.name == "app_settings"
                        and row.get("key") == app_settings.RADIO_KEY
                        and here != "native"
                    ):
                        if (row.get("value") or {}).get("mode") == "hat":
                            row["value"] = {**row["value"], "mode": "none"}
                    batch.append({k: _db_value(table, k, v) for k, v in row.items()})
                    if len(batch) >= INSERT_BATCH:
                        await s.execute(insert(table), batch)
                        batch = []
            if batch:
                await s.execute(insert(table), batch)
        await s.execute(
            text(
                "SELECT setval(pg_get_serial_sequence('audit_events', 'id'), COALESCE((SELECT max(id) FROM audit_events), 0) + 1, false)"
            )
        )
        await s.commit()
