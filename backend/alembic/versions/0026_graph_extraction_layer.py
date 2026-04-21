"""Add graph extraction layer tables.

MQP-CONTRACT: GRAPH-EXTRACTION-LAYER v1.0

Adds:
  file_nodes    — one row per ingested file (path, language, hash, size)
  symbol_nodes  — named symbols (functions, classes) extracted from files
  file_edges    — directed import/dependency edges between files

Revision ID: 0026
Revises: 0025
Create Date: 2026-04-21 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    # ------------------------------------------------------------------
    # file_nodes
    # ------------------------------------------------------------------
    if "file_nodes" not in existing_tables:
        op.create_table(
            "file_nodes",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("repo_id", sa.Uuid(), nullable=False),
            sa.Column("path", sa.Text(), nullable=False),
            sa.Column("language", sa.Text(), nullable=True),
            sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("content_hash", sa.Text(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("repo_id", "path", name="uq_repo_file_path"),
        )
        op.create_index("ix_file_nodes_repo_id", "file_nodes", ["repo_id"])
        op.create_index("ix_file_nodes_path", "file_nodes", ["path"])

    # ------------------------------------------------------------------
    # symbol_nodes
    # ------------------------------------------------------------------
    if "symbol_nodes" not in existing_tables:
        op.create_table(
            "symbol_nodes",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("file_id", sa.Uuid(), nullable=False),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("kind", sa.Text(), nullable=False),
            sa.Column("start_line", sa.Integer(), nullable=False),
            sa.Column("end_line", sa.Integer(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["file_id"], ["file_nodes.id"]),
        )
        op.create_index("ix_symbol_nodes_file_id", "symbol_nodes", ["file_id"])

    # ------------------------------------------------------------------
    # file_edges
    # ------------------------------------------------------------------
    if "file_edges" not in existing_tables:
        op.create_table(
            "file_edges",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("source_file_id", sa.Uuid(), nullable=False),
            sa.Column("target_path", sa.Text(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["source_file_id"], ["file_nodes.id"]),
        )
        op.create_index("ix_file_edges_source_file_id", "file_edges", ["source_file_id"])


def downgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    if "file_edges" in existing_tables:
        op.drop_index("ix_file_edges_source_file_id", table_name="file_edges")
        op.drop_table("file_edges")

    if "symbol_nodes" in existing_tables:
        op.drop_index("ix_symbol_nodes_file_id", table_name="symbol_nodes")
        op.drop_table("symbol_nodes")

    if "file_nodes" in existing_tables:
        op.drop_index("ix_file_nodes_path", table_name="file_nodes")
        op.drop_index("ix_file_nodes_repo_id", table_name="file_nodes")
        op.drop_table("file_nodes")
