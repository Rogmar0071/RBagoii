"""Add superseded_by_id column to global_chat_messages.

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-11 00:00:00.000000

Notes
-----
global_chat_messages may already exist on deployments that relied on
init_db() (SQLModel.metadata.create_all) rather than Alembic for table
creation.  The upgrade is therefore guarded:

* If the table does not exist at all (greenfield deployment running
  alembic upgrade head before any init_db() call), the column addition
  is skipped – init_db() will create the full table with the column.
* If the table exists but the column is absent (existing production DB
  that pre-dates PR #38), the column is added.
* If the column is already present (idempotent re-run), the addition is
  skipped.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision: str = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    if "global_chat_messages" not in inspector.get_table_names():
        # Table does not exist yet; init_db() will create it with the column.
        return
    existing_columns = {col["name"] for col in inspector.get_columns("global_chat_messages")}
    if "superseded_by_id" in existing_columns:
        # Column already present – nothing to do.
        return
    op.add_column(
        "global_chat_messages",
        sa.Column("superseded_by_id", sa.Uuid, nullable=True),
    )


def downgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    if "global_chat_messages" not in inspector.get_table_names():
        return
    existing_columns = {col["name"] for col in inspector.get_columns("global_chat_messages")}
    if "superseded_by_id" not in existing_columns:
        return
    op.drop_column("global_chat_messages", "superseded_by_id")
