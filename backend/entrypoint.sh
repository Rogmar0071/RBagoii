#!/usr/bin/env bash
set -euo pipefail
cd backend
echo "Running migrations (if available)..."
python -m alembic -c alembic.ini upgrade head || true
echo "Starting server..."
exec uvicorn backend.app.main:app --host 0.0.0.0 --port "${PORT}"
