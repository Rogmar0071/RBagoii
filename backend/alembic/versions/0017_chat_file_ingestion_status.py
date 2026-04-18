"""Add ingestion_status column to chat_files (REPO_CONTEXT_FLOW_RECOVERY_V1).

Revision ID: 0017
Revises: 0016
Create Date: 2026-04-18 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    if "chat_files" not in inspector.get_table_names():
        return
    existing_columns = {col["name"] for col in inspector.get_columns("chat_files")}
    if "ingestion_status" not in existing_columns:
        op.add_column(
            "chat_files",
            sa.Column("ingestion_status", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    if "chat_files" not in inspector.get_table_names():
        return
    existing_columns = {col["name"] for col in inspector.get_columns("chat_files")}
    if "ingestion_status" in existing_columns:
        op.drop_column("chat_files", "ingestion_status")
