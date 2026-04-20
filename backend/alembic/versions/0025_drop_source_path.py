"""Drop source_path from ingest_jobs.

MQP-CONTRACT: AIC-v1.1-ENFORCEMENT-COMPLETE — DB-BACKED INGESTION

Removes the legacy filesystem-staging field from ingest_jobs.
source_path was used by the eliminated filesystem-staging architecture.
All ingestion data is now stored in blob_data (DB-only).

Revision ID: 0025
Revises: 0024
Create Date: 2026-04-20 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    if "ingest_jobs" in existing_tables:
        existing_cols = {c["name"] for c in inspector.get_columns("ingest_jobs")}
        if "source_path" in existing_cols:
            op.drop_column("ingest_jobs", "source_path")


def downgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    if "ingest_jobs" in existing_tables:
        existing_cols = {c["name"] for c in inspector.get_columns("ingest_jobs")}
        if "source_path" not in existing_cols:
            op.add_column(
                "ingest_jobs",
                sa.Column("source_path", sa.Text(), nullable=True),
            )
