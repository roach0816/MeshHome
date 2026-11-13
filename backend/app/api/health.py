"""Settings → Health & logs: what's working and what isn't (services/health.py), and MeshHome's
own log (services/app_log.py)."""

from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from app.api.deps import AuthContext, require_auth, require_session
from app.services import app_log, health

router = APIRouter(prefix="/api/system", tags=["health"])

Status = Literal["ok", "info", "warn", "bad", "off"]


class HealthFact(BaseModel):
    label: str
    value: str


class HealthItem(BaseModel):
    id: str  # radio | radio_hat | mesh | sending | collection | trackers | database | services | ...
    title: str
    status: Status
    summary: str
    detail: str | None = None  # what it means (when something is wrong)
    fix: list[str] = []  # what to do, in order
    facts: list[HealthFact] = []
    link: str | None = None  # the web interface's page for it (e.g. /settings/radio)
    link_label: str | None = None
    logs: str | None = None  # its component in the log
    since: float | None = None


class ProblemCount(BaseModel):
    component: str
    label: str
    count: int
    last_at: float
    latest: str
    level: str


class HealthReport(BaseModel):
    checked_at: float
    version: str
    started_at: float
    install_kind: str
    overall: Literal["ok", "warn", "bad"]
    summary: str
    items: list[HealthItem]
    problems: list[ProblemCount]  # warnings and errors in the last 24 hours, per component


class LogLine(BaseModel):
    id: int | None = None  # history entries; None while not yet saved
    seq: int | None = None  # recent lines
    first_at: float
    last_at: float
    count: int  # the same line repeated (history)
    level: Literal["INFO", "WARNING", "ERROR", "CRITICAL"]
    component: str
    event: str | None = None  # "radio" for the radio's connection history, "app", "settings", "task"
    message: str
    detail: str | None = None  # an error's traceback
    saved: bool | None = None


class LogPage(BaseModel):
    items: list[LogLine]
    more: bool
    error: str | None = None
    components: dict[str, str]
    since: float | None = None  # recent: when MeshHome started
    kept: int | None = None  # recent: how many lines are kept in memory


@router.get("/health", response_model=HealthReport)
async def get_health(ctx: AuthContext = Depends(require_auth)) -> dict:
    """What's working and what isn't. Also readable with an API key, for monitoring."""
    return await health.report()


@router.get("/health/report", response_class=PlainTextResponse)
async def get_health_report(ctx: AuthContext = Depends(require_session)) -> str:
    """The health report, recent problems and the radio's connection history as plain text."""
    return await health.text_report()


@router.get("/logs", response_model=LogPage)
async def get_logs(
    view: Literal["history", "recent"] = "history",
    level: Literal["all", "problems"] = "all",
    component: str | None = Query(default=None, max_length=24),
    q: str | None = Query(default=None, max_length=100),
    offset: int = Query(default=0, ge=0, le=100000),
    before: int | None = Query(default=None, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    ctx: AuthContext = Depends(require_session),
) -> dict:
    """history: warnings, errors and events, kept 30 days (paged with offset). recent: every line
    since MeshHome started, from INFO up (paged with `before`, a line's seq)."""
    problems = level == "problems"
    if view == "recent":
        page = app_log.recent(problems=problems, component=component, q=q, before=before, limit=limit)
    else:
        page = await app_log.history(problems=problems, component=component, q=q, offset=offset, limit=limit)
    return {**page, "components": app_log.COMPONENTS}
