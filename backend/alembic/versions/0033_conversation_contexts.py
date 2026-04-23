"""Add conversation_contexts table for deterministic active context binding.

Revision ID: 0033
Revises: 0032
Create Date: 2026-04-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    if "conversation_contexts" not in existing_tables:
        op.create_table(
            "conversation_contexts",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("conversation_id", sa.Text(), nullable=False),
            sa.Column("repo_id", sa.Uuid(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
            sa.ForeignKeyConstraint(["repo_id"], ["repos.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "conversation_id",
                name="uq_conversation_contexts_conversation_id",
            ),
        )
        op.create_index(
            "ix_conversation_contexts_conversation_id",
            "conversation_contexts",
            ["conversation_id"],
        )
        op.create_index("ix_conversation_contexts_repo_id", "conversation_contexts", ["repo_id"])


def downgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    if "conversation_contexts" in existing_tables:
        existing_indexes = {idx["name"] for idx in inspector.get_indexes("conversation_contexts")}
        if "ix_conversation_contexts_repo_id" in existing_indexes:
            op.drop_index("ix_conversation_contexts_repo_id", table_name="conversation_contexts")
        if "ix_conversation_contexts_conversation_id" in existing_indexes:
            op.drop_index(
                "ix_conversation_contexts_conversation_id",
                table_name="conversation_contexts",
            )
        op.drop_table("conversation_contexts")
