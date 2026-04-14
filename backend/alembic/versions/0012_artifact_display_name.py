"""Add display_name column to artifacts table.

Revision ID: 0012
Revises: 0011
Create Date: 2026-04-14 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    if "artifacts" not in inspector.get_table_names():
        return
    existing_columns = {col["name"] for col in inspector.get_columns("artifacts")}
    if "display_name" not in existing_columns:
        op.add_column("artifacts", sa.Column("display_name", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    if "artifacts" not in inspector.get_table_names():
        return
    existing_columns = {col["name"] for col in inspector.get_columns("artifacts")}
    if "display_name" in existing_columns:
        op.drop_column("artifacts", "display_name")
