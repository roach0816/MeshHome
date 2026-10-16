#!/bin/sh
set -eu
# Apply database migrations (serialised by an advisory lock) before starting the app.
# Set SKIP_MIGRATIONS=1 when an init container already ran them.
if [ "${SKIP_MIGRATIONS:-0}" != "1" ]; then
  attempt=0
  until alembic upgrade head 2>/tmp/migrate.log; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ]; then
      cat /tmp/migrate.log >&2
      echo "migrations failed after $attempt attempts; giving up" >&2
      exit 1
    fi
    echo "database not ready ($(tail -n 1 /tmp/migrate.log)); retrying in 2s ($attempt/30)" >&2
    sleep 2
  done
  grep -h "Running upgrade" /tmp/migrate.log >&2 || true
fi
exec "$@"
