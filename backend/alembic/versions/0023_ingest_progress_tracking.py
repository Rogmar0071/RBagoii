"""Add progress tracking to ingest_jobs.

Adds progress, stage, and message fields to support real-time UI visibility
per MQP-CONTRACT: INGESTION_EXECUTION_ALIGNMENT_V1.

Revision ID: 0023
Revises: 0022
Create Date: 2026-04-19 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    if "ingest_jobs" in existing_tables:
        existing_cols = {c["name"] for c in inspector.get_columns("ingest_jobs")}

        if "progress" not in existing_cols:
            op.add_column(
                "ingest_jobs",
                sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
            )

        if "stage" not in existing_cols:
            op.add_column(
                "ingest_jobs",
                sa.Column("stage", sa.Text(), nullable=False, server_default="queued"),
            )

        if "message" not in existing_cols:
            op.add_column(
                "ingest_jobs", sa.Column("message", sa.Text(), nullable=True)
            )


def downgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    if "ingest_jobs" in existing_tables:
        existing_cols = {c["name"] for c in inspector.get_columns("ingest_jobs")}

        if "message" in existing_cols:
            op.drop_column("ingest_jobs", "message")

        if "stage" in existing_cols:
            op.drop_column("ingest_jobs", "stage")

        if "progress" in existing_cols:
            op.drop_column("ingest_jobs", "progress")
