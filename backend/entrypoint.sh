#!/bin/bash
set -e

echo "════════════════════════════════════════════════════════════════"
echo "SCHEMA ENFORCEMENT: MQP-CONTRACT SCHEMA_ALIGNMENT_ENFORCEMENT_V1"
echo "════════════════════════════════════════════════════════════════"
echo ""

# ────────────────────────────────────────────────────────────────────
# STEP 1: Verify Alembic Installation
# ────────────────────────────────────────────────────────────────────
echo "STEP 1: Verifying migration system..."
# Note: Using 'alembic' command (preferred invocation method)
# Both 'alembic' and 'python -m alembic' work, but direct command is clearer
if ! alembic --version; then
  echo "FATAL: alembic not installed" >&2
  echo "  python path: $(command -v python)" >&2
  python -V >&2
  python -m pip show alembic >&2 || true
  exit 1
fi
echo "  ✓ Alembic available"
echo ""

# ────────────────────────────────────────────────────────────────────
# STEP 2: Apply Database Migrations
# ────────────────────────────────────────────────────────────────────
echo "STEP 2: Applying database migrations..."
ALEMBIC_INI="backend/alembic.ini"
echo "  Configuration: ${ALEMBIC_INI}"
echo "  Command: alembic -c ${ALEMBIC_INI} upgrade head"

if ! alembic -c "${ALEMBIC_INI}" upgrade head; then
  echo "FATAL: Migration failed. Cannot start server with misaligned schema." >&2
  exit 1
fi
echo "  ✓ Migrations applied successfully"
echo ""

# ────────────────────────────────────────────────────────────────────
# STEP 3: Validate Schema Alignment (CRITICAL)
# ────────────────────────────────────────────────────────────────────
echo "STEP 3: Validating schema alignment..."
if ! python backend/validate_schema.py; then
  echo "FATAL: Schema validation failed" >&2
  echo "Database schema does not match application expectations" >&2
  exit 1
fi
echo ""

# ────────────────────────────────────────────────────────────────────
# STEP 4: Start Application
# ────────────────────────────────────────────────────────────────────
echo "════════════════════════════════════════════════════════════════"
echo "SCHEMA VALIDATION PASSED ✓"
echo "Starting API server..."
echo "════════════════════════════════════════════════════════════════"
echo ""
exec uvicorn backend.app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
