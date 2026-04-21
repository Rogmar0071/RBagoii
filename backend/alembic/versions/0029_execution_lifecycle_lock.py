"""Add execution lifecycle lock fields to stateful job tables.

Revision ID: 0029
Revises: 0028
Create Date: 2026-04-21 15:55:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


_TABLES: tuple[str, ...] = ("jobs", "analysis_jobs", "ingest_jobs")


def _existing_columns(table_name: str) -> set[str]:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    for table_name in _TABLES:
        existing = _existing_columns(table_name)
        if not existing:
            continue

        if "execution_locked" not in existing:
            op.add_column(
                table_name,
                sa.Column(
                    "execution_locked",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.false(),
                ),
            )
        if "execution_attempts" not in existing:
            op.add_column(
                table_name,
                sa.Column(
                    "execution_attempts",
                    sa.Integer(),
                    nullable=False,
                    server_default="0",
                ),
            )
        if "last_execution_at" not in existing:
            op.add_column(
                table_name,
                sa.Column("last_execution_at", sa.DateTime(timezone=True), nullable=True),
            )


def downgrade() -> None:
    for table_name in _TABLES:
        existing = _existing_columns(table_name)
        if not existing:
            continue
        if "last_execution_at" in existing:
            op.drop_column(table_name, "last_execution_at")
        if "execution_attempts" in existing:
            op.drop_column(table_name, "execution_attempts")
        if "execution_locked" in existing:
            op.drop_column(table_name, "execution_locked")
