"""Graph layer correction — drop legacy tables, create canonical graph tables.

MQP-STEERING-CONTRACT: GRAPH-LAYER-CORRECTION v1.1

Removes the v1.0 graph tables (file_nodes, symbol_nodes, file_edges) and
creates the single-authority canonical graph schema:

  repo_files        — canonical file identity (replaces file_nodes)
  code_symbols      — canonical symbol identity (replaces symbol_nodes)
  file_dependencies — resolved file→file edges (target_file_id NEVER NULL)
  symbol_call_edges — symbol→symbol call edges (source always valid FK)
  entry_points      — detected execution entry points

Revision ID: 0027
Revises: 0026
Create Date: 2026-04-21 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()
    dialect = bind.dialect.name

    # ------------------------------------------------------------------
    # DROP legacy v1.0 graph tables (child FKs first).
    # ------------------------------------------------------------------

    if "file_edges" in existing_tables:
        existing_indexes = {ix["name"] for ix in inspector.get_indexes("file_edges")}
        if "ix_file_edges_source_file_id" in existing_indexes:
            op.drop_index("ix_file_edges_source_file_id", table_name="file_edges")
        op.drop_table("file_edges")

    if "symbol_nodes" in existing_tables:
        existing_indexes = {ix["name"] for ix in inspector.get_indexes("symbol_nodes")}
        if "ix_symbol_nodes_file_id" in existing_indexes:
            op.drop_index("ix_symbol_nodes_file_id", table_name="symbol_nodes")
        op.drop_table("symbol_nodes")

    if "file_nodes" in existing_tables:
        existing_indexes = {ix["name"] for ix in inspector.get_indexes("file_nodes")}
        for ix_name in ("ix_file_nodes_path", "ix_file_nodes_repo_id"):
            if ix_name in existing_indexes:
                op.drop_index(ix_name, table_name="file_nodes")
        op.drop_table("file_nodes")

    # ------------------------------------------------------------------
    # CREATE canonical graph tables.
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

    # Drop canonical tables (reverse dependency order)
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

    # Restore legacy v1.0 tables
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

    if "file_edges" not in existing_tables:
        op.create_table(
            "file_edges",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("source_file_id", sa.Uuid(), nullable=False),
            sa.Column("target_path", sa.Text(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["source_file_id"], ["file_nodes.id"]),
        )
        op.create_index(
            "ix_file_edges_source_file_id", "file_edges", ["source_file_id"]
        )
