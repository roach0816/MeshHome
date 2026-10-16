# Development

## Running it locally

Requirements: Python 3.11+, Node, and Docker (for PostgreSQL).

```bash
docker run -d --name meshcore-dev-db -e POSTGRES_USER=meshcore -e POSTGRES_DB=meshcore \
  -e POSTGRES_PASSWORD="$POSTGRES_PASSWORD" -p 127.0.0.1:55432:5432 postgres:18-alpine

cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
export DATABASE_URL="postgresql+asyncpg://meshcore:$POSTGRES_PASSWORD@127.0.0.1:55432/meshcore"
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --port 8080          # API on :8080

cd ../frontend && npm install && npm run dev         # UI on :5173 (proxies /api and /ws)
```

The simulated radio produces labelled sample traffic, including simulated repeaters for remote
administration and raw packets for message paths, so everything can be tried without hardware.

## Tests

Tests run against a real, disposable PostgreSQL database (never SQLite):

```bash
export TEST_DATABASE_URL="postgresql+asyncpg://meshcore:$POSTGRES_PASSWORD@127.0.0.1:55432/meshcore_test"
cd backend && .venv/bin/pytest -q
```

Also run `ruff check` and `ruff format --check` in `backend/`, `npm run build` in `frontend/`, and
`shellcheck -S warning deploy/native/install.sh`. CI runs all of these. Installer changes are tested
in systemd-enabled `debian:bookworm` and `debian:trixie` containers: an interactive install
(including answering "n"), an unattended `--yes` install, a web-UI upgrade, a broken upgrade
(rollback), and `uninstall --purge`.

API docs are served at `/api/docs` by the running app; [API.md](API.md) is the guide for API-key users.

**API contract.** `backend/openapi.json` is the API description, kept in the repository. After any
API change, regenerate it with `python -m app.openapi_export --write` in `backend/`; a test fails if
it is out of date. CI also compares it with the last release using
[oasdiff](https://github.com/oasdiff/oasdiff) and fails on breaking changes, because scripts and
services that use API keys depend on the released API. Keep changes additive: new endpoints and new
optional fields. A deliberate break can be allowed for one release by adding
oasdiff's message to `backend/openapi-allowed-breaks.txt`. Empty that file again in the next
release.

## Code layout

```
backend/app/radio/      adapter contract, simulated radio, MeshCore TCP adapter, supervisor, packets
backend/app/services/   persistence rules (positions, dedup, channel generations, sends), bot,
                        bot apps (catalog and recipe engine), remote admin, backup, firmware check
backend/app/api/        REST routes
backend/migrations/     Alembic migrations (additive only, so rollback stays safe)
frontend/src/           React + TypeScript + Tailwind UI
deploy/native/          installer, root helpers, systemd units, pinned lego and ZephCore
deploy/k8s/             Fleet bundle (Kustomize) + *.example.yaml templates applied by hand
```

All radio access goes through `app/radio/base.py:RadioAdapter`; nothing else calls the `meshcore`
library.

## Releases and forks

- Pushing a `vX.Y.Z` tag runs `release-assets.yml`, which attaches `meshhome-X.Y.Z.tar.gz` (and the same file under its old name, `meshcore-home-X.Y.Z.tar.gz`, for installers from before the rename),
  `install.sh` and `SHA256SUMS` to the GitHub release. Native installs update from those files.
- Point the native installer at a fork with `MESHCORE_HOME_REPO=<owner>/<repo> sudo -E bash install.sh`.
- Container images and Fleet from a fork: see
  [Deploying from your own fork](install-kubernetes.md#deploying-from-your-own-fork).
- `[skip ci]` is reserved for CI's deploy-pin commit; don't use it in your own commit messages.

## Not yet included

Bluetooth and USB-serial radio connections, installing radio firmware, importing signed contact cards (`meshcore://<hex>`),
Playwright tests in CI, NetworkPolicies, and verification on real hardware of: the radio HAT, remote
node configuration, and message paths.
