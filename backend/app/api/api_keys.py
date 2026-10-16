"""API keys for other services and scripts. Managed from a signed-in browser only.

A key is shown once when it is created; only its SHA-256 digest is stored. Requests use
"Authorization: Bearer mh_...". See docs/API.md.
"""

import uuid
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, require_session
from app.db import get_db
from app.models import ApiKey, AuditEvent, utcnow
from app.realtime import hub
from app.security import new_api_key, token_digest

router = APIRouter(prefix="/api/api-keys", tags=["api-keys"])

MAX_KEYS = 25
PREFIX_CHARS = 12  # "mh_" + 9 characters, enough to tell keys apart


class ApiKeyOut(BaseModel):
    id: uuid.UUID
    name: str
    prefix: str
    scope: str
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    expired: bool

    @classmethod
    def of(cls, k: ApiKey) -> "ApiKeyOut":
        return cls(
            id=k.id,
            name=k.name,
            prefix=k.prefix,
            scope=k.scope,
            created_at=k.created_at,
            expires_at=k.expires_at,
            last_used_at=k.last_used_at,
            expired=k.expires_at is not None and k.expires_at <= utcnow(),
        )


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    # read: GET requests only. write: also send messages and act on conversations and contacts.
    scope: Literal["read", "write"]
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Enter a name for this key")
        return v


class ApiKeyCreated(BaseModel):
    key: str  # shown once
    api_key: ApiKeyOut


@router.get("", response_model=list[ApiKeyOut])
async def list_keys(ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)):
    rows = (
        await db.execute(
            select(ApiKey).where(ApiKey.user_id == ctx.user.id).order_by(ApiKey.created_at.desc())
        )
    ).scalars()
    return [ApiKeyOut.of(k) for k in rows]


@router.post("", response_model=ApiKeyCreated, status_code=201)
async def create_key(
    body: ApiKeyCreate, ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)
):
    count = (
        await db.execute(select(func.count()).select_from(ApiKey).where(ApiKey.user_id == ctx.user.id))
    ).scalar_one()
    if count >= MAX_KEYS:
        raise HTTPException(status.HTTP_409_CONFLICT, f"At most {MAX_KEYS} API keys; revoke one first")
    raw = new_api_key()
    key = ApiKey(
        user_id=ctx.user.id,
        name=body.name,
        prefix=raw[:PREFIX_CHARS],
        key_hash=token_digest(raw),
        scope=body.scope,
        expires_at=utcnow() + timedelta(days=body.expires_in_days) if body.expires_in_days else None,
    )
    db.add(key)
    await db.flush()
    db.add(AuditEvent(kind="api_key.created", detail={"id": str(key.id), "scope": body.scope}))
    await db.commit()
    return ApiKeyCreated(key=raw, api_key=ApiKeyOut.of(key))


@router.delete("/{key_id}", status_code=204)
async def revoke_key(
    key_id: uuid.UUID, ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)
):
    result = await db.execute(delete(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == ctx.user.id))
    if not result.rowcount:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "API key not found")
    db.add(AuditEvent(kind="api_key.revoked", detail={"id": str(key_id)}))
    await db.commit()
    hub.disconnect(f"api_key:{key_id}")
