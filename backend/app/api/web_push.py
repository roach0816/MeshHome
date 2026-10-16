"""Web push notifications to this browser (part of "Install as an app"; app/services/web_push.py).

A browser has at most one subscription, tied to its sign-in. Everything here is for the signed-in
owner's browser, never API keys.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, is_https, require_session
from app.db import get_db
from app.models import AuditEvent, WebPushSubscription
from app.services import app_settings, web_push

router = APIRouter(prefix="/api/web-push", tags=["web-push"])


class SubscriptionOut(BaseModel):
    endpoint: str
    dms: bool
    channels: bool
    last_sent_at: datetime | None
    last_error: str | None


class WebPushStatus(BaseModel):
    # Install as an app is on and this request came over HTTPS: notifications can be turned on.
    available: bool
    # This server's VAPID public key (the browser's applicationServerKey); null when unavailable.
    public_key: str | None
    # This browser's subscription, if it has one.
    subscription: SubscriptionOut | None
    # Browsers with notifications on, across all of the owner's sign-ins.
    devices: int


class Keys(BaseModel):
    p256dh: str = Field(min_length=80, max_length=100)
    auth: str = Field(min_length=16, max_length=32)

    @field_validator("p256dh")
    @classmethod
    def _point(cls, v: str) -> str:
        try:
            web_push.browser_key(v)
        except (ValueError, TypeError) as exc:
            raise ValueError("p256dh must be the browser's P-256 public key") from exc
        return v

    @field_validator("auth")
    @classmethod
    def _secret(cls, v: str) -> str:
        try:
            ok = len(web_push.b64url_decode(v)) == 16
        except ValueError:
            ok = False
        if not ok:
            raise ValueError("auth must be the browser's 16-byte secret")
        return v


class SubscriptionIn(BaseModel):
    # PushSubscription.toJSON() from the browser, plus which messages to notify about.
    endpoint: str = Field(max_length=1000)
    keys: Keys
    dms: bool = True
    channels: bool = False


async def _available(request: Request, db: AsyncSession) -> bool:
    return is_https(request) and (await app_settings.get_app_install_config(db)).enabled


async def _mine(db: AsyncSession, ctx: AuthContext) -> WebPushSubscription | None:
    return (
        await db.execute(select(WebPushSubscription).where(WebPushSubscription.session_id == ctx.session.id))
    ).scalar_one_or_none()


def _out(sub: WebPushSubscription | None) -> SubscriptionOut | None:
    if sub is None:
        return None
    return SubscriptionOut(
        endpoint=sub.endpoint,
        dms=sub.dms,
        channels=sub.channels,
        last_sent_at=sub.last_sent_at,
        last_error=sub.last_error,
    )


@router.get("", response_model=WebPushStatus)
async def get_status(
    request: Request, ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)
):
    available = await _available(request, db)
    public_key = (await web_push.vapid_keys(db))[1] if available else None
    devices = (await db.execute(select(func.count()).select_from(WebPushSubscription))).scalar_one()
    return WebPushStatus(
        available=available, public_key=public_key, subscription=_out(await _mine(db, ctx)), devices=devices
    )


@router.put("/subscription", response_model=SubscriptionOut)
async def subscribe(
    body: SubscriptionIn,
    request: Request,
    ctx: AuthContext = Depends(require_session),
    db: AsyncSession = Depends(get_db),
):
    """Turn notifications on for this browser, or change which messages it's told about."""
    if not await _available(request, db):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Turn on Install as an app (Settings, over HTTPS) to use notifications"
        )
    if not web_push.endpoint_allowed(body.endpoint):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "That isn't a known browser push service")
    # One subscription per browser: replace this sign-in's, and take over the endpoint if an
    # earlier sign-in in the same browser registered it.
    await db.execute(
        delete(WebPushSubscription).where(
            (WebPushSubscription.session_id == ctx.session.id)
            | (WebPushSubscription.endpoint == body.endpoint)
        )
    )
    sub = WebPushSubscription(
        session_id=ctx.session.id,
        endpoint=body.endpoint,
        p256dh=body.keys.p256dh,
        auth=body.keys.auth,
        dms=body.dms,
        channels=body.channels,
        user_agent=(request.headers.get("user-agent") or "")[:256] or None,
    )
    db.add(sub)
    db.add(AuditEvent(kind="web_push.subscribed", detail={"dms": body.dms, "channels": body.channels}))
    await db.commit()
    return _out(sub)


@router.delete("/subscription", status_code=204)
async def unsubscribe(ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)):
    """Turn notifications off for this browser."""
    await db.execute(delete(WebPushSubscription).where(WebPushSubscription.session_id == ctx.session.id))
    await db.commit()


class TestResult(BaseModel):
    ok: bool
    error: str | None


@router.post("/test", response_model=TestResult)
async def send_test(
    request: Request, ctx: AuthContext = Depends(require_session), db: AsyncSession = Depends(get_db)
):
    """Send a test notification to this browser."""
    sub = await _mine(db, ctx)
    if sub is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notifications aren't on for this browser")
    if not await _available(request, db):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Turn on Install as an app (Settings, over HTTPS) first"
        )
    result = await web_push.send_test(db, sub)
    return TestResult(ok=result.ok, error=result.error)
