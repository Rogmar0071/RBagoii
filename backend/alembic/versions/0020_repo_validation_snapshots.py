"""REPO_VALIDATION_SNAPSHOT_V1 — create repo_validation_snapshots table.

Adds:
  repo_validation_snapshots
    id              UUID  PRIMARY KEY
    repo_id         UUID  NOT NULL  FK -> repos.id  (indexed)
    validation_score INTEGER NOT NULL
    trust_class     TEXT   NOT NULL
    validation_signals JSON nullable
    created_at      DATETIME(tz) NOT NULL

Revision ID: 0020
Revises: 0019
Create Date: 2026-04-19 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    if "repo_validation_snapshots" in existing_tables:
        return

    op.create_table(
        "repo_validation_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("repo_id", sa.Uuid(), nullable=False),
        sa.Column("validation_score", sa.Integer(), nullable=False),
        sa.Column("trust_class", sa.Text(), nullable=False),
        sa.Column("validation_signals", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["repo_id"], ["repos.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_repo_validation_snapshots_repo_id",
        "repo_validation_snapshots",
        ["repo_id"],
    )


def downgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    if "repo_validation_snapshots" not in existing_tables:
        return

    op.drop_index(
        "ix_repo_validation_snapshots_repo_id",
        table_name="repo_validation_snapshots",
    )
    op.drop_table("repo_validation_snapshots")
