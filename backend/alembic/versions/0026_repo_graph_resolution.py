"""REPO_GRAPH_RESOLUTION_V1 — execution graph tables.

Adds:
  repo_files         — one row per ingested file in a repo IngestJob
  code_symbols       — named symbols (functions/classes) extracted per file
  file_dependencies  — resolved import edges between repo_files
  symbol_call_edges  — function/method call edges between code_symbols
  entry_points       — detected entry-point files per repo IngestJob

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
    dialect = bind.dialect.name

    # ------------------------------------------------------------------
    # repo_files
    # ------------------------------------------------------------------
    if "repo_files" not in existing_tables:
        op.create_table(
            "repo_files",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("ingest_job_id", sa.Uuid(), nullable=False),
            sa.Column("file_path", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_repo_files_ingest_job_id", "repo_files", ["ingest_job_id"])
        if dialect != "sqlite":
            op.create_foreign_key(
                "fk_repo_files_ingest_job_id",
                "repo_files", "ingest_jobs",
                ["ingest_job_id"], ["id"],
            )

    # ------------------------------------------------------------------
    # code_symbols
    # ------------------------------------------------------------------
    if "code_symbols" not in existing_tables:
        op.create_table(
            "code_symbols",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("file_id", sa.Uuid(), nullable=False),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("symbol_type", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_code_symbols_file_id", "code_symbols", ["file_id"])
        op.create_index("ix_code_symbols_name", "code_symbols", ["name"])
        if dialect != "sqlite":
            op.create_foreign_key(
                "fk_code_symbols_file_id",
                "code_symbols", "repo_files",
                ["file_id"], ["id"],
            )

    # ------------------------------------------------------------------
    # file_dependencies
    # ------------------------------------------------------------------
    if "file_dependencies" not in existing_tables:
        op.create_table(
            "file_dependencies",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("source_file_id", sa.Uuid(), nullable=False),
            sa.Column("target_file_id", sa.Uuid(), nullable=False),
            sa.Column("import_path", sa.Text(), nullable=False),
            sa.Column("is_resolved", sa.Boolean(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_file_dependencies_source_file_id", "file_dependencies", ["source_file_id"]
        )
        op.create_index(
            "ix_file_dependencies_target_file_id", "file_dependencies", ["target_file_id"]
        )
        if dialect != "sqlite":
            op.create_foreign_key(
                "fk_file_dependencies_source",
                "file_dependencies", "repo_files",
                ["source_file_id"], ["id"],
            )
            op.create_foreign_key(
                "fk_file_dependencies_target",
                "file_dependencies", "repo_files",
                ["target_file_id"], ["id"],
            )

    # ------------------------------------------------------------------
    # symbol_call_edges
    # ------------------------------------------------------------------
    if "symbol_call_edges" not in existing_tables:
        op.create_table(
            "symbol_call_edges",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("source_symbol_id", sa.Uuid(), nullable=False),
            sa.Column("target_symbol_name", sa.Text(), nullable=False),
            sa.Column("target_file_id", sa.Uuid(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_symbol_call_edges_source_symbol_id",
            "symbol_call_edges", ["source_symbol_id"],
        )
        op.create_index(
            "ix_symbol_call_edges_target_symbol_name",
            "symbol_call_edges", ["target_symbol_name"],
        )
        op.create_index(
            "ix_symbol_call_edges_target_file_id",
            "symbol_call_edges", ["target_file_id"],
        )
        if dialect != "sqlite":
            op.create_foreign_key(
                "fk_symbol_call_edges_source_symbol",
                "symbol_call_edges", "code_symbols",
                ["source_symbol_id"], ["id"],
            )
            op.create_foreign_key(
                "fk_symbol_call_edges_target_file",
                "symbol_call_edges", "repo_files",
                ["target_file_id"], ["id"],
            )

    # ------------------------------------------------------------------
    # entry_points
    # ------------------------------------------------------------------
    if "entry_points" not in existing_tables:
        op.create_table(
            "entry_points",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("ingest_job_id", sa.Uuid(), nullable=False),
            sa.Column("file_id", sa.Uuid(), nullable=False),
            sa.Column("entry_type", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_entry_points_ingest_job_id", "entry_points", ["ingest_job_id"])
        op.create_index("ix_entry_points_file_id", "entry_points", ["file_id"])
        if dialect != "sqlite":
            op.create_foreign_key(
                "fk_entry_points_ingest_job_id",
                "entry_points", "ingest_jobs",
                ["ingest_job_id"], ["id"],
            )
            op.create_foreign_key(
                "fk_entry_points_file_id",
                "entry_points", "repo_files",
                ["file_id"], ["id"],
            )


def downgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()
    dialect = bind.dialect.name

    if "entry_points" in existing_tables:
        if dialect != "sqlite":
            op.drop_constraint("fk_entry_points_file_id", "entry_points", type_="foreignkey")
            op.drop_constraint(
                "fk_entry_points_ingest_job_id", "entry_points", type_="foreignkey"
            )
        op.drop_index("ix_entry_points_file_id", table_name="entry_points")
        op.drop_index("ix_entry_points_ingest_job_id", table_name="entry_points")
        op.drop_table("entry_points")

    if "symbol_call_edges" in existing_tables:
        if dialect != "sqlite":
            op.drop_constraint(
                "fk_symbol_call_edges_target_file", "symbol_call_edges", type_="foreignkey"
            )
            op.drop_constraint(
                "fk_symbol_call_edges_source_symbol", "symbol_call_edges", type_="foreignkey"
            )
        op.drop_index("ix_symbol_call_edges_target_file_id", table_name="symbol_call_edges")
        op.drop_index(
            "ix_symbol_call_edges_target_symbol_name", table_name="symbol_call_edges"
        )
        op.drop_index(
            "ix_symbol_call_edges_source_symbol_id", table_name="symbol_call_edges"
        )
        op.drop_table("symbol_call_edges")

    if "file_dependencies" in existing_tables:
        if dialect != "sqlite":
            op.drop_constraint(
                "fk_file_dependencies_target", "file_dependencies", type_="foreignkey"
            )
            op.drop_constraint(
                "fk_file_dependencies_source", "file_dependencies", type_="foreignkey"
            )
        op.drop_index(
            "ix_file_dependencies_target_file_id", table_name="file_dependencies"
        )
        op.drop_index(
            "ix_file_dependencies_source_file_id", table_name="file_dependencies"
        )
        op.drop_table("file_dependencies")

    if "code_symbols" in existing_tables:
        if dialect != "sqlite":
            op.drop_constraint("fk_code_symbols_file_id", "code_symbols", type_="foreignkey")
        op.drop_index("ix_code_symbols_name", table_name="code_symbols")
        op.drop_index("ix_code_symbols_file_id", table_name="code_symbols")
        op.drop_table("code_symbols")

    if "repo_files" in existing_tables:
        if dialect != "sqlite":
            op.drop_constraint(
                "fk_repo_files_ingest_job_id", "repo_files", type_="foreignkey"
            )
        op.drop_index("ix_repo_files_ingest_job_id", table_name="repo_files")
        op.drop_table("repo_files")
