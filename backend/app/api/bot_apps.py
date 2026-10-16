"""Bot apps (Settings → Bot → Apps): browse the catalog, install, configure, update and remove.

Owner only (a signed-in browser, never an API key): app settings hold network addresses and may
hold keys. See app/services/bot_apps.py.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, require_session
from app.db import get_db
from app.models import AuditEvent
from app.realtime import hub
from app.services import bot_apps, bot_recipes, push_inbox
from app.services.bot_recipes import Recipe

router = APIRouter(prefix="/api/bot/apps", tags=["bot"])


class OptionOut(BaseModel):
    value: str
    label: str


class SettingOut(BaseModel):
    key: str
    label: str
    # host, text, secret, groups ("name: id, id" per line) or select.
    type: str
    required: bool
    placeholder: str
    help: str
    # select: its choices, or (depends_on) the choices for each value of an earlier select.
    options: list[OptionOut] | None = None
    depends_on: str | None = None
    options_by: dict[str, list[OptionOut]] | None = None


class CommandOut(BaseModel):
    name: str
    help: str
    # The groups setting it reads: it's sent as "/<name> <group>".
    group: str | None = None


class AppInfo(BaseModel):
    id: str
    name: str
    version: str
    summary: str
    category: str
    description: str
    author: str
    homepage: str
    commands: list[CommandOut]
    settings: list[SettingOut]
    # What it talks to: "a device on your local network", internet hosts.
    contacts: list[str]


class CatalogEntry(AppInfo):
    installed: bool
    compatible: bool
    min_meshhome: str


class UpdateInfo(BaseModel):
    version: str
    changes: list[str]


class PushInfo(BaseModel):
    # Where the device sends its readings: this path on MeshHome's plain-HTTP address.
    path: str
    help: str
    format: str
    # How long a reading counts as current.
    max_age: int
    received_at: datetime | None


class InstalledApp(AppInfo):
    enabled: bool
    # Saved settings; secrets are never sent back (an empty value means "set").
    values: dict[str, str]
    secrets_set: list[str]
    update: UpdateInfo | None
    # Apps whose data a device sends to MeshHome (a push source).
    push: PushInfo | None = None
    # Why it needs settings before it can answer (e.g. an update changed a list of choices).
    problem: str | None = None


class AppsState(BaseModel):
    installed: list[InstalledApp]
    catalog: list[CatalogEntry]
    # "online": the catalog repository; "bundled": the copy included with this MeshHome.
    catalog_source: str
    catalog_error: str | None
    # The catalog's categories, from its apps, in order.
    categories: list[str]


def _info(r: Recipe) -> dict:
    return {
        "id": r.id,
        "name": r.name,
        "version": r.version,
        "summary": r.summary,
        "category": r.category,
        "description": r.description,
        "author": r.author,
        "homepage": r.homepage,
        "commands": [CommandOut(name=c.name, help=c.help, group=c.group) for c in r.commands],
        "settings": [SettingOut(**s.model_dump()) for s in r.settings],
        "contacts": r.contacts(),
    }


async def _state(db: AsyncSession, force: bool = False) -> AppsState:
    cat = await bot_apps.catalog(force=force)
    mine = await bot_apps.installed(db)
    out = []
    for app in mine:
        secret = {s.key for s in app.recipe.settings if s.type == "secret"}
        newer = cat.apps.get(app.id)
        update = None
        if newer and bot_apps._version(newer.recipe.version) > bot_apps._version(app.recipe.version):
            update = UpdateInfo(
                version=newer.recipe.version, changes=bot_apps.changes(app.recipe, newer.recipe)
            )
        push = None
        if (found := app.recipe.push_source()) and app.push_token:
            src = found[1]
            at = push_inbox.received_at(app.id)
            push = PushInfo(
                path=f"/push/{app.push_token}",
                help=src.help,
                format=src.format,
                max_age=src.max_age,
                received_at=datetime.fromtimestamp(at, UTC) if at else None,
            )
        out.append(
            InstalledApp(
                **_info(app.recipe),
                push=push,
                problem=app.problem,
                enabled=app.enabled,
                values={k: v for k, v in app.settings.items() if k not in secret},
                secrets_set=sorted(k for k in secret if app.settings.get(k)),
                update=update,
            )
        )
    ids = {a.id for a in mine}
    entries = [
        CatalogEntry(
            **_info(c.recipe),
            installed=c.recipe.id in ids,
            compatible=c.compatible,
            min_meshhome=c.min_meshhome,
        )
        for c in sorted(cat.apps.values(), key=lambda c: c.recipe.name.lower())
    ]
    categories = sorted({e.category for e in entries}, key=lambda c: (c == "Other", c.lower()))
    return AppsState(
        installed=out,
        catalog=entries,
        catalog_source=cat.source,
        catalog_error=cat.error,
        categories=categories,
    )


def _fail(exc: Exception) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, str(exc))


@router.get("", response_model=AppsState)
async def get_apps(ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)):
    return await _state(db)


@router.post("/refresh", response_model=AppsState)
async def refresh(ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)):
    """Read the catalog again now (it's otherwise refreshed every few hours)."""
    return await _state(db, force=True)


class InstallIn(BaseModel):
    settings: dict[str, str] = Field(default_factory=dict)


@router.post("/{app_id}", response_model=AppsState)
async def install(
    app_id: str,
    body: InstallIn,
    ctx: AuthContext = Depends(require_session),
    db: AsyncSession = Depends(get_db),
):
    try:
        app = await bot_apps.install(db, app_id, body.settings)
    except (bot_apps.AppError, bot_recipes.RecipeError) as exc:
        raise _fail(exc) from exc
    db.add(AuditEvent(kind="bot_app.installed", detail={"app": app_id, "version": app.recipe.version}))
    await db.commit()
    hub.publish("settings-updated", key="bot_apps")
    return await _state(db)


class ConfigureIn(BaseModel):
    settings: dict[str, str] | None = None
    enabled: bool | None = None


@router.patch("/{app_id}", response_model=AppsState)
async def configure(
    app_id: str,
    body: ConfigureIn,
    ctx: AuthContext = Depends(require_session),
    db: AsyncSession = Depends(get_db),
):
    try:
        await bot_apps.configure(db, app_id, body.settings, body.enabled)
    except (bot_apps.AppError, bot_recipes.RecipeError) as exc:
        raise _fail(exc) from exc
    db.add(AuditEvent(kind="bot_app.configured", detail={"app": app_id}))
    await db.commit()
    hub.publish("settings-updated", key="bot_apps")
    return await _state(db)


@router.post("/{app_id}/update", response_model=AppsState)
async def update(
    app_id: str,
    body: InstallIn,
    ctx: AuthContext = Depends(require_session),
    db: AsyncSession = Depends(get_db),
):
    """Move to the catalog's newer version (after the owner has seen what changes)."""
    try:
        app = await bot_apps.update(db, app_id, body.settings or None)
    except (bot_apps.AppError, bot_recipes.RecipeError) as exc:
        raise _fail(exc) from exc
    db.add(AuditEvent(kind="bot_app.updated", detail={"app": app_id, "version": app.recipe.version}))
    await db.commit()
    hub.publish("settings-updated", key="bot_apps")
    return await _state(db)


@router.delete("/{app_id}", response_model=AppsState)
async def remove(
    app_id: str, ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)
):
    try:
        await bot_apps.remove(db, app_id)
    except bot_apps.AppError as exc:
        raise _fail(exc) from exc
    db.add(AuditEvent(kind="bot_app.removed", detail={"app": app_id}))
    await db.commit()
    hub.publish("settings-updated", key="bot_apps")
    return await _state(db)


@router.post("/{app_id}/push-token", response_model=AppsState)
async def new_push_address(
    app_id: str, ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)
):
    """A new upload address for the app; the old one stops working at once."""
    try:
        await bot_apps.rotate_token(db, app_id)
    except bot_apps.AppError as exc:
        raise _fail(exc) from exc
    db.add(AuditEvent(kind="bot_app.push_address_changed", detail={"app": app_id}))
    await db.commit()
    hub.publish("settings-updated", key="bot_apps")
    return await _state(db)


class TestIn(BaseModel):
    command: str | None = None
    # Unsaved settings to try (e.g. while installing); saved ones are used for anything left out.
    settings: dict[str, str] = Field(default_factory=dict)
    # The word after the command, for a command that reads a group.
    arg: str | None = None


class TestOut(BaseModel):
    command: str
    reply: str


@router.post("/{app_id}/test", response_model=TestOut)
async def test(
    app_id: str, body: TestIn, ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)
):
    """Run a command now and show the reply (nothing is sent). Works before installing, too."""
    mine = {a.id: a for a in await bot_apps.installed(db)}
    if app_id in mine:
        recipe, saved = mine[app_id].recipe, mine[app_id].settings
    else:
        entry = (await bot_apps.catalog()).apps.get(app_id)
        if entry is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "That app isn't in the catalog")
        recipe, saved = entry.recipe, {}
    command = body.command or recipe.commands[0].name
    try:
        settings = bot_apps._merge_settings(recipe, {**saved, **body.settings}, saved)
        arg = bot_recipes.argument(body.arg or "")
        reply = await bot_recipes.run(recipe, command, settings, arg, use_cache=False)
    except (bot_apps.AppError, bot_recipes.RecipeError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except bot_recipes.SourceError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return TestOut(command=command, reply=reply)
