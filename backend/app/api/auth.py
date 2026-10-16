"""First-run setup wizard and owner sign-in."""

import logging
import secrets
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    AuthContext,
    clear_session_cookies,
    client_ip,
    create_session,
    is_web_session,
    require_auth,
    require_requested_with,
    require_session,
)
from app.config import get_settings
from app.db import get_db
from app.models import AuditEvent, Session, User, utcnow
from app.radio.supervisor import supervisor
from app.realtime import hub
from app.security import (
    MIN_PASSWORD_LENGTH,
    constant_time_equals,
    hash_password,
    login_failures,
    setup_failures,
    verify_password,
)
from app.services import app_settings, radio_hat

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["auth"])


class SetupToken:
    """One-time token that proves the person running the wizard can read the server logs."""

    value: str | None = None

    @classmethod
    def ensure(cls) -> str:
        if cls.value is None:
            cls.value = get_settings().setup_token or secrets.token_urlsafe(12)
        return cls.value

    @classmethod
    def clear(cls) -> None:
        cls.value = None
        path = cls._file()
        if path is not None:
            path.unlink(missing_ok=True)

    @classmethod
    def _file(cls):
        from pathlib import Path

        d = get_settings().state_dir
        return Path(d) / "setup-token" if d else None

    @classmethod
    def write_file(cls) -> None:
        """Native installs: let the installer show the token without digging through logs."""
        path = cls._file()
        if path is None or cls.value is None:
            return
        try:
            path.write_text(cls.value + "\n")
            path.chmod(0o600)
        except OSError as exc:
            log.warning("could not write setup token file: %s", exc)


def _check_password(v: str) -> str:
    if len(v) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(v) > 256:
        raise ValueError("Password is too long")
    return v


class SetupStatus(BaseModel):
    needs_setup: bool
    version: str
    release_url: str | None = None
    # The installer set up a radio HAT on this Pi, so the wizard can offer it.
    radio_hat_ready: bool = False


class SetupRequest(BaseModel):
    setup_token: str = Field(min_length=1, max_length=128)
    username: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.@-]+$")
    password: str
    home_name: str = Field(default="Home", min_length=1, max_length=64)
    radio: app_settings.RadioConfig

    @field_validator("password")
    @classmethod
    def _password_ok(cls, v: str) -> str:
        return _check_password(v)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _password_ok(cls, v: str) -> str:
        return _check_password(v)


class ChangeUsernameRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.@-]+$")
    current_password: str = Field(min_length=1, max_length=256)


class Me(BaseModel):
    username: str
    home_name: str


class SignedIn(Me):
    """The owner, just signed in (the session is in the cookies)."""


class SessionInfo(BaseModel):
    id: uuid.UUID
    user_agent: str | None
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    current: bool


@router.get("/setup/status", response_model=SetupStatus)
async def setup_status(db: AsyncSession = Depends(get_db)) -> SetupStatus:
    from app.config import APP_VERSION

    template = get_settings().release_notes_url
    needs_setup = not await app_settings.setup_complete(db)
    hat_ready = False
    if needs_setup and radio_hat.native():
        hat_ready = (await radio_hat.service_state()).get("installed", False)
    return SetupStatus(
        needs_setup=needs_setup,
        version=APP_VERSION,
        release_url=template.replace("{version}", APP_VERSION) if template else None,
        radio_hat_ready=hat_ready,
    )


@router.post("/setup", response_model=SignedIn, dependencies=[Depends(require_requested_with)])
async def run_setup(
    body: SetupRequest, request: Request, response: Response, db: AsyncSession = Depends(get_db)
) -> SignedIn:
    ip = client_ip(request)
    if setup_failures.blocked(ip):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts; wait a minute")
    if await app_settings.setup_complete(db):
        raise HTTPException(status.HTTP_409_CONFLICT, "Setup has already been completed")
    expected = SetupToken.value
    if expected is None or not constant_time_equals(body.setup_token.strip(), expected):
        setup_failures.hit(ip)
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Setup token is incorrect — copy it from the server log"
        )
    if body.radio.mode == "tcp" and not body.radio.host:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Enter the radio's IP address or hostname")
    if body.radio.mode == "hat" and not radio_hat.native():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "There is no radio HAT on this install")

    user = User(username=body.username, password_hash=hash_password(body.password))
    db.add(user)
    await app_settings.put_installation(
        db,
        app_settings.InstallationConfig(
            home_name=body.home_name.strip(), setup_completed_at=datetime.now(UTC).isoformat()
        ),
    )
    await app_settings.put_radio_config(db, body.radio)
    await app_settings.get_fingerprint_key(db)
    db.add(AuditEvent(kind="setup.completed", detail={"radio_mode": body.radio.mode}))
    await db.flush()
    await create_session(db, request, response, user)
    await db.commit()
    SetupToken.clear()
    log.info("first-run setup completed; owner account created")
    supervisor.reload()
    return SignedIn(username=user.username, home_name=body.home_name.strip())


@router.post("/auth/login", response_model=SignedIn, dependencies=[Depends(require_requested_with)])
async def login(
    body: LoginRequest, request: Request, response: Response, db: AsyncSession = Depends(get_db)
) -> SignedIn:
    ip = client_ip(request)
    if login_failures.blocked(ip):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many failed sign-ins; wait a minute")
    user = (await db.execute(select(User).where(User.username == body.username))).scalar_one_or_none()
    # Always run a hash verification so timing does not reveal whether the username exists.
    ok = verify_password(user.password_hash if user else _DUMMY_HASH, body.password) and user is not None
    if not ok:
        login_failures.hit(ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect username or password")
    login_failures.reset(ip)
    await create_session(db, request, response, user)
    await db.commit()
    inst = await app_settings.get_installation(db)
    return SignedIn(username=user.username, home_name=inst.home_name)


@router.post("/auth/logout", status_code=204)
async def logout(
    response: Response, ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)
):
    await db.execute(delete(Session).where(Session.id == ctx.session.id))
    await db.commit()
    hub.disconnect(ctx.owner_tag)
    clear_session_cookies(response)
    response.status_code = 204
    return response


@router.get("/auth/sessions", response_model=list[SessionInfo])
async def list_sessions(
    ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)
) -> list[SessionInfo]:
    """Signed-in browsers, most recently used first."""
    rows = (
        await db.execute(
            select(Session)
            .where(Session.user_id == ctx.user.id, Session.expires_at > utcnow())
            .order_by(Session.last_seen_at.desc())
        )
    ).scalars()
    return [
        SessionInfo(
            id=s.id,
            user_agent=s.user_agent or None,
            created_at=s.created_at,
            last_seen_at=s.last_seen_at,
            expires_at=s.expires_at,
            current=s.id == ctx.session.id,
        )
        for s in rows
        if is_web_session(s)
    ]


@router.delete("/auth/sessions/{session_id}", status_code=204)
async def sign_out_session(
    session_id: uuid.UUID,
    response: Response,
    ctx: AuthContext = Depends(require_session),
    db: AsyncSession = Depends(get_db),
):
    """Sign out one browser (e.g. on a lost phone). Its open connections close at once."""
    result = await db.execute(delete(Session).where(Session.id == session_id, Session.user_id == ctx.user.id))
    if result.rowcount == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That sign-in no longer exists")
    db.add(AuditEvent(kind="auth.session_revoked", detail={"id": str(session_id)}))
    await db.commit()
    hub.disconnect(f"session:{session_id}")
    if session_id == ctx.session.id:
        clear_session_cookies(response)
    response.status_code = 204
    return response


@router.delete("/auth/sessions", status_code=204)
async def sign_out_others(
    response: Response, ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)
):
    """Sign out every other browser."""
    revoked = await _sign_out_others(db, ctx)
    db.add(AuditEvent(kind="auth.others_signed_out", detail={}))
    await db.commit()
    hub.disconnect(*revoked)
    response.status_code = 204
    return response


async def _sign_out_others(db: AsyncSession, ctx: AuthContext) -> list[str]:
    """Delete every other session; returns their socket tags, to disconnect after commit."""
    ids = (
        (
            await db.execute(
                delete(Session)
                .where(Session.user_id == ctx.user.id, Session.id != ctx.session.id)
                .returning(Session.id)
            )
        )
        .scalars()
        .all()
    )
    return [f"session:{i}" for i in ids]


@router.get("/auth/me", response_model=Me)
async def me(ctx: AuthContext = Depends(require_auth), db: AsyncSession = Depends(get_db)) -> Me:
    inst = await app_settings.get_installation(db)
    return Me(username=ctx.user.username, home_name=inst.home_name)


@router.post("/auth/password", status_code=204)
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
    response: Response,
    ctx: AuthContext = Depends(require_session),
    db: AsyncSession = Depends(get_db),
):
    if not verify_password(ctx.user.password_hash, body.current_password):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Current password is incorrect")
    user = await db.get(User, ctx.user.id)
    user.password_hash = hash_password(body.new_password)
    user.password_changed_at = utcnow()
    # Sign out every other browser.
    revoked = await _sign_out_others(db, ctx)
    db.add(AuditEvent(kind="auth.password_changed", detail={}))
    await db.commit()
    hub.disconnect(*revoked)
    response.status_code = 204
    return response


@router.put("/auth/username", response_model=Me)
async def change_username(
    body: ChangeUsernameRequest,
    ctx: AuthContext = Depends(require_session),
    db: AsyncSession = Depends(get_db),
) -> Me:
    if not verify_password(ctx.user.password_hash, body.current_password):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Current password is incorrect")
    taken = (
        await db.execute(select(User.id).where(User.username == body.username, User.id != ctx.user.id))
    ).first()
    if taken:
        raise HTTPException(status.HTTP_409_CONFLICT, "That username is already in use")
    user = await db.get(User, ctx.user.id)
    user.username = body.username
    db.add(AuditEvent(kind="auth.username_changed", detail={}))
    await db.commit()
    inst = await app_settings.get_installation(db)
    return Me(username=user.username, home_name=inst.home_name)


_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
