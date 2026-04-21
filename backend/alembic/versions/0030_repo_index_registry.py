"""Add repo_index_registry table for deterministic retrieval visibility.

Revision ID: 0030
Revises: 0029
Create Date: 2026-04-21 19:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    if "repo_index_registry" in inspector.get_table_names():
        return

    op.create_table(
        "repo_index_registry",
        sa.Column("repo_id", sa.Uuid(), nullable=False),
        sa.Column("total_files", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_chunks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("indexed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="created"),
        sa.Column("last_retrieved_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["repo_id"], ["repos.id"]),
        sa.PrimaryKeyConstraint("repo_id"),
    )


def downgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    if "repo_index_registry" in inspector.get_table_names():
        op.drop_table("repo_index_registry")
