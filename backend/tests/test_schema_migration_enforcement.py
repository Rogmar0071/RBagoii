"""
backend/tests/test_schema_migration_enforcement.py
==================================================
MQP-CONTRACT: AIC-v1.1-SCHEMA-MIGRATION-ENFORCEMENT

Tests for schema validation and migration enforcement system.

COVERAGE:
- Schema state machine transitions
- Missing column detection
- Migration enforcement
- Startup validation blocking
- create_all blocking in production
- Ingestion metrics presence validation
"""

from __future__ import annotations

import os

import pytest
import sqlalchemy as sa
from sqlmodel import SQLModel, create_engine

# Import before setting test environment
os.environ.setdefault("BACKEND_DISABLE_JOBS", "1")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def test_engine(tmp_path):
    """Create a test database engine."""
    db_path = tmp_path / "test_schema.db"
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url, connect_args={"check_same_thread": False})

    # Import models to register metadata
    from backend.app import models as _models  # noqa: F401

    # Create all tables for test
    SQLModel.metadata.create_all(engine)

    yield engine

    engine.dispose()


@pytest.fixture
def empty_engine(tmp_path):
    """Create an empty database engine (no tables)."""
    db_path = tmp_path / "test_empty.db"
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url, connect_args={"check_same_thread": False})

    yield engine

    engine.dispose()


# ---------------------------------------------------------------------------
# Test: Schema State Machine
# ---------------------------------------------------------------------------


class TestSchemaStateMachine:
    """Test schema state machine transitions."""

    def test_state_enum_values(self):
        """Verify all required states are defined."""
        from backend.app.schema_validation import SchemaState

        assert SchemaState.UNINITIALIZED == "UNINITIALIZED"
        assert SchemaState.BASELINE_ALIGNED == "BASELINE_ALIGNED"
        assert SchemaState.MIGRATION_PENDING == "MIGRATION_PENDING"
        assert SchemaState.MIGRATION_APPLIED == "MIGRATION_APPLIED"
        assert SchemaState.SCHEMA_VALID == "SCHEMA_VALID"
        assert SchemaState.SCHEMA_INVALID == "SCHEMA_INVALID"

    def test_cannot_transition_from_invalid(self, test_engine):
        """Schema_invalid is terminal - cannot transition from it."""
        from backend.app.schema_validation import (
            SchemaState,
            SchemaValidationError,
            transition_schema,
        )

        with pytest.raises(SchemaValidationError, match="terminal state"):
            transition_schema(
                test_engine,
                SchemaState.SCHEMA_INVALID,
                SchemaState.SCHEMA_VALID,
            )

    def test_any_state_can_transition_to_invalid(self, test_engine):
        """Any state can transition to SCHEMA_INVALID."""
        from backend.app.schema_validation import SchemaState, transition_schema

        for state in [
            SchemaState.UNINITIALIZED,
            SchemaState.BASELINE_ALIGNED,
            SchemaState.MIGRATION_PENDING,
            SchemaState.MIGRATION_APPLIED,
            SchemaState.SCHEMA_VALID,
        ]:
            result = transition_schema(test_engine, state, SchemaState.SCHEMA_INVALID)
            assert result == SchemaState.SCHEMA_INVALID


# ---------------------------------------------------------------------------
# Test: Column Validation
# ---------------------------------------------------------------------------


class TestColumnValidation:
    """Test table column validation."""

    def test_validate_existing_table_with_all_columns(self, test_engine):
        """Validation passes when all columns exist."""
        from backend.app.schema_validation import validate_table_columns

        # ingest_jobs should have all columns after create_all
        required = {"id", "kind", "source", "status"}
        is_valid, errors = validate_table_columns(test_engine, "ingest_jobs", required)

        assert is_valid
        assert errors == []

    def test_validate_missing_table(self, empty_engine):
        """Validation fails when table doesn't exist."""
        from backend.app.schema_validation import validate_table_columns

        is_valid, errors = validate_table_columns(
            empty_engine, "nonexistent_table", {"id"}
        )

        assert not is_valid
        assert len(errors) == 1
        assert "does not exist" in errors[0]

    def test_validate_missing_columns(self, test_engine):
        """Validation fails when required columns are missing."""
        from backend.app.schema_validation import validate_table_columns

        # Check for columns that definitely don't exist
        required = {"id", "kind", "fake_column_xyz", "another_fake_column"}
        is_valid, errors = validate_table_columns(test_engine, "ingest_jobs", required)

        assert not is_valid
        assert len(errors) == 2
        assert any("fake_column_xyz" in err for err in errors)
        assert any("another_fake_column" in err for err in errors)


# ---------------------------------------------------------------------------
# Test: Ingestion Contract Validation
# ---------------------------------------------------------------------------


class TestIngestionContractValidation:
    """Test ingestion contract field validation."""

    def test_ingest_jobs_has_required_columns(self, test_engine):
        """Verify all required ingestion columns exist."""
        from backend.app.schema_validation import (
            REQUIRED_INGEST_JOB_COLUMNS,
        )

        # Get actual columns
        inspector = sa.inspect(test_engine)
        actual_cols = {col["name"] for col in inspector.get_columns("ingest_jobs")}

        # Verify all required columns are in the model
        missing = REQUIRED_INGEST_JOB_COLUMNS - actual_cols

        # The test should show which columns are missing so migration can be run
        assert missing == set(), f"Missing columns in ingest_jobs: {missing}"

    def test_repo_id_column_exists(self, test_engine):
        """CRITICAL: repo_id column must exist."""
        inspector = sa.inspect(test_engine)
        cols = {col["name"] for col in inspector.get_columns("ingest_jobs")}

        assert "repo_id" in cols, "repo_id column missing from ingest_jobs"

    def test_ingestion_metrics_columns_exist(self, test_engine):
        """CRITICAL: All ingestion metrics columns must exist."""
        inspector = sa.inspect(test_engine)
        cols = {col["name"] for col in inspector.get_columns("ingest_jobs")}

        required_metrics = {
            "avg_chunks_per_file",
            "skipped_files_count",
            "min_chunks_per_file",
            "max_chunks_per_file",
            "median_chunks_per_file",
            "chunk_variance_flagged",
            "chunk_variance_delta_pct",
        }

        missing = required_metrics - cols
        assert missing == set(), f"Missing ingestion metrics: {missing}"


# ---------------------------------------------------------------------------
# Test: Migration System Validation
# ---------------------------------------------------------------------------


class TestMigrationSystemValidation:
    """Test migration system validation."""

    def test_migration_table_exists_after_init(self, test_engine):
        """alembic_version table should exist after migrations."""
        # Create alembic_version table manually for test
        with test_engine.connect() as conn:
            conn.execute(
                sa.text(
                    "CREATE TABLE IF NOT EXISTS alembic_version "
                    "(version_num VARCHAR(32) PRIMARY KEY)"
                )
            )
            conn.commit()

        from backend.app.schema_validation import validate_schema_migrations_table

        assert validate_schema_migrations_table(test_engine)

    def test_migration_table_missing_in_empty_db(self, empty_engine):
        """alembic_version table should not exist in empty database."""
        from backend.app.schema_validation import validate_schema_migrations_table

        assert not validate_schema_migrations_table(empty_engine)

    def test_get_current_migration_version(self, test_engine):
        """Should retrieve current migration version."""
        # Create alembic_version table and insert version
        with test_engine.connect() as conn:
            conn.execute(
                sa.text(
                    "CREATE TABLE IF NOT EXISTS alembic_version "
                    "(version_num VARCHAR(32) PRIMARY KEY)"
                )
            )
            conn.execute(sa.text("INSERT INTO alembic_version VALUES ('0032')"))
            conn.commit()

        from backend.app.schema_validation import get_current_migration_version

        version = get_current_migration_version(test_engine)
        assert version == "0032"


# ---------------------------------------------------------------------------
# Test: Startup Validation Flow
# ---------------------------------------------------------------------------


class TestStartupValidation:
    """Test startup validation flow."""

    def test_validation_fails_without_migration_table(self, test_engine):
        """Startup validation fails if alembic_version table missing."""
        from backend.app.schema_validation import (
            SchemaValidationError,
            validate_schema_on_startup,
        )

        # Don't create alembic_version table
        with pytest.raises(SchemaValidationError, match="Migration system not initialized"):
            validate_schema_on_startup(test_engine)

    def test_validation_passes_with_complete_schema(self, test_engine):
        """Startup validation passes when schema is complete."""
        # Create alembic_version table
        with test_engine.connect() as conn:
            conn.execute(
                sa.text(
                    "CREATE TABLE IF NOT EXISTS alembic_version "
                    "(version_num VARCHAR(32) PRIMARY KEY)"
                )
            )
            conn.execute(sa.text("INSERT INTO alembic_version VALUES ('0032')"))
            conn.commit()

        from backend.app.schema_validation import (
            SchemaState,
            validate_schema_on_startup,
        )

        # This will fail if columns are missing, showing what needs migration
        try:
            result = validate_schema_on_startup(test_engine)
            assert result == SchemaState.SCHEMA_VALID
        except Exception as e:
            pytest.fail(
                f"Schema validation failed. Missing columns need migration: {e}"
            )


# ---------------------------------------------------------------------------
# Test: Production Safety Guards
# ---------------------------------------------------------------------------


class TestProductionSafetyGuards:
    """Test production safety mechanisms."""

    def test_create_all_blocked_in_production(self, monkeypatch, tmp_path):
        """create_all should be blocked in production mode."""
        # Set up production-like database URL
        db_path = tmp_path / "prod.db"
        monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost/db")

        from backend.app.schema_validation import block_create_all_in_production

        # Block create_all
        block_create_all_in_production()

        # Attempting to call create_all should raise
        engine = create_engine(f"sqlite:///{db_path}")

        with pytest.raises(RuntimeError, match="BLOCKED.*create_all"):
            SQLModel.metadata.create_all(engine)

        engine.dispose()

    def test_create_all_allowed_in_test_mode(self, monkeypatch, tmp_path):
        """create_all should be allowed for sqlite/in-memory databases."""
        db_path = tmp_path / "test.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

        from backend.app.schema_validation import block_create_all_in_production

        # Should not block for sqlite
        block_create_all_in_production()

        # create_all should work
        engine = create_engine(f"sqlite:///{db_path}")

        from backend.app import models as _models  # noqa: F401
        SQLModel.metadata.create_all(engine)  # Should not raise

        engine.dispose()


# ---------------------------------------------------------------------------
# Test: Database Module Integration
# ---------------------------------------------------------------------------


class TestDatabaseModuleIntegration:
    """Test database.py module integration."""

    def test_validate_and_init_db_test_mode(self, monkeypatch, tmp_path):
        """validate_and_init_db should use create_all in test mode."""
        db_path = tmp_path / "test.db"
        db_url = f"sqlite:///{db_path}"

        monkeypatch.setenv("DATABASE_URL", db_url)

        from backend.app.database import reset_engine, validate_and_init_db

        reset_engine(db_url)

        # Should not raise in test mode
        validate_and_init_db(strict=False)

        # Verify tables were created
        from backend.app.database import get_engine
        engine = get_engine()
        inspector = sa.inspect(engine)
        tables = inspector.get_table_names()

        assert "ingest_jobs" in tables

    def test_init_db_still_works_for_tests(self, monkeypatch, tmp_path):
        """Legacy init_db should still work for test compatibility."""
        db_path = tmp_path / "test.db"
        db_url = f"sqlite:///{db_path}"

        monkeypatch.setenv("DATABASE_URL", db_url)

        from backend.app.database import init_db, reset_engine

        reset_engine(db_url)

        # Should not raise
        init_db()

        # Verify tables were created
        from backend.app.database import get_engine
        engine = get_engine()
        inspector = sa.inspect(engine)
        tables = inspector.get_table_names()

        assert "ingest_jobs" in tables


# ---------------------------------------------------------------------------
# Test: Error Messages
# ---------------------------------------------------------------------------


class TestErrorMessages:
    """Test that error messages are clear and actionable."""

    def test_missing_column_error_message(self, test_engine):
        """Error message should list missing columns clearly."""
        from backend.app.schema_validation import validate_table_columns

        required = {"id", "missing_col_1", "missing_col_2"}
        is_valid, errors = validate_table_columns(test_engine, "ingest_jobs", required)

        assert not is_valid
        assert len(errors) == 2
        for error in errors:
            assert "Missing column:" in error
            assert "missing_col" in error

    def test_schema_validation_error_has_migration_command(self, empty_engine):
        """Schema validation error should tell user how to fix it."""
        from backend.app.schema_validation import (
            SchemaValidationError,
            validate_schema_on_startup,
        )

        with pytest.raises(SchemaValidationError) as exc_info:
            validate_schema_on_startup(empty_engine)

        error_msg = str(exc_info.value)
        assert "alembic" in error_msg.lower()
        assert "upgrade head" in error_msg.lower()


# ---------------------------------------------------------------------------
# Test: Ingestion Failure Contract
# ---------------------------------------------------------------------------


class TestIngestionFailureContract:
    """Test that ingestion fails if schema is invalid."""

    def test_ingestion_must_fail_if_repo_id_missing(self):
        """MQP-CONTRACT: Ingestion MUST FAIL if repo_id column missing."""
        from backend.app.schema_validation import REQUIRED_INGEST_JOB_COLUMNS

        # repo_id must be in required columns
        assert "repo_id" in REQUIRED_INGEST_JOB_COLUMNS

    def test_ingestion_must_fail_if_metrics_missing(self):
        """MQP-CONTRACT: Ingestion MUST FAIL if metrics columns missing."""
        from backend.app.schema_validation import REQUIRED_INGEST_JOB_COLUMNS

        required_metrics = {
            "avg_chunks_per_file",
            "skipped_files_count",
            "min_chunks_per_file",
            "max_chunks_per_file",
            "median_chunks_per_file",
            "chunk_variance_flagged",
            "chunk_variance_delta_pct",
        }

        # All metrics must be in required columns
        assert required_metrics.issubset(REQUIRED_INGEST_JOB_COLUMNS)
