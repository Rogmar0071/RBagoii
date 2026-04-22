#!/bin/bash
set -e

echo "Running Alembic migrations (worker)..."
echo "  pwd: $(pwd)"

ALEMBIC_INI="backend/alembic.ini"
echo "  alembic ini: ${ALEMBIC_INI}"

# Show the real reason if this fails (don't redirect to /dev/null)
if ! alembic --version; then
  echo "ERROR: alembic command failed (alembic may not be installed)." >&2
  echo "  python path: $(command -v python)" >&2
  python -V >&2
  python -m pip show alembic >&2 || true
  exit 1
fi

echo "  cmd: alembic -c ${ALEMBIC_INI} upgrade head"
if ! alembic -c "${ALEMBIC_INI}" upgrade head; then
  echo "ERROR: Migration failed. Cannot start worker with misaligned schema." >&2
  exit 1
fi

echo "Migration completed successfully."
echo "Starting RQ worker..."
exec python -m rq worker --url "${REDIS_URL}" default
