"""Add canonical graph schema.

MQP-STEERING-CONTRACT: GRAPH-LAYER-CORRECTION v1.1
(Supersedes the original GRAPH-EXTRACTION-LAYER v1.0 design)

Creates the single-authority canonical graph tables directly:

  repo_files        — canonical file identity
  code_symbols      — canonical symbol identity (function | class)
  file_dependencies — resolved file→file dependency edges (NEVER NULL target)
  symbol_call_edges — symbol→symbol call edges (source always a valid FK)
  entry_points      — detected execution entry points (main | server | framework)

Legacy tables (file_nodes, symbol_nodes, file_edges) are NOT created by this
migration — they were superseded before this branch reached production.

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
    # repo_files — canonical file identity
    # ------------------------------------------------------------------
    if "repo_files" not in existing_tables:
        op.create_table(
            "repo_files",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("repo_id", sa.Uuid(), nullable=False),
            sa.Column("path", sa.Text(), nullable=False),
            sa.Column("language", sa.Text(), nullable=True),
            sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("content_hash", sa.Text(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("repo_id", "path", name="uq_repo_files_path"),
        )
        op.create_index("ix_repo_files_repo_id", "repo_files", ["repo_id"])
        op.create_index("ix_repo_files_path", "repo_files", ["path"])

    # ------------------------------------------------------------------
    # code_symbols — canonical symbol identity
    # ------------------------------------------------------------------
    if "code_symbols" not in existing_tables:
        op.create_table(
            "code_symbols",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("file_id", sa.Uuid(), nullable=False),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("symbol_type", sa.Text(), nullable=False),
            sa.Column("start_line", sa.Integer(), nullable=False),
            sa.Column("end_line", sa.Integer(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["file_id"], ["repo_files.id"]),
        )
        op.create_index("ix_code_symbols_file_id", "code_symbols", ["file_id"])

    # ------------------------------------------------------------------
    # file_dependencies — resolved file→file dependency edges
    # INVARIANT: target_file_id IS NEVER NULL
    # ------------------------------------------------------------------
    if "file_dependencies" not in existing_tables:
        op.create_table(
            "file_dependencies",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("source_file_id", sa.Uuid(), nullable=False),
            sa.Column("target_file_id", sa.Uuid(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["source_file_id"], ["repo_files.id"]),
            sa.ForeignKeyConstraint(["target_file_id"], ["repo_files.id"]),
        )
        op.create_index(
            "ix_file_dependencies_source_file_id", "file_dependencies", ["source_file_id"]
        )
        op.create_index(
            "ix_file_dependencies_target_file_id", "file_dependencies", ["target_file_id"]
        )

    # ------------------------------------------------------------------
    # symbol_call_edges — symbol→symbol call edges
    # source_symbol_id is NEVER NULL
    # ------------------------------------------------------------------
    if "symbol_call_edges" not in existing_tables:
        op.create_table(
            "symbol_call_edges",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("source_symbol_id", sa.Uuid(), nullable=False),
            sa.Column("callee_name", sa.Text(), nullable=False),
            sa.Column("target_symbol_id", sa.Uuid(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["source_symbol_id"], ["code_symbols.id"]),
            sa.ForeignKeyConstraint(["target_symbol_id"], ["code_symbols.id"]),
        )
        op.create_index(
            "ix_symbol_call_edges_source_symbol_id",
            "symbol_call_edges",
            ["source_symbol_id"],
        )
        op.create_index(
            "ix_symbol_call_edges_target_symbol_id",
            "symbol_call_edges",
            ["target_symbol_id"],
        )

    # ------------------------------------------------------------------
    # entry_points — detected execution entry points
    # ------------------------------------------------------------------
    if "entry_points" not in existing_tables:
        op.create_table(
            "entry_points",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("file_id", sa.Uuid(), nullable=False),
            sa.Column("entry_type", sa.Text(), nullable=False),
            sa.Column("line", sa.Integer(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["file_id"], ["repo_files.id"]),
        )
        op.create_index("ix_entry_points_file_id", "entry_points", ["file_id"])


def downgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    for table, index_names in [
        ("entry_points", ["ix_entry_points_file_id"]),
        (
            "symbol_call_edges",
            [
                "ix_symbol_call_edges_source_symbol_id",
                "ix_symbol_call_edges_target_symbol_id",
            ],
        ),
        (
            "file_dependencies",
            [
                "ix_file_dependencies_source_file_id",
                "ix_file_dependencies_target_file_id",
            ],
        ),
        ("code_symbols", ["ix_code_symbols_file_id"]),
        ("repo_files", ["ix_repo_files_path", "ix_repo_files_repo_id"]),
    ]:
        if table in existing_tables:
            existing_indexes = {ix["name"] for ix in inspector.get_indexes(table)}
            for ix_name in index_names:
                if ix_name in existing_indexes:
                    op.drop_index(ix_name, table_name=table)
            op.drop_table(table)
