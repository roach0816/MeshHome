"""The radio supervisor outlives a database outage (PostgreSQL restarting for a security update): it
retries with backoff and reconnects, rather than stopping and leaving the radio offline until the
app restarts."""

from app.radio.supervisor import supervisor
from app.services import app_settings
from tests.conftest import do_setup, radio_adapter, wait_for


async def test_reconnects_after_database_outage(client, monkeypatch):
    await do_setup(client)
    await radio_adapter()

    real = app_settings.get_radio_config
    failures = {"left": 2}

    async def flaky(db):
        if failures["left"]:
            failures["left"] -= 1
            # What asyncpg raises while PostgreSQL's socket file is gone.
            raise FileNotFoundError(2, "No such file or directory")
        return await real(db)

    monkeypatch.setattr(app_settings, "get_radio_config", flaky)
    supervisor.reload()  # the session ends, and the next pass meets the outage

    async def backing_off():
        return supervisor.status.state == "backoff" and "Database unavailable" in (
            supervisor.status.last_error or ""
        )

    await wait_for(backing_off, timeout=10, interval=0.02)
    assert not supervisor._task.done()

    await radio_adapter()  # connected again once the database is back
    assert failures["left"] == 0
    assert not supervisor._task.done()
