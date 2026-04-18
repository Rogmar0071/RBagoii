"""Add repo_chunks table (REPO_CONTEXT_SELECTIVE_RETRIEVAL_LAYER_V1).

Revision ID: 0015
Revises: 0014
Create Date: 2026-04-18 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    if "repo_chunks" in existing_tables:
        return

    op.create_table(
        "repo_chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("chat_file_id", sa.Uuid(), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("token_estimate", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["chat_file_id"], ["chat_files.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_repo_chunks_chat_file_id", "repo_chunks", ["chat_file_id"])


def downgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    if "repo_chunks" not in existing_tables:
        return

    existing_indexes = {
        idx["name"]
        for idx in inspector.get_indexes("repo_chunks")
    }
    if "ix_repo_chunks_chat_file_id" in existing_indexes:
        op.drop_index("ix_repo_chunks_chat_file_id", table_name="repo_chunks")

    op.drop_table("repo_chunks")
