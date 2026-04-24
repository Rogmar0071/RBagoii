"""Add active_context_session_id to conversation_contexts.

Revision ID: 0035
Revises: 0034
Create Date: 2026-04-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)

    if "conversation_contexts" not in inspector.get_table_names():
        return

    existing_cols = {c["name"] for c in inspector.get_columns("conversation_contexts")}
    if "active_context_session_id" not in existing_cols:
        op.add_column(
            "conversation_contexts",
            sa.Column("active_context_session_id", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)

    if "conversation_contexts" not in inspector.get_table_names():
        return

    existing_cols = {c["name"] for c in inspector.get_columns("conversation_contexts")}
    if "active_context_session_id" in existing_cols:
        op.drop_column("conversation_contexts", "active_context_session_id")
