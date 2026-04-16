"""Add conversation_id column to global_chat_messages.

Revision ID: 0014
Revises: 0013
Create Date: 2026-04-16 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    if "global_chat_messages" not in inspector.get_table_names():
        return
    existing_columns = {col["name"] for col in inspector.get_columns("global_chat_messages")}
    existing_indexes = {idx["name"] for idx in inspector.get_indexes("global_chat_messages")}
    if "conversation_id" not in existing_columns:
        op.add_column(
            "global_chat_messages",
            sa.Column("conversation_id", sa.Text(), nullable=False, server_default="legacy_default"),
        )
    if "ix_global_chat_messages_conversation_id" not in existing_indexes:
        op.create_index(
            "ix_global_chat_messages_conversation_id",
            "global_chat_messages",
            ["conversation_id"],
        )


def downgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    if "global_chat_messages" not in inspector.get_table_names():
        return
    existing_columns = {col["name"] for col in inspector.get_columns("global_chat_messages")}
    existing_indexes = {idx["name"] for idx in inspector.get_indexes("global_chat_messages")}
    if "ix_global_chat_messages_conversation_id" in existing_indexes:
        op.drop_index(
            "ix_global_chat_messages_conversation_id",
            table_name="global_chat_messages",
        )
    if "conversation_id" in existing_columns:
        op.drop_column("global_chat_messages", "conversation_id")
