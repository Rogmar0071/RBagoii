"""
backend.app.database
====================
Database engine and session helpers.

MQP-CONTRACT: AIC-v1.1-SCHEMA-MIGRATION-ENFORCEMENT

Configuration
-------------
DATABASE_URL    SQLAlchemy-compatible URL.  Examples:
                  postgresql+psycopg2://user:pass@host/db
                  sqlite:///./dev.db
                  sqlite:///:memory:   (tests)

When DATABASE_URL is not set the module still imports cleanly but
``get_engine()`` / ``get_session()`` raise ``RuntimeError`` with a
helpful message so the calling route can return HTTP 503.

Migration Enforcement
---------------------
Schema changes MUST go through Alembic migrations.
Use ``validate_and_init_db()`` at startup to enforce schema validation.
Direct use of ``init_db()`` is deprecated - it only exists for test compatibility.
"""

from __future__ import annotations

import os
from typing import Generator

from sqlmodel import Session, SQLModel, create_engine

_engine = None


def get_engine():
    """Return (and lazily create) the shared SQLAlchemy engine."""
    global _engine
    if _engine is None:
        url = os.environ.get("DATABASE_URL", "").strip()
        if not url:
            raise RuntimeError(
                "DATABASE_URL environment variable is not set. "
                "Configure a Postgres (or SQLite) URL to enable folder persistence."
            )
        # connect_args only needed for SQLite
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _engine = create_engine(url, connect_args=connect_args)
    return _engine


def reset_engine(url: str | None = None) -> None:
    """Replace the cached engine – used in tests to inject a fresh SQLite URL."""
    global _engine
    _engine = None
    if url is not None:
        os.environ["DATABASE_URL"] = url


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency – yields a DB session per request."""
    with Session(get_engine()) as session:
        yield session


def init_db() -> None:
    """
    Create all tables defined in ``backend.app.models`` (idempotent).

    WARNING: This function is DEPRECATED for production use.
    It only exists for backward compatibility with tests.

    In production, use ``validate_and_init_db()`` which enforces
    migration-based schema management.
    """
    from backend.app import models as _models  # noqa: F401 – registers SQLModel metadata

    SQLModel.metadata.create_all(get_engine())


def validate_and_init_db(strict: bool = True) -> None:
    """
    Initialize database with schema validation enforcement.

    MQP-CONTRACT: AIC-v1.1-SCHEMA-MIGRATION-ENFORCEMENT

    This function:
    1. Validates that schema matches application models
    2. Blocks startup if schema mismatch detected
    3. Prevents direct schema mutations outside migrations

    Args:
        strict: If True, enforce schema validation (default: True).
                If False, fall back to init_db() for test compatibility.

    Raises:
        SchemaValidationError: If schema validation fails (blocks startup)
    """
    import os

    from backend.app import models as _models  # noqa: F401

    db_url = os.environ.get("DATABASE_URL", "").strip()

    # For tests with in-memory databases, allow create_all
    is_test_db = not db_url or ":memory:" in db_url or db_url.startswith("sqlite:///")

    if not strict or is_test_db:
        # Test mode: allow create_all for backward compatibility
        SQLModel.metadata.create_all(get_engine())
        return

    # Production mode: enforce schema validation
    from backend.app.schema_validation import (
        SchemaValidationError,
        block_create_all_in_production,
        validate_schema_on_startup,
    )

    try:
        # Block create_all in production
        block_create_all_in_production()

        # Validate schema
        validate_schema_on_startup(get_engine())

    except SchemaValidationError as e:
        # Schema validation failed - log and re-raise
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"SCHEMA VALIDATION FAILED: {e}")
        logger.error(
            "APPLICATION STARTUP BLOCKED. "
            "Run migrations: alembic -c backend/alembic.ini upgrade head"
        )
        raise
