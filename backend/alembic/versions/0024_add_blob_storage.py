"""Add blob storage to ingest_jobs.

MQP-CONTRACT: AIC-v1.1-ENFORCEMENT-COMPLETE — DB-BACKED INGESTION

Adds:
  ingest_jobs.blob_data         BYTEA/BLOB nullable (binary storage)
  ingest_jobs.blob_mime_type    TEXT nullable
  ingest_jobs.blob_size_bytes   INTEGER default 0

Revision ID: 0024
Revises: 0023
Create Date: 2026-04-20 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    # ------------------------------------------------------------------
    # ingest_jobs: add blob storage fields
    # ------------------------------------------------------------------
    if "ingest_jobs" in existing_tables:
        existing_cols = {c["name"] for c in inspector.get_columns("ingest_jobs")}

        if "blob_data" not in existing_cols:
            op.add_column("ingest_jobs", sa.Column("blob_data", sa.LargeBinary(), nullable=True))

        if "blob_mime_type" not in existing_cols:
            op.add_column("ingest_jobs", sa.Column("blob_mime_type", sa.Text(), nullable=True))

        if "blob_size_bytes" not in existing_cols:
            op.add_column(
                "ingest_jobs",
                sa.Column("blob_size_bytes", sa.Integer(), nullable=False, server_default="0")
            )


def downgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    if "ingest_jobs" in existing_tables:
        existing_cols = {c["name"] for c in inspector.get_columns("ingest_jobs")}

        if "blob_size_bytes" in existing_cols:
            op.drop_column("ingest_jobs", "blob_size_bytes")

        if "blob_mime_type" in existing_cols:
            op.drop_column("ingest_jobs", "blob_mime_type")

        if "blob_data" in existing_cols:
            op.drop_column("ingest_jobs", "blob_data")
