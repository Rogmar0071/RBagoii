#!/bin/bash
set -e

echo "Running Alembic migrations..."
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
  echo "ERROR: Migration failed. Cannot start server with misaligned schema." >&2
  exit 1
fi

echo "Migration completed successfully."
echo "Starting API server..."
exec uvicorn backend.app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
