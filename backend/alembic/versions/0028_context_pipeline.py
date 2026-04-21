"""Add Phase 3 context pipeline tables.

MQP-CONTRACT: RBOII-PHASE1-SEAL + PHASE3-PIPELINE-SPINE v1.0

Creates:
  context_pipeline_runs      — tracks run_context_pipeline executions
  context_gap_records        — persists ContextGap rows from Stage 5
  context_alignment_records  — persists AlignedIntentContract rows from Stage 6

Revision ID: 0028
Revises: 0027
Create Date: 2026-04-21 00:01:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    # ------------------------------------------------------------------
    # context_pipeline_runs
    # ------------------------------------------------------------------
    if "context_pipeline_runs" not in existing_tables:
        op.create_table(
            "context_pipeline_runs",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("ingest_job_id", sa.Uuid(), nullable=False),
            sa.Column("user_intent", sa.Text(), nullable=False, server_default=""),
            sa.Column("status", sa.Text(), nullable=False, server_default="pending_alignment"),
            sa.Column("active_session_id", sa.Uuid(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_context_pipeline_runs_ingest_job_id",
            "context_pipeline_runs",
            ["ingest_job_id"],
        )
        op.create_index(
            "ix_context_pipeline_runs_status",
            "context_pipeline_runs",
            ["status"],
        )

    # ------------------------------------------------------------------
    # context_gap_records
    # ------------------------------------------------------------------
    if "context_gap_records" not in existing_tables:
        op.create_table(
            "context_gap_records",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("pipeline_run_id", sa.Uuid(), nullable=False),
            sa.Column("gap_type", sa.Text(), nullable=False),
            sa.Column("severity", sa.Text(), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["pipeline_run_id"], ["context_pipeline_runs.id"]),
        )
        op.create_index(
            "ix_context_gap_records_pipeline_run_id",
            "context_gap_records",
            ["pipeline_run_id"],
        )
        op.create_index(
            "ix_context_gap_records_gap_type",
            "context_gap_records",
            ["gap_type"],
        )
        op.create_index(
            "ix_context_gap_records_severity",
            "context_gap_records",
            ["severity"],
        )

    # ------------------------------------------------------------------
    # context_alignment_records
    # ------------------------------------------------------------------
    if "context_alignment_records" not in existing_tables:
        op.create_table(
            "context_alignment_records",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("pipeline_run_id", sa.Uuid(), nullable=False),
            sa.Column("user_intent", sa.Text(), nullable=False),
            sa.Column("refinement", sa.Text(), nullable=True),
            sa.Column("system_summary", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["pipeline_run_id"], ["context_pipeline_runs.id"]),
        )
        op.create_index(
            "ix_context_alignment_records_pipeline_run_id",
            "context_alignment_records",
            ["pipeline_run_id"],
        )


def downgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    for table, indexes in [
        (
            "context_alignment_records",
            ["ix_context_alignment_records_pipeline_run_id"],
        ),
        (
            "context_gap_records",
            [
                "ix_context_gap_records_pipeline_run_id",
                "ix_context_gap_records_gap_type",
                "ix_context_gap_records_severity",
            ],
        ),
        (
            "context_pipeline_runs",
            [
                "ix_context_pipeline_runs_ingest_job_id",
                "ix_context_pipeline_runs_status",
            ],
        ),
    ]:
        if table in existing_tables:
            existing_indexes = {ix["name"] for ix in inspector.get_indexes(table)}
            for ix_name in indexes:
                if ix_name in existing_indexes:
                    op.drop_index(ix_name, table_name=table)
            op.drop_table(table)
