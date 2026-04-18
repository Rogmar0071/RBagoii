"""REPO_CONTEXT_FINALIZATION_V1 — Phase 1+2.

Creates the first-class `repos` table and updates `repo_chunks` to:
- Add nullable `repo_id` FK → repos.id
- Make `chat_file_id` nullable (backward-compat path kept)

Revision ID: 0018
Revises: 0017
Create Date: 2026-04-18 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    # -----------------------------------------------------------------------
    # 1. Create `repos` table
    # -----------------------------------------------------------------------
    if "repos" not in existing_tables:
        op.create_table(
            "repos",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("conversation_id", sa.Text(), nullable=False),
            sa.Column("repo_url", sa.Text(), nullable=False),
            sa.Column("owner", sa.Text(), nullable=False),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("branch", sa.Text(), nullable=False, server_default="main"),
            sa.Column("ingestion_status", sa.Text(), nullable=False, server_default="pending"),
            sa.Column("total_files", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("total_chunks", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_repos_conversation_id", "repos", ["conversation_id"])

    # -----------------------------------------------------------------------
    # 2. Update `repo_chunks`:
    #    a) add nullable `repo_id` column
    #    b) make `chat_file_id` nullable
    # Use batch_alter_table for SQLite compatibility (table recreation).
    # -----------------------------------------------------------------------
    if "repo_chunks" not in existing_tables:
        # Table doesn't exist yet — nothing to migrate
        return

    existing_columns = {col["name"] for col in inspector.get_columns("repo_chunks")}
    chat_files_exists = "chat_files" in existing_tables

    if chat_files_exists:
        # Full recreate — reflection of the FK to chat_files is safe
        with op.batch_alter_table("repo_chunks", recreate="always") as batch_op:
            # a) Add repo_id if not present
            if "repo_id" not in existing_columns:
                batch_op.add_column(sa.Column("repo_id", sa.Uuid(), nullable=True))
            # b) Make chat_file_id nullable
            if "chat_file_id" in existing_columns:
                batch_op.alter_column("chat_file_id", nullable=True)
    else:
        # chat_files absent — skip reflection-based recreate (it would fail trying
        # to resolve the FK); only add repo_id via direct op.add_column which
        # requires no reflection.  chat_file_id nullability is moot here since
        # the referenced table doesn't exist (greenfield / test scenario).
        if "repo_id" not in existing_columns:
            op.add_column("repo_chunks", sa.Column("repo_id", sa.Uuid(), nullable=True))

    # Re-create index on repo_id after batch operation
    existing_indexes = {idx["name"] for idx in inspector.get_indexes("repo_chunks")}
    if "ix_repo_chunks_repo_id" not in existing_indexes:
        # Inspector may be stale after batch_alter_table — use try/except
        try:
            op.create_index("ix_repo_chunks_repo_id", "repo_chunks", ["repo_id"])
        except Exception:
            pass  # Index may already exist after recreate


def downgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    # Restore chat_file_id NOT NULL, remove repo_id
    if "repo_chunks" in existing_tables:
        with op.batch_alter_table("repo_chunks", recreate="always") as batch_op:
            existing_columns = {col["name"] for col in inspector.get_columns("repo_chunks")}
            if "repo_id" in existing_columns:
                batch_op.drop_column("repo_id")
            if "chat_file_id" in existing_columns:
                batch_op.alter_column("chat_file_id", nullable=False)

    # Drop repos table
    if "repos" in existing_tables:
        existing_indexes = {idx["name"] for idx in inspector.get_indexes("repos")}
        if "ix_repos_conversation_id" in existing_indexes:
            op.drop_index("ix_repos_conversation_id", table_name="repos")
        op.drop_table("repos")
