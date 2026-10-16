"""The health snapshot that `meshcore-home status` reads on native installs."""

import json
import os
import stat

from app.services import diagnostics
from tests.conftest import do_setup
from tests.test_api import _connected_conversations


async def test_snapshot_reports_radio_database_and_updates(client, tmp_path):
    await do_setup(client)
    await _connected_conversations(client)

    snap = await diagnostics.snapshot()
    assert snap["setup_complete"] is True
    assert snap["radio"]["state"] == "connected" and snap["radio_config"]["mode"] == "simulated"
    assert snap["database"]["ok"] is True and snap["database"]["conversations"] >= 2
    assert snap["database"]["open_gap"] is None
    assert set(snap["updates"]) >= {"checks_enabled", "latest_version", "update_available"}

    # Written atomically, readable by root and the app only, and free of secrets.
    path = tmp_path / diagnostics.FILENAME
    diagnostics.write(path, snap)
    assert json.loads(path.read_text())["version"] == snap["version"]
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o640
    text = path.read_text().lower()
    assert "password" not in text and "secret" not in text and "token" not in text
