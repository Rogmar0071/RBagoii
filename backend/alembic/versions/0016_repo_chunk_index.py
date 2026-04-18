"""Add chunk_index column to repo_chunks (REPO_CONTEXT_INTELLIGENCE_LAYER_V2).

Revision ID: 0016
Revises: 0015
Create Date: 2026-04-18 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    if "repo_chunks" not in inspector.get_table_names():
        return
    existing_columns = {col["name"] for col in inspector.get_columns("repo_chunks")}
    if "chunk_index" not in existing_columns:
        op.add_column(
            "repo_chunks",
            sa.Column("chunk_index", sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    if "repo_chunks" not in inspector.get_table_names():
        return
    existing_columns = {col["name"] for col in inspector.get_columns("repo_chunks")}
    if "chunk_index" in existing_columns:
        op.drop_column("repo_chunks", "chunk_index")
