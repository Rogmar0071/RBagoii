"""Unified ingestion pipeline schema.

Adds:
  ingest_jobs table (new unified ingestion pipeline)
  repo_chunks.source_url       TEXT nullable
  repo_chunks.ingest_job_id    UUID nullable FK -> ingest_jobs.id (indexed)
  chat_files.workspace_id      TEXT nullable (indexed)

Revision ID: 0021
Revises: 0020
Create Date: 2026-04-19 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    # ------------------------------------------------------------------
    # ingest_jobs
    # ------------------------------------------------------------------
    if "ingest_jobs" not in existing_tables:
        op.create_table(
            "ingest_jobs",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("kind", sa.Text(), nullable=False),
            sa.Column("source", sa.Text(), nullable=False),
            sa.Column("branch", sa.Text(), nullable=True),
            sa.Column("source_path", sa.Text(), nullable=True),
            sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("conversation_id", sa.Text(), nullable=True),
            sa.Column("workspace_id", sa.Text(), nullable=True),
            sa.Column("file_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_ingest_jobs_kind", "ingest_jobs", ["kind"])
        op.create_index("ix_ingest_jobs_status", "ingest_jobs", ["status"])
        op.create_index(
            "ix_ingest_jobs_conversation_id", "ingest_jobs", ["conversation_id"]
        )
        op.create_index("ix_ingest_jobs_workspace_id", "ingest_jobs", ["workspace_id"])

    # ------------------------------------------------------------------
    # repo_chunks: source_url and ingest_job_id
    # ------------------------------------------------------------------
    if "repo_chunks" in existing_tables:
        existing_cols = {c["name"] for c in inspector.get_columns("repo_chunks")}

        if "source_url" not in existing_cols:
            op.add_column("repo_chunks", sa.Column("source_url", sa.Text(), nullable=True))

        if "ingest_job_id" not in existing_cols:
            op.add_column(
                "repo_chunks", sa.Column("ingest_job_id", sa.Uuid(), nullable=True)
            )
            # FK constraint — skip for SQLite (no ALTER TABLE ADD FOREIGN KEY)
            dialect = bind.dialect.name
            if dialect != "sqlite":
                op.create_foreign_key(
                    "fk_repo_chunks_ingest_job_id",
                    "repo_chunks",
                    "ingest_jobs",
                    ["ingest_job_id"],
                    ["id"],
                )
            op.create_index(
                "ix_repo_chunks_ingest_job_id", "repo_chunks", ["ingest_job_id"]
            )

    # ------------------------------------------------------------------
    # chat_files: workspace_id
    # ------------------------------------------------------------------
    if "chat_files" in existing_tables:
        chat_cols = {c["name"] for c in inspector.get_columns("chat_files")}

        if "workspace_id" not in chat_cols:
            op.add_column(
                "chat_files", sa.Column("workspace_id", sa.Text(), nullable=True)
            )
            op.create_index("ix_chat_files_workspace_id", "chat_files", ["workspace_id"])


def downgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()
    dialect = bind.dialect.name

    # chat_files.workspace_id
    chat_cols = {c["name"] for c in inspector.get_columns("chat_files")}
    if "workspace_id" in chat_cols:
        op.drop_index("ix_chat_files_workspace_id", table_name="chat_files")
        op.drop_column("chat_files", "workspace_id")

    # repo_chunks columns
    rc_cols = {c["name"] for c in inspector.get_columns("repo_chunks")}
    if "ingest_job_id" in rc_cols:
        op.drop_index("ix_repo_chunks_ingest_job_id", table_name="repo_chunks")
        if dialect != "sqlite":
            op.drop_constraint(
                "fk_repo_chunks_ingest_job_id", "repo_chunks", type_="foreignkey"
            )
        op.drop_column("repo_chunks", "ingest_job_id")
    if "source_url" in rc_cols:
        op.drop_column("repo_chunks", "source_url")

    # ingest_jobs table
    if "ingest_jobs" in existing_tables:
        op.drop_index("ix_ingest_jobs_workspace_id", table_name="ingest_jobs")
        op.drop_index("ix_ingest_jobs_conversation_id", table_name="ingest_jobs")
        op.drop_index("ix_ingest_jobs_status", table_name="ingest_jobs")
        op.drop_index("ix_ingest_jobs_kind", table_name="ingest_jobs")
        op.drop_table("ingest_jobs")
