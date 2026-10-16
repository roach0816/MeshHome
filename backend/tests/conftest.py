"""Integration tests run against a real PostgreSQL (never SQLite).

Set TEST_DATABASE_URL to a disposable database, e.g.
  postgresql+asyncpg://meshcore:<password>@127.0.0.1:55432/meshcore_test
The schema is dropped and recreated by migrations at the start of the session.
"""

import asyncio
import os

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

TEST_URL = os.environ.get("TEST_DATABASE_URL", "")
if not TEST_URL:
    pytest.skip("TEST_DATABASE_URL is not set", allow_module_level=True)

os.environ["DATABASE_URL"] = TEST_URL
os.environ["SETUP_TOKEN"] = "test-setup-token"
os.environ["STATIC_DIR"] = ""
os.environ["BOT_APPS_CATALOG_URL"] = ""  # the bundled catalog only: no network in tests

from app import db  # noqa: E402
from app.api.auth import SetupToken  # noqa: E402
from app.main import app  # noqa: E402
from app.radio.supervisor import supervisor  # noqa: E402

HEADERS = {"X-Requested-With": "meshhome"}
PASSWORD = "correct horse battery"


def _migrate() -> None:
    cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "migrations"))
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session", autouse=True)
async def database():
    engine = db.init_engine(TEST_URL)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await db.dispose_engine()
    await asyncio.to_thread(_migrate)
    yield


@pytest.fixture
async def client(database):
    """A fresh app (lifespan run) over a truncated database."""
    db.init_engine(TEST_URL)
    # A background write left over from the previous test can deadlock with TRUNCATE; PostgreSQL
    # then aborts one of the two, so try again once that write has finished.
    for attempt in range(5):
        try:
            async with db.engine().begin() as conn:
                tables = ", ".join(
                    r[0]
                    for r in await conn.execute(
                        text(
                            "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename <> 'alembic_version'"
                        )
                    )
                )
                await conn.execute(text(f"TRUNCATE {tables} CASCADE"))
            break
        except DBAPIError as exc:
            if attempt == 4 or "deadlock detected" not in str(exc):
                raise
            await asyncio.sleep(0.2)
    await db.dispose_engine()
    SetupToken.clear()
    supervisor.__init__()
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c


async def wait_for(predicate, timeout=8.0, interval=0.1):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        result = await predicate()
        if result:
            return result
        await asyncio.sleep(interval)
    raise AssertionError("condition not met in time")


def csrf(c: AsyncClient) -> dict:
    return {**HEADERS, "X-CSRF-Token": c.cookies.get("mh_csrf", "")}


async def do_setup(c: AsyncClient, mode="simulated", **radio) -> None:
    r = await c.post(
        "/api/setup",
        headers=HEADERS,
        json={
            "setup_token": "test-setup-token",
            "username": "owner",
            "password": PASSWORD,
            "radio": {"mode": mode, "sim_interval_seconds": 0, **radio},
        },
    )
    assert r.status_code == 200, r.text


async def radio_adapter():
    """The connected radio adapter, waiting out a reconnect if one is in progress (the app
    reconnects by itself; a test that injects traffic must not catch it mid-way)."""
    from app.radio.supervisor import supervisor

    async def ready():
        return supervisor.adapter if supervisor.connected and supervisor.adapter is not None else None

    return await wait_for(ready, timeout=20)
