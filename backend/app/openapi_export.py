"""The API contract as a file: backend/openapi.json.

Clients can be generated from it, and CI fails when a change would break clients built against
the last release (see .github/workflows/ci.yml). Regenerate it after any API change:

    python -m app.openapi_export --write
"""

import json
import sys
from pathlib import Path

SNAPSHOT = Path(__file__).resolve().parent.parent / "openapi.json"


def current() -> str:
    from app.config import API_VERSION
    from app.main import app

    spec = app.openapi()
    # The app version would change the file every release; the contract is the API version.
    spec["info"] = {**spec["info"], "version": f"api-{API_VERSION}"}
    return json.dumps(spec, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    if sys.argv[1:] == ["--write"]:
        SNAPSHOT.write_text(current())
        print(f"wrote {SNAPSHOT}")
        return 0
    if sys.argv[1:] == ["--check"]:
        if not SNAPSHOT.exists() or SNAPSHOT.read_text() != current():
            print("openapi.json is out of date: run `python -m app.openapi_export --write`", file=sys.stderr)
            return 1
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
