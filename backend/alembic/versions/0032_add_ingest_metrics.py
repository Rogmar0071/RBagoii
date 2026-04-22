"""Add ingestion metrics and repo_id to ingest_jobs

MQP-CONTRACT: AIC-v1.1-SCHEMA-MIGRATION-ENFORCEMENT

Adds required ingestion contract fields to ingest_jobs table:
- repo_id: FK to repos.id (nullable, indexed)
- avg_chunks_per_file: REAL default 0.0
- skipped_files_count: INTEGER default 0
- min_chunks_per_file: INTEGER default 0
- max_chunks_per_file: INTEGER default 0
- median_chunks_per_file: REAL default 0.0
- chunk_variance_flagged: BOOLEAN default false
- chunk_variance_delta_pct: REAL default 0.0

Revision ID: 0032
Revises: 0031
Create Date: 2026-04-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add ingestion metrics and repo_id to ingest_jobs."""
    bind = op.get_context().bind
    inspector = sa.inspect(bind)

    if "ingest_jobs" not in inspector.get_table_names():
        # Table doesn't exist - skip
        return

    existing_cols = {c["name"] for c in inspector.get_columns("ingest_jobs")}
    dialect = bind.dialect.name

    # Add repo_id column with FK
    if "repo_id" not in existing_cols:
        op.add_column(
            "ingest_jobs",
            sa.Column("repo_id", sa.Uuid(), nullable=True),
        )
        # Create FK constraint (skip for SQLite)
        if dialect != "sqlite":
            op.create_foreign_key(
                "fk_ingest_jobs_repo_id",
                "ingest_jobs",
                "repos",
                ["repo_id"],
                ["id"],
            )
        op.create_index("ix_ingest_jobs_repo_id", "ingest_jobs", ["repo_id"])

    # Add ingestion metrics columns
    if "avg_chunks_per_file" not in existing_cols:
        op.add_column(
            "ingest_jobs",
            sa.Column("avg_chunks_per_file", sa.Float(), nullable=False, server_default="0.0"),
        )

    if "skipped_files_count" not in existing_cols:
        op.add_column(
            "ingest_jobs",
            sa.Column("skipped_files_count", sa.Integer(), nullable=False, server_default="0"),
        )

    if "min_chunks_per_file" not in existing_cols:
        op.add_column(
            "ingest_jobs",
            sa.Column("min_chunks_per_file", sa.Integer(), nullable=False, server_default="0"),
        )

    if "max_chunks_per_file" not in existing_cols:
        op.add_column(
            "ingest_jobs",
            sa.Column("max_chunks_per_file", sa.Integer(), nullable=False, server_default="0"),
        )

    if "median_chunks_per_file" not in existing_cols:
        op.add_column(
            "ingest_jobs",
            sa.Column("median_chunks_per_file", sa.Float(), nullable=False, server_default="0.0"),
        )

    if "chunk_variance_flagged" not in existing_cols:
        op.add_column(
            "ingest_jobs",
            sa.Column(
                "chunk_variance_flagged",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )

    if "chunk_variance_delta_pct" not in existing_cols:
        op.add_column(
            "ingest_jobs",
            sa.Column(
                "chunk_variance_delta_pct",
                sa.Float(),
                nullable=False,
                server_default="0.0",
            ),
        )


def downgrade() -> None:
    """Remove ingestion metrics and repo_id from ingest_jobs."""
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    dialect = bind.dialect.name

    if "ingest_jobs" not in inspector.get_table_names():
        return

    existing_cols = {c["name"] for c in inspector.get_columns("ingest_jobs")}

    # Remove metrics columns
    for col in [
        "chunk_variance_delta_pct",
        "chunk_variance_flagged",
        "median_chunks_per_file",
        "max_chunks_per_file",
        "min_chunks_per_file",
        "skipped_files_count",
        "avg_chunks_per_file",
    ]:
        if col in existing_cols:
            op.drop_column("ingest_jobs", col)

    # Remove repo_id
    if "repo_id" in existing_cols:
        op.drop_index("ix_ingest_jobs_repo_id", table_name="ingest_jobs")
        if dialect != "sqlite":
            op.drop_constraint(
                "fk_ingest_jobs_repo_id", "ingest_jobs", type_="foreignkey"
            )
        op.drop_column("ingest_jobs", "repo_id")
