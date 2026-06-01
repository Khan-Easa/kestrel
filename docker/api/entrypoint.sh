#!/bin/sh
# Kestrel API container entrypoint.
# Runs DB migrations (only when a database is configured) then starts uvicorn.
set -eu

if [ -n "${KESTREL_DATABASE_URL:-}" ]; then
    echo "kestrel-entrypoint: applying migrations (alembic upgrade head)"
    alembic upgrade head
fi

exec uvicorn kestrel.app:create_app --factory --host 0.0.0.0 --port 8000