"""Bot apps: the catalog, and the apps installed on this MeshHome (Settings → Bot → Apps).

The catalog is a public repository (BOT_APPS_CATALOG_URL): catalog.json lists each app with the
SHA-256 of its recipe, stored beside it. Apps are added, updated and removed there without a
MeshHome release. A copy of the catalog ships with each release (app/bot_apps/) for when the
online one can't be reached, and to install the Ecowitt app for servers that set up a weather
station before apps existed.

Installed apps live in the settings table (key "bot_apps"), each with the exact recipe it runs and
the owner's settings for it. A newer catalog version is only offered: it runs after the owner
updates the app. Recipes are data run by app/services/bot_recipes.py, never code.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import APP_VERSION, get_settings
from app.services import app_settings, bot_recipes, push_inbox
from app.services.bot_recipes import Recipe, RecipeError

log = logging.getLogger(__name__)

BUNDLED = Path(__file__).resolve().parent.parent / "bot_apps"
STORE_KEY = "bot_apps"
CATALOG_TTL = 6 * 3600
TIMEOUT = 10.0
MAX_BYTES = 256 * 1024
# Push upload addresses (/push/<token>): always 32 URL-safe characters (192 bits). The fixed length
# matters: some devices glue their readings straight onto the path, with no "?" in between.
TOKEN_LENGTH = 32


class AppError(Exception):
    """Something the owner should be told (installing, updating, settings)."""


def _version(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split(".") if x.isdigit())


# ---- the catalog ---------------------------------------------------------------------------------


class CatalogApp:
    def __init__(self, entry: dict[str, Any], recipe: Recipe, raw: bytes) -> None:
        self.entry, self.recipe, self.raw = entry, recipe, raw
        self.min_meshhome = str(entry.get("min_meshhome") or "")

    @property
    def compatible(self) -> bool:
        return not self.min_meshhome or _version(APP_VERSION) >= _version(self.min_meshhome)


class Catalog:
    def __init__(
        self, source: str, apps: dict[str, CatalogApp], error: str | None, fetched_at: float
    ) -> None:
        self.source, self.apps, self.error, self.fetched_at = source, apps, error, fetched_at


_catalog: Catalog | None = None


def _parse_catalog(raw: bytes) -> list[dict[str, Any]]:
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("apps"), list):
        raise ValueError("not a catalog")
    return [a for a in data["apps"] if isinstance(a, dict) and isinstance(a.get("id"), str)]


def _verified(entry: dict[str, Any], raw: bytes) -> Recipe:
    """The recipe, if it matches the catalog entry's SHA-256, id and version."""
    if hashlib.sha256(raw).hexdigest() != str(entry.get("sha256", "")).lower():
        raise RecipeError(f"{entry.get('id')}: the recipe doesn't match the catalog's checksum")
    recipe = bot_recipes.load(raw)
    if recipe.id != entry["id"] or recipe.version != entry.get("version"):
        raise RecipeError(f"{entry['id']}: the recipe's id or version doesn't match the catalog")
    return recipe


def _bundled() -> Catalog:
    apps = {}
    for entry in _parse_catalog((BUNDLED / "catalog.json").read_bytes()):
        raw = (BUNDLED / entry["recipe"]).read_bytes()
        apps[entry["id"]] = CatalogApp(entry, _verified(entry, raw), raw)
    return Catalog("bundled", apps, None, time.time())


async def _online(url: str) -> Catalog:
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:

        async def get(u: str) -> bytes:
            r = await client.get(u, headers={"User-Agent": f"MeshHome/{APP_VERSION}"})
            r.raise_for_status()
            if len(r.content) > MAX_BYTES:
                raise ValueError(f"{u} is too large")
            return r.content

        apps = {}
        base = urlsplit(url)
        for entry in _parse_catalog(await get(url)):
            rel = str(entry.get("recipe") or "")
            target = urljoin(url, rel)
            # Recipes come from beside the catalog, never from elsewhere.
            if urlsplit(target).netloc != base.netloc or rel.startswith(("/", "http:", "https:")):
                log.warning("bot apps: skipping %s (recipe outside the catalog)", entry["id"])
                continue
            try:
                raw = await get(target)
                apps[entry["id"]] = CatalogApp(entry, _verified(entry, raw), raw)
            except (RecipeError, httpx.HTTPError, ValueError) as exc:
                log.warning("bot apps: skipping %s: %s", entry["id"], exc)
        return Catalog("online", apps, None, time.time())


async def catalog(force: bool = False) -> Catalog:
    """The online catalog (refreshed every few hours), or the bundled one when it can't be read."""
    global _catalog
    if _catalog and not force and time.time() - _catalog.fetched_at < CATALOG_TTL:
        return _catalog
    url = get_settings().bot_apps_catalog_url
    if not url:
        _catalog = _bundled()
        return _catalog
    try:
        _catalog = await _online(url)
    except (httpx.HTTPError, ValueError) as exc:
        log.info("bot apps: catalog unavailable (%s); using the bundled copy", type(exc).__name__)
        fallback = _bundled()
        fallback.error = "The app catalog couldn't be reached; showing the apps included with MeshHome."
        # Try again sooner than the usual refresh.
        fallback.fetched_at = time.time() - CATALOG_TTL + 600
        _catalog = fallback
    return _catalog


# ---- installed apps -----------------------------------------------------------------------------


class Installed:
    def __init__(self, app_id: str, data: dict[str, Any]) -> None:
        self.id = app_id
        self.data = data
        self.recipe = bot_recipes.load(data["recipe"])
        self.settings: dict[str, str] = dict(data.get("settings") or {})
        self.enabled: bool = bool(data.get("enabled", True))
        self.push_token: str | None = data.get("push_token")

    @property
    def problem(self) -> str | None:
        """Why the app needs settings before it can run (e.g. an update changed a list of choices)."""
        return bot_recipes.problem(self.recipe, self.settings)


async def _store(s: AsyncSession) -> dict[str, Any]:
    return await app_settings.get_raw(s, STORE_KEY) or {"apps": {}}


async def _save(s: AsyncSession, store: dict[str, Any]) -> None:
    global _tokens
    await app_settings.put_raw(s, STORE_KEY, store)
    _tokens = None  # rebuilt on the next upload


def new_token() -> str:
    token = secrets.token_urlsafe(24)
    assert len(token) == TOKEN_LENGTH
    return token


def _sync_token(data: dict[str, Any], recipe: Recipe) -> None:
    """An app with a push source has an upload token; one without doesn't."""
    if recipe.push_source():
        data.setdefault("push_token", new_token())
    else:
        data.pop("push_token", None)


# token -> app id, for the upload route; None until first needed after a change.
_tokens: dict[str, str] | None = None


async def app_for_token(s: AsyncSession, token: str) -> str | None:
    """The installed app a push token belongs to. Compares against every token in constant time."""
    global _tokens
    if _tokens is None:
        store = await _store(s)
        _tokens = {
            d["push_token"]: app_id for app_id, d in store.get("apps", {}).items() if d.get("push_token")
        }
    found = None
    for known, app_id in _tokens.items():
        if hmac.compare_digest(known.encode(), token.encode()):
            found = app_id
    return found


async def rotate_token(s: AsyncSession, app_id: str) -> str:
    """A new upload address for an app; the old one stops working."""
    store = await _store(s)
    current = store["apps"].get(app_id)
    if current is None or not Installed(app_id, current).recipe.push_source():
        raise AppError("That app doesn't receive uploads")
    current["push_token"] = new_token()
    await _save(s, store)
    return current["push_token"]


async def installed(s: AsyncSession) -> list[Installed]:
    out = []
    for app_id, data in (await _store(s)).get("apps", {}).items():
        try:
            out.append(Installed(app_id, data))
        except RecipeError as exc:  # e.g. restored onto an older MeshHome
            log.warning("bot apps: %s can't be used: %s", app_id, exc)
    return sorted(out, key=lambda a: a.recipe.name.lower())


async def find_command(s: AsyncSession, name: str) -> Installed | None:
    for app in await installed(s):
        if app.enabled and app.recipe.command(name):
            return app
    return None


def _conflicts(recipe: Recipe, others: list[Installed]) -> None:
    for other in others:
        if other.id == recipe.id:
            continue
        clash = {c.name for c in recipe.commands} & {c.name for c in other.recipe.commands}
        if clash:
            raise AppError(
                f"/{sorted(clash)[0]} is already used by {other.recipe.name}. Remove that app first."
            )


def _merge_settings(
    recipe: Recipe, new: dict[str, str], old: dict[str, str], *, stale_ok: bool = False
) -> dict[str, str]:
    """Owner's settings for a recipe. A secret left empty keeps its saved value. stale_ok: a choice
    that's no longer offered is cleared (the app then shows that it needs settings)."""
    merged = dict(new)
    for st in recipe.settings:
        if st.type == "secret" and not str(new.get(st.key) or "").strip() and old.get(st.key):
            merged[st.key] = old[st.key]
    try:
        return bot_recipes.check_settings(recipe, merged, stale_ok=stale_ok)
    except ValueError as exc:
        raise AppError(str(exc)) from None


async def install(s: AsyncSession, app_id: str, settings: dict[str, str]) -> Installed:
    cat = await catalog()
    entry = cat.apps.get(app_id)
    if entry is None:
        raise AppError("That app isn't in the catalog")
    if not entry.compatible:
        raise AppError(f"{entry.recipe.name} needs MeshHome {entry.min_meshhome} or later")
    store = await _store(s)
    if app_id in store["apps"]:
        raise AppError(f"{entry.recipe.name} is already installed")
    _conflicts(entry.recipe, await installed(s))
    store["apps"][app_id] = {
        "recipe": json.loads(entry.raw),
        "sha256": entry.entry["sha256"],
        "settings": _merge_settings(entry.recipe, settings, {}),
        "enabled": True,
        "installed_at": datetime.now(UTC).isoformat(),
        "source": cat.source,
    }
    _sync_token(store["apps"][app_id], entry.recipe)
    await _save(s, store)
    return Installed(app_id, store["apps"][app_id])


async def update(s: AsyncSession, app_id: str, settings: dict[str, str] | None) -> Installed:
    """Move an installed app to the catalog's version (the owner approved the changes)."""
    store = await _store(s)
    current = store["apps"].get(app_id)
    if current is None:
        raise AppError("That app isn't installed")
    entry = (await catalog()).apps.get(app_id)
    if entry is None or entry.recipe.version == Installed(app_id, current).recipe.version:
        raise AppError("No update is available")
    if not entry.compatible:
        raise AppError(f"This update needs MeshHome {entry.min_meshhome} or later")
    _conflicts(entry.recipe, await installed(s))
    old = dict(current.get("settings") or {})
    current.update(
        recipe=json.loads(entry.raw),
        sha256=entry.entry["sha256"],
        # Changed lists of choices don't block the update: a stale choice is cleared instead.
        settings=_merge_settings(entry.recipe, {**old, **(settings or {})}, old, stale_ok=True),
        updated_at=datetime.now(UTC).isoformat(),
    )
    _sync_token(current, entry.recipe)
    if not entry.recipe.push_source():
        push_inbox.forget(app_id)
    await _save(s, store)
    return Installed(app_id, current)


async def configure(
    s: AsyncSession, app_id: str, settings: dict[str, str] | None, enabled: bool | None
) -> Installed:
    store = await _store(s)
    current = store["apps"].get(app_id)
    if current is None:
        raise AppError("That app isn't installed")
    app = Installed(app_id, current)
    if settings is not None:
        current["settings"] = _merge_settings(app.recipe, settings, app.settings)
    if enabled is not None:
        if enabled:
            _conflicts(app.recipe, [a for a in await installed(s) if a.enabled])
        current["enabled"] = enabled
    await _save(s, store)
    return Installed(app_id, current)


async def remove(s: AsyncSession, app_id: str) -> None:
    store = await _store(s)
    if store["apps"].pop(app_id, None) is None:
        raise AppError("That app isn't installed")
    push_inbox.forget(app_id)
    await _save(s, store)


def changes(old: Recipe, new: Recipe) -> list[str]:
    """What an update changes, in the owner's terms (shown before they approve it)."""
    out = []
    old_cmds, new_cmds = {c.name for c in old.commands}, {c.name for c in new.commands}
    out += [f"New command /{c}" for c in sorted(new_cmds - old_cmds)]
    out += [f"Removes /{c}" for c in sorted(old_cmds - new_cmds)]
    old_keys = {st.key for st in old.settings}
    out += [f"New setting: {st.label}" for st in new.settings if st.key not in old_keys]
    before = old.contacts()
    for phrase in new.contacts():
        if phrase in before:
            continue
        if phrase.startswith(bot_recipes.LOCAL_DEVICE) and any(
            p.startswith(bot_recipes.LOCAL_DEVICE) for p in before
        ):
            if phrase.endswith(bot_recipes.SELF_SIGNED):
                out.append("Will accept the device's self-signed certificate")
            continue
        verb, _, rest = phrase.partition(" ")
        verb = {"contacts": "contact", "receives": "receive", "signs": "sign"}.get(verb, verb)
        out.append(f"Will also {verb} {rest}")
    if old.logins and new.logins and _dump(old.logins) != _dump(new.logins):
        out.append("Changes how it signs in to the device")
    return out


def _dump(logins: dict[str, bot_recipes.Login]) -> dict[str, Any]:
    return {k: v.model_dump() for k, v in logins.items()}


# ---- before apps: the weather station setting ---------------------------------------------------


async def migrate(s: AsyncSession) -> None:
    """Once: a server with a weather station set up (before apps) gets the Ecowitt app, from the copy
    bundled with this release, with the same address. The old setting is left for a rollback."""
    store = await app_settings.get_raw(s, STORE_KEY)
    if store is not None:
        return
    store = {"apps": {}}
    host = (await app_settings.get_weather_config(s)).host
    if host:
        entry = _bundled().apps["ecowitt"]
        store["apps"]["ecowitt"] = {
            "recipe": json.loads(entry.raw),
            "sha256": entry.entry["sha256"],
            "settings": {"host": host},
            "enabled": True,
            "installed_at": datetime.now(UTC).isoformat(),
            "source": "bundled",
        }
        log.info("bot apps: installed the Ecowitt app for the weather station set up before apps")
    await _save(s, store)
