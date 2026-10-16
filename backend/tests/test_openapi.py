"""The committed API contract (openapi.json) matches the code."""

from app.openapi_export import SNAPSHOT, current


def test_openapi_snapshot_is_current():
    assert SNAPSHOT.read_text() == current(), "API changed: run `python -m app.openapi_export --write`"
