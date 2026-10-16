"""Remote administration of repeaters and room servers (owner only, signed-in browser).

Nothing here polls the node: each call is one request over the mesh, made when the user asks.
"""

import re
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, require_session
from app.db import get_db
from app.models import AuditEvent, Contact, Radio
from app.radio.base import NotSupported, RadioError
from app.radio.supervisor import supervisor
from app.services import remote_admin

router = APIRouter(prefix="/api/remote", tags=["remote admin"])

MANAGEABLE_KINDS = (2, 3)  # repeater, room server


class LoginIn(BaseModel):
    password: str = Field(default="", max_length=64)


class RequestIn(BaseModel):
    kind: Literal["status", "telemetry", "acl", "neighbours", "owner", "regions"]
    offset: int = Field(default=0, ge=0, le=65535)  # neighbours only: page through long lists


class CliIn(BaseModel):
    command: str = Field(min_length=1, max_length=160)

    @field_validator("command")
    @classmethod
    def _one_line(cls, v: str) -> str:
        v = v.strip()
        if not v or any(c in v for c in "\r\n\0"):
            raise ValueError("Enter a single command")
        return v


class IdentityIn(BaseModel):
    prefix: str = Field(default="", max_length=4)

    @field_validator("prefix")
    @classmethod
    def _hex(cls, v: str) -> str:
        v = v.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{0,4}", v):
            raise ValueError("Use up to 4 hex digits (0-9, a-f)")
        if v[:2] in ("00", "ff"):
            raise ValueError("Keys starting with 00 or ff are reserved")
        return v


async def _node(db: AsyncSession, contact_id: uuid.UUID) -> tuple[Contact, Radio]:
    c = await db.get(Contact, contact_id)
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact not found")
    if c.kind not in MANAGEABLE_KINDS:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Only repeaters and room servers can be managed remotely"
        )
    radio = await db.get(Radio, c.radio_id)
    return c, radio


def _require_radio(c: Contact) -> None:
    if supervisor.radio is None or supervisor.radio.id != c.radio_id or not supervisor.connected:
        raise HTTPException(status.HTTP_409_CONFLICT, "The radio for this node is not connected")


async def _run(coro):
    try:
        return await coro
    except remote_admin.RemoteTimeout as exc:
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, str(exc)) from exc
    except remote_admin.RemoteBusy as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except NotSupported as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except (RadioError, TimeoutError) as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"The radio could not send this: {exc}") from exc


async def _audit(db: AsyncSession, kind: str, c: Contact, **detail: Any) -> None:
    db.add(AuditEvent(kind=f"remote.{kind}", detail={"contact": c.public_key[:12], **detail}))
    await db.commit()


async def _names(db: AsyncSession, radio: Radio, cached: dict[str, Any]) -> dict[str, str]:
    """Contact names for the key prefixes in cached neighbour and access lists."""
    sections = cached["sections"]
    prefixes: set[str] = set()
    for n in ((sections.get("neighbours") or {}).get("data") or {}).get("neighbours") or []:
        prefixes.add(str(n.get("pubkey") or "").lower())
    for a in ((sections.get("acl") or {}).get("data") or {}).get("acl") or []:
        prefixes.add(str(a.get("key") or "").lower())
    prefixes = {x for x in prefixes if re.fullmatch(r"[0-9a-f]{2,64}", x)}
    if not prefixes:
        return {}
    out: dict[str, str] = {x: "This radio (you)" for x in prefixes if radio.public_key.startswith(x)}
    rows = await db.execute(
        select(Contact.public_key, Contact.alias, Contact.name).where(
            Contact.radio_id == radio.id, or_(*(Contact.public_key.startswith(x) for x in prefixes))
        )
    )
    for key, alias, name in rows:
        for x in prefixes:
            if key.startswith(x):
                out[x] = alias or name
    return out


@router.get("/{contact_id}")
async def get_state(
    contact_id: uuid.UUID, ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)
):
    c, radio = await _node(db, contact_id)
    cached = await remote_admin.load(c.public_key)
    return {
        "contact": {
            "id": str(c.id),
            "name": c.alias or c.name,
            "public_key": c.public_key,
            "kind": c.kind,
            "lat": c.lat,
            "lon": c.lon,
        },
        "simulated": radio.is_simulated,
        "radio_connected": supervisor.connected
        and supervisor.radio is not None
        and supervisor.radio.id == c.radio_id,
        "session": remote_admin.session(c.public_key),
        "busy": remote_admin.busy(),
        "names": await _names(db, radio, cached),
        **cached,
    }


@router.post("/{contact_id}/login")
async def login(
    contact_id: uuid.UUID,
    body: LoginIn,
    ctx: AuthContext = Depends(require_session),
    db: AsyncSession = Depends(get_db),
):
    c, _ = await _node(db, contact_id)
    _require_radio(c)
    result = await _run(remote_admin.login(c.public_key, body.password))
    await _audit(db, "login", c, ok=bool(result.get("ok")), admin=bool(result.get("admin")))
    if not result.get("ok"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "The node rejected the password")
    return {"session": remote_admin.session(c.public_key)}


@router.post("/{contact_id}/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    contact_id: uuid.UUID, ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)
):
    c, _ = await _node(db, contact_id)
    _require_radio(c)
    await _run(remote_admin.logout(c.public_key))


@router.post("/{contact_id}/request")
async def request(
    contact_id: uuid.UUID,
    body: RequestIn,
    ctx: AuthContext = Depends(require_session),
    db: AsyncSession = Depends(get_db),
):
    c, _ = await _node(db, contact_id)
    _require_radio(c)
    arg = str(body.offset) if body.kind == "neighbours" else None
    return await _run(remote_admin.request(c.public_key, body.kind, arg))


@router.post("/{contact_id}/cli")
async def cli(
    contact_id: uuid.UUID,
    body: CliIn,
    ctx: AuthContext = Depends(require_session),
    db: AsyncSession = Depends(get_db),
):
    c, _ = await _node(db, contact_id)
    _require_radio(c)
    reply = await _run(remote_admin.cli(c.public_key, body.command))
    await _audit(db, "cli", c, command=remote_admin.redact(body.command)[:80])
    return {"reply": reply}


@router.post("/{contact_id}/identity")
async def change_identity(
    contact_id: uuid.UUID,
    body: IdentityIn,
    ctx: AuthContext = Depends(require_session),
    db: AsyncSession = Depends(get_db),
):
    c, _ = await _node(db, contact_id)
    _require_radio(c)
    result = await _run(remote_admin.change_identity(c.public_key, body.prefix))
    await _audit(db, "identity", c, new_key=result["new_public_key"][:12])
    return result


@router.delete("/{contact_id}/console", status_code=status.HTTP_204_NO_CONTENT)
async def clear_console(
    contact_id: uuid.UUID, ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)
):
    c, _ = await _node(db, contact_id)
    await remote_admin.clear_console(c.public_key)
