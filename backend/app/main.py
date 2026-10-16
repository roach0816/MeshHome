import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware

from app import db
from app.api import (
    api_keys,
    auth,
    backup,
    bot_apps,
    contacts,
    firmware,
    inbox,
    meta,
    node_map,
    push_receiver,
    radio,
    radio_config,
    remote,
    system,
)
from app.api import (
    web_push as web_push_api,
)
from app.api.deps import bearer_token, load_api_key, load_session, session_cookie
from app.config import APP_VERSION, get_settings
from app.radio.supervisor import supervisor
from app.realtime import hub
from app.services import app_settings, diagnostics, updates, web_push
from app.services import bot_apps as bot_apps_service

log = logging.getLogger("meshhome")

WS_HEARTBEAT_SECONDS = 25


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    db.init_engine()
    async with db.session_factory()() as s:
        needs_setup = not await app_settings.setup_complete(s)
        await bot_apps_service.migrate(s)
        await s.commit()
    if needs_setup:
        token = auth.SetupToken.ensure()
        auth.SetupToken.write_file()
        banner = "=" * 64
        log.warning(
            "\n%s\n  MeshHome first-run setup\n  Open the web UI and enter this setup token:\n\n      %s\n%s",
            banner,
            token,
            banner,
        )
    supervisor.start()
    web_push.start()
    update_task = asyncio.create_task(_update_check_loop(), name="update-check")
    diag_task = asyncio.create_task(diagnostics.loop(), name="diagnostics")
    try:
        yield
    finally:
        update_task.cancel()
        diag_task.cancel()
        await supervisor.stop()
        await web_push.stop()
        hub.close_all()
        await db.dispose_engine()


async def _update_check_loop() -> None:
    """Check for new releases shortly after start, then on the checker's own schedule."""
    await asyncio.sleep(30)
    while True:
        await updates.checker.check()
        await asyncio.sleep(3600)


app = FastAPI(
    title="MeshHome",
    version=APP_VERSION,
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)


def _openapi_schema() -> dict:
    """Declare API-key (bearer) auth so /api/docs offers "Authorize" and clients see it."""
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
    schema.setdefault("components", {})["securitySchemes"] = {
        "ApiKey": {
            "type": "http",
            "scheme": "bearer",
            "description": "An API key from Settings → API keys (mh_...). See docs/API.md.",
        }
    }
    schema["security"] = [{"ApiKey": []}]
    app.openapi_schema = schema
    return schema


app.openapi = _openapi_schema


class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        h = response.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("Referrer-Policy", "same-origin")
        h.setdefault("X-Frame-Options", "DENY")
        if not request.url.path.startswith("/api/docs"):
            h.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline'; "
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
            )
        if request.url.path.startswith("/api/"):
            h.setdefault("Cache-Control", "no-store")
        return response


app.add_middleware(SecurityHeaders)
app.include_router(auth.router)
app.include_router(inbox.router)
app.include_router(contacts.router)
app.include_router(radio.router)
app.include_router(node_map.router)
app.include_router(radio_config.router)
app.include_router(system.router)
app.include_router(api_keys.router)
app.include_router(remote.router)
app.include_router(firmware.router)
app.include_router(backup.router)
app.include_router(meta.router)
app.include_router(web_push_api.router)
app.include_router(bot_apps.router)
app.include_router(push_receiver.router)  # /push/<token>: device uploads, no session (local network only)


# ---- health ------------------------------------------------------------------------------


@app.get("/health/live", include_in_schema=False)
async def live():
    return {"ok": True}


@app.get("/health/ready", include_in_schema=False)
async def ready():
    try:
        async with db.engine().connect() as conn:
            await asyncio.wait_for(conn.execute(text("SELECT 1")), timeout=3)
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False}, status_code=503)
    return {"ok": True}


# ---- websocket ---------------------------------------------------------------------------


def _same_origin(ws: WebSocket) -> bool:
    origin = ws.headers.get("origin")
    if not origin:
        return False
    host = ws.headers.get("x-forwarded-host") or ws.headers.get("host", "")
    return origin.split("://", 1)[-1] == host


@app.websocket("/ws")
async def websocket(ws: WebSocket):
    raw_token = bearer_token(ws.headers)
    if raw_token is not None:
        # Another service, with an API key: no browser origin check applies.
        async with db.session_factory()() as s:
            ctx = await load_api_key(s, raw_token)
    else:
        if not _same_origin(ws):
            await ws.close(code=4403)
            return
        async with db.session_factory()() as s:
            ctx = await load_session(s, session_cookie(ws.cookies))
    if ctx is None:
        await ws.close(code=4401)
        return
    await ws.accept()
    queue = hub.register(ctx.owner_tag)
    await ws.send_json({"v": 1, "type": "hello", "radio_state": supervisor.status.state})

    async def pump():
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), WS_HEARTBEAT_SECONDS)
            except TimeoutError:
                event = {"v": 1, "type": "ping"}
            if event is None:  # dropped as a slow client, or shutting down
                await ws.close(code=4408)
                return
            if "_close" in event:  # its session or API key was revoked
                await ws.close(code=event["_close"])
                return
            await ws.send_json(event)

    async def drain():
        while True:
            await ws.receive_text()  # client pings / keepalives; content ignored

    tasks = [asyncio.create_task(pump()), asyncio.create_task(drain())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        hub.unregister(queue)


# ---- static frontend (production build) --------------------------------------------------

WEB_APP_MANIFEST = {
    "id": "/",
    "name": "MeshHome",
    "short_name": "MeshHome",
    "description": "Your home MeshCore radio's messages, contacts and map.",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#f7f7f8",
    "theme_color": "#f7f7f8",
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
        {"src": "/favicon.svg", "sizes": "any", "type": "image/svg+xml"},
    ],
    # Android: MeshHome appears in the share sheet; text and links arrive at /share (Share.tsx).
    "share_target": {
        "action": "/share",
        "method": "GET",
        "params": {"title": "title", "text": "text", "url": "url"},
    },
    "shortcuts": [
        {"name": "Contacts", "url": "/contacts", "icons": [{"src": "/icon-192.png", "sizes": "192x192"}]},
        {"name": "Map", "url": "/map", "icons": [{"src": "/icon-192.png", "sizes": "192x192"}]},
    ],
}


@app.get("/manifest.webmanifest", include_in_schema=False)
async def web_app_manifest():
    """Lets browsers install MeshHome as an app, once the owner turns that on (needs HTTPS)."""
    async with db.session_factory()() as s:
        if not (await app_settings.get_app_install_config(s)).enabled:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
    return JSONResponse(
        WEB_APP_MANIFEST, media_type="application/manifest+json", headers={"Cache-Control": "no-cache"}
    )


_static = Path(get_settings().static_dir)
if get_settings().static_dir and _static.is_dir():
    _index = _static / "index.html"

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def spa(path: str):
        if path.startswith(("api/", "health/", "ws")):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        candidate = (_static / path).resolve()
        if path and candidate.is_file() and _static.resolve() in candidate.parents:
            if path.startswith("assets/"):
                headers = {"Cache-Control": "public, max-age=31536000, immutable"}
            elif path == "sw.js":
                headers = {"Cache-Control": "no-cache"}  # browsers check for a new service worker
            else:
                headers = {}
            return FileResponse(candidate, headers=headers)
        return FileResponse(_index, headers={"Cache-Control": "no-cache"})
elif os.environ.get("STATIC_DIR"):
    log.warning("STATIC_DIR=%s does not exist; frontend will not be served", _static)
