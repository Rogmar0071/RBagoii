"""REPO_VALIDATION_LAYER_V1 — add validation columns to repos table.

Adds:
  validation_status   TEXT   NOT NULL  DEFAULT 'pending'
  validation_score    INTEGER NOT NULL DEFAULT 0
  trust_class         TEXT   NOT NULL  DEFAULT 'UNKNOWN'
  validation_signals  JSON   nullable
  validated_at        DATETIME(tz) nullable

Revision ID: 0019
Revises: 0018
Create Date: 2026-04-19 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    if "repos" not in existing_tables:
        # Table doesn't exist yet — init_db() will create it with the new
        # columns already present via the SQLModel metadata, so nothing to do.
        return

    existing_columns = {col["name"] for col in inspector.get_columns("repos")}

    with op.batch_alter_table("repos", recreate="auto") as batch_op:
        if "validation_status" not in existing_columns:
            batch_op.add_column(
                sa.Column(
                    "validation_status",
                    sa.Text(),
                    nullable=False,
                    server_default="pending",
                )
            )
        if "validation_score" not in existing_columns:
            batch_op.add_column(
                sa.Column(
                    "validation_score",
                    sa.Integer(),
                    nullable=False,
                    server_default="0",
                )
            )
        if "trust_class" not in existing_columns:
            batch_op.add_column(
                sa.Column(
                    "trust_class",
                    sa.Text(),
                    nullable=False,
                    server_default="UNKNOWN",
                )
            )
        if "validation_signals" not in existing_columns:
            batch_op.add_column(
                sa.Column("validation_signals", sa.JSON(), nullable=True)
            )
        if "validated_at" not in existing_columns:
            batch_op.add_column(
                sa.Column(
                    "validated_at", sa.DateTime(timezone=True), nullable=True
                )
            )


def downgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    if "repos" not in existing_tables:
        return

    existing_columns = {col["name"] for col in inspector.get_columns("repos")}

    with op.batch_alter_table("repos", recreate="auto") as batch_op:
        for col in (
            "validated_at",
            "validation_signals",
            "trust_class",
            "validation_score",
            "validation_status",
        ):
            if col in existing_columns:
                batch_op.drop_column(col)
