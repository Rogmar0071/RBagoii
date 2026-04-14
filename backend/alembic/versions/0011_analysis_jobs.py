"""Add analysis_jobs table for standalone upload/analysis pipeline.

Revision ID: 0011
Revises: 0010
Create Date: 2026-04-13 00:00:00.000000

Notes
-----
Creates the ``analysis_jobs`` table used by the session-based upload endpoint
(/v1/sessions) and the analysis job processor to track job state, partial
results, errors, and warnings.

Columns
-------
id              UUID primary key.
file_path       Absolute path of the uploaded file (under /tmp/uploads/).
status          queued / running / succeeded / failed.
results_json    Full analysis result JSON (populated on success).
errors_json     List of error dicts recorded during processing.
warnings_json   List of warning dicts recorded during processing.
created_at      UTC creation timestamp.
updated_at      UTC last-updated timestamp.

The creation is guarded: if the table already exists (greenfield deployment
via init_db()) the upgrade step is a no-op.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    if "analysis_jobs" in inspector.get_table_names():
        return  # already created by init_db()

    op.create_table(
        "analysis_jobs",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("file_path", sa.Text, nullable=True),
        sa.Column("status", sa.Text, nullable=False, server_default="queued"),
        sa.Column("results_json", sa.JSON, nullable=True),
        sa.Column("errors_json", sa.JSON, nullable=True),
        sa.Column("warnings_json", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    if "analysis_jobs" in inspector.get_table_names():
        op.drop_table("analysis_jobs")
