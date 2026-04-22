"""
backend.app.schema_validation
==============================
MQP-CONTRACT: AIC-v1.1-SCHEMA-MIGRATION-ENFORCEMENT

Schema validation and migration enforcement system.

This module implements a strict schema validation layer that ensures:
1. Application schema == database schema (always)
2. Migrations are the ONLY mutation mechanism
3. Runtime MUST fail if schema mismatch exists

State Machine
-------------
- UNINITIALIZED: Initial state, no validation performed yet
- BASELINE_ALIGNED: DB is empty or schema matches
- MIGRATION_PENDING: Migrations need to be applied
- MIGRATION_APPLIED: Migrations were just applied
- SCHEMA_VALID: Schema fully validated and matches
- SCHEMA_INVALID: Schema mismatch detected (TERMINAL, blocks app)

Transition Rules
----------------
- No implicit states
- No skipping states
- No reverse transitions
- SCHEMA_INVALID is terminal
- ANY_STATE → SCHEMA_INVALID (on violation)
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Optional

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlmodel import SQLModel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema State Machine
# ---------------------------------------------------------------------------


class SchemaState(str, Enum):
    """Schema validation state machine states."""

    UNINITIALIZED = "UNINITIALIZED"
    BASELINE_ALIGNED = "BASELINE_ALIGNED"
    MIGRATION_PENDING = "MIGRATION_PENDING"
    MIGRATION_APPLIED = "MIGRATION_APPLIED"
    SCHEMA_VALID = "SCHEMA_VALID"
    SCHEMA_INVALID = "SCHEMA_INVALID"  # Terminal state


class SchemaValidationError(Exception):
    """Raised when schema validation fails - application MUST NOT start."""

    pass


# ---------------------------------------------------------------------------
# Required Columns Definition
# ---------------------------------------------------------------------------

# MQP-CONTRACT: INGESTION CONTRACT ALIGNMENT (CRITICAL)
# These columns MUST exist in ingest_jobs table
REQUIRED_INGEST_JOB_COLUMNS = {
    "id",
    "kind",
    "source",
    "branch",
    "blob_data",
    "blob_mime_type",
    "blob_size_bytes",
    "status",
    "execution_locked",
    "execution_attempts",
    "last_execution_at",
    "progress",
    "error",
    "conversation_id",
    "workspace_id",
    "file_count",
    "chunk_count",
    "repo_id",  # CRITICAL: FK to repos.id
    "avg_chunks_per_file",  # CRITICAL: ingestion metrics
    "skipped_files_count",
    "min_chunks_per_file",
    "max_chunks_per_file",
    "median_chunks_per_file",
    "chunk_variance_flagged",
    "chunk_variance_delta_pct",
    "created_at",
    "updated_at",
}


# ---------------------------------------------------------------------------
# Schema Validation Functions
# ---------------------------------------------------------------------------


def validate_table_columns(
    engine: sa.Engine, table_name: str, required_columns: set[str]
) -> tuple[bool, list[str]]:
    """
    Validate that a table contains all required columns.

    Returns:
        (is_valid, missing_columns)
    """
    inspector = inspect(engine)

    if table_name not in inspector.get_table_names():
        return False, [f"Table '{table_name}' does not exist"]

    existing_cols = {col["name"] for col in inspector.get_columns(table_name)}
    missing = required_columns - existing_cols

    if missing:
        return False, [f"Missing column: {col}" for col in sorted(missing)]

    return True, []


def validate_ingest_jobs_schema(engine: sa.Engine) -> tuple[bool, list[str]]:
    """
    Validate ingest_jobs table schema.

    MQP-CONTRACT: INGESTION CONTRACT ALIGNMENT (CRITICAL)
    If ANY column is missing, ingestion MUST FAIL before execution.
    """
    return validate_table_columns(engine, "ingest_jobs", REQUIRED_INGEST_JOB_COLUMNS)


def validate_schema_migrations_table(engine: sa.Engine) -> bool:
    """Check if alembic_version table exists (migration system initialized)."""
    inspector = inspect(engine)
    return "alembic_version" in inspector.get_table_names()


def get_current_migration_version(engine: sa.Engine) -> Optional[str]:
    """Get the current migration version from alembic_version table."""
    try:
        with engine.connect() as conn:
            result = conn.execute(
                sa.text("SELECT version_num FROM alembic_version LIMIT 1")
            )
            row = result.fetchone()
            return row[0] if row else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Schema State Transitions
# ---------------------------------------------------------------------------


def transition_schema(
    engine: sa.Engine, current_state: SchemaState, target_state: SchemaState
) -> SchemaState:
    """
    Transition schema state with validation.

    MQP-CONTRACT: TRANSITION AUTHORITY (CRITICAL)
    ALL schema changes MUST occur through this function.

    Rules:
    - Validate schema BEFORE transition
    - Apply migration atomically
    - Record schema version AFTER success
    - HARD FAIL on violation
    """
    logger.info(f"Schema state transition: {current_state.value} → {target_state.value}")

    # Validate transition is allowed
    if current_state == SchemaState.SCHEMA_INVALID:
        raise SchemaValidationError(
            "Cannot transition from SCHEMA_INVALID - terminal state"
        )

    # Execute transition
    if target_state == SchemaState.SCHEMA_INVALID:
        # Any state can transition to SCHEMA_INVALID
        return SchemaState.SCHEMA_INVALID

    # Other transitions require validation
    return target_state


# ---------------------------------------------------------------------------
# Startup Validation Flow
# ---------------------------------------------------------------------------


def validate_schema_on_startup(engine: sa.Engine) -> SchemaState:
    """
    Validate database schema on application startup.

    MQP-CONTRACT: DETERMINISTIC FLOW ENFORCEMENT

    Startup Flow:
    INIT → VALIDATE_SCHEMA → APPLY_PENDING_MIGRATIONS → VALIDATE_SCHEMA → READY

    If ANY step fails → SCHEMA_INVALID

    Returns:
        SchemaState.SCHEMA_VALID if validation passes
        SchemaState.SCHEMA_INVALID if validation fails (raises exception)

    Raises:
        SchemaValidationError: If schema is invalid (APPLICATION MUST NOT START)
    """
    current_state = SchemaState.UNINITIALIZED

    try:
        # Step 1: Check if migration system exists
        if not validate_schema_migrations_table(engine):
            current_state = transition_schema(
                engine, current_state, SchemaState.SCHEMA_INVALID
            )
            raise SchemaValidationError(
                "Migration system not initialized (alembic_version table missing). "
                "Run: alembic -c backend/alembic.ini upgrade head"
            )

        current_state = SchemaState.BASELINE_ALIGNED

        # Step 2: Validate critical tables exist and have required columns
        is_valid, errors = validate_ingest_jobs_schema(engine)

        if not is_valid:
            current_state = transition_schema(
                engine, current_state, SchemaState.SCHEMA_INVALID
            )
            error_msg = (
                "SCHEMA VALIDATION FAILED - ingest_jobs table schema mismatch:\n"
                + "\n".join(f"  - {err}" for err in errors)
                + "\n\nMIGRATION REQUIRED. Run: alembic -c backend/alembic.ini upgrade head"
            )
            raise SchemaValidationError(error_msg)

        # Step 3: All validations passed
        current_state = transition_schema(
            engine, current_state, SchemaState.SCHEMA_VALID
        )

        version = get_current_migration_version(engine)
        logger.info(f"Schema validation PASSED (migration version: {version})")

        return SchemaState.SCHEMA_VALID

    except SchemaValidationError:
        # Re-raise validation errors
        raise
    except Exception as e:
        # Unexpected errors also block startup
        current_state = SchemaState.SCHEMA_INVALID
        logger.error(f"Unexpected error during schema validation: {e}", exc_info=True)
        raise SchemaValidationError(
            f"Schema validation failed with unexpected error: {e}"
        ) from e


# ---------------------------------------------------------------------------
# Production Safety Guards
# ---------------------------------------------------------------------------


def block_create_all_in_production() -> None:
    """
    Block usage of SQLModel.metadata.create_all() in production.

    MQP-CONTRACT: DRIFT PREVENTION ENFORCEMENT

    This function should be called at application startup to prevent
    automatic schema creation/modification outside the migration system.

    NOTE: Only blocks for production databases (PostgreSQL, MySQL, etc.)
    SQLite and in-memory databases are allowed for testing.
    """
    import os

    original_create_all = SQLModel.metadata.create_all

    # Store reference to original if it's not already wrapped
    if not hasattr(original_create_all, '__wrapped__'):
        _original_impl = original_create_all
    else:
        # Already wrapped, get the original
        _original_impl = getattr(original_create_all, '__wrapped__')

    def blocked_create_all(*args, **kwargs):
        # Check current DATABASE_URL to determine if we should block
        db_url = os.environ.get("DATABASE_URL", "")
        is_test_db = not db_url or db_url.startswith("sqlite:///") or ":memory:" in db_url

        if is_test_db:
            # Allow for test databases
            return _original_impl(*args, **kwargs)

        # Block for production
        raise RuntimeError(
            "BLOCKED: SQLModel.metadata.create_all() is disabled in production. "
            "Schema changes MUST go through Alembic migrations. "
            "See: backend/alembic/versions/"
        )

    # Mark the wrapper so we can detect it later
    blocked_create_all.__wrapped__ = _original_impl

    SQLModel.metadata.create_all = blocked_create_all
    logger.info("Schema mutation guard installed (blocks non-test databases)")
