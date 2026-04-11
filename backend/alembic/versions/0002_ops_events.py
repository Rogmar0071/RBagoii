"""Add ops_events table.

Revision ID: 0002
Revises: 0001
Create Date: 2025-01-02 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision: str = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ops_events",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("source", sa.Text, nullable=False, index=True),
        sa.Column("level", sa.Text, nullable=False, index=True),
        sa.Column("event_type", sa.Text, nullable=False, index=True),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("folder_id", sa.Uuid, nullable=True, index=True),
        sa.Column("job_id", sa.Uuid, nullable=True, index=True),
        sa.Column("artifact_id", sa.Uuid, nullable=True, index=True),
        sa.Column("rq_job_id", sa.Text, nullable=True, index=True),
        sa.Column("request_id", sa.Text, nullable=True, index=True),
        sa.Column("http_method", sa.Text, nullable=True),
        sa.Column("http_path", sa.Text, nullable=True),
        sa.Column("http_status", sa.Integer, nullable=True),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        sa.Column("error_type", sa.Text, nullable=True),
        sa.Column("error_detail", sa.Text, nullable=True),
        sa.Column("details_json", sa.JSON, nullable=True),
    )


def downgrade() -> None:
    op.drop_table("ops_events")
