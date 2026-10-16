"""Uploads from devices on the local network, for bot apps with a push source.

A device (a weather console, a bridge, anything that can call a URL) sends its readings to
/push/<token>, where the token identifies one installed app (app/services/bot_apps.py). The
latest reading is kept for that app's commands (app/services/push_inbox.py).

Devices can't sign in or use HTTPS, so this route has no session and works on the plain-HTTP port.
Instead it only accepts uploads from the local network. Firmware builds these URLs carelessly:
readings may follow the token after "?", "&", "/" or nothing at all, which is why tokens have a
fixed length. Uploads carry device keys (station MACs, passwords): never log them.
"""

import ipaddress
import logging

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from app import db
from app.services import bot_apps, bot_recipes, push_inbox

log = logging.getLogger(__name__)
router = APIRouter(include_in_schema=False)


class HidePushPaths(logging.Filter):
    """The web server's access log lists every request line; an upload's carries the device's keys.
    Show only "/push/…"."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if (
            isinstance(args, tuple)
            and len(args) >= 3
            and isinstance(args[2], str)
            and args[2].startswith("/push/")
        ):
            record.args = (*args[:2], "/push/…", *args[3:])
        return True


logging.getLogger("uvicorn.access").addFilter(HidePushPaths())

MAX_BYTES = 16 * 1024
PREFIX = b"/push/"


def _local(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.strip().strip("[]").split("%")[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_private or ip.is_loopback or ip.is_link_local


def from_local_network(request: Request) -> bool:
    """The client, and every hop a proxy reports, is on the local network. Forwarding headers are
    only ever used to refuse: an untrusted proxy (e.g. a Kubernetes ingress) relaying a public
    client lists that client's address, which is then refused."""
    peer = request.client.host if request.client else ""
    if not _local(peer):
        return False
    forwarded = request.headers.get("x-forwarded-for", "")
    hops = [h for h in (x.strip() for x in forwarded.split(",")) if h]
    return all(_local(h) for h in hops)


def _ok() -> PlainTextResponse:
    return PlainTextResponse("ok")


@router.api_route("/push/{rest:path}", methods=["GET", "POST"])
async def receive(request: Request, rest: str):
    peer = request.client.host if request.client else "?"
    if not from_local_network(request):
        log.info("push: refused an upload from %s (it, or an address it forwarded, is not local)", peer)
        return PlainTextResponse("Only devices on the local network can send uploads.", status_code=403)

    # The raw path, undecoded: what follows the token is part of the readings.
    raw = request.scope.get("raw_path") or request.url.path.encode()
    after = raw[len(PREFIX) :] if raw.startswith(PREFIX) else b""
    token = after[: bot_apps.TOKEN_LENGTH].decode("ascii", errors="replace")
    extra = after[bot_apps.TOKEN_LENGTH :].decode("latin-1").lstrip("/?&")
    query = "&".join(q for q in (extra, request.url.query) if q)

    length = request.headers.get("content-length", "")
    if len(query) > MAX_BYTES or (length.isdigit() and int(length) > MAX_BYTES):
        log.info("push: refused an oversized upload from %s", peer)
        return PlainTextResponse("Too large.", status_code=413)
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BYTES:
            log.info("push: refused an oversized upload from %s", peer)
            return PlainTextResponse("Too large.", status_code=413)

    async with db.session_factory()() as s:
        app_id = await bot_apps.app_for_token(s, token) if len(token) == bot_apps.TOKEN_LENGTH else None
        app = next((a for a in await bot_apps.installed(s) if a.id == app_id), None) if app_id else None
    found = app.recipe.push_source() if app else None
    if found is None:
        log.info("push: unknown upload address used by %s", peer)
        return PlainTextResponse("Not found.", status_code=404)

    _, source = found
    try:
        data = bot_recipes.decode_upload(source, query, body, request.headers.get("content-type", ""))
    except ValueError as exc:
        log.info("push: %s sent an upload for %s that couldn't be read (%s)", peer, app.id, exc)
        return PlainTextResponse("Couldn't read the upload.", status_code=400)
    if push_inbox.accept(app.id, data):
        log.debug("push: reading for %s from %s", app.id, peer)
    return _ok()
