"""GRAPH_RECONSTRUCTION_LAYER_V1 — graph reconstruction fields on repo_chunks.

Adds:
  repo_chunks.chunk_type    TEXT nullable (indexed)
  repo_chunks.graph_group   TEXT nullable (indexed)
  repo_chunks.symbol        TEXT nullable (indexed)
  repo_chunks.dependencies  JSON nullable
  repo_chunks.start_line    INTEGER nullable
  repo_chunks.end_line      INTEGER nullable

All columns are nullable so that existing chunks are unaffected and the
upgrade is fully backward compatible (no data migration required).

Revision ID: 0022
Revises: 0021
Create Date: 2026-04-19 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    if "repo_chunks" not in existing_tables:
        return

    existing_cols = {c["name"] for c in inspector.get_columns("repo_chunks")}

    new_columns = [
        ("chunk_type", sa.Column("chunk_type", sa.Text(), nullable=True)),
        ("graph_group", sa.Column("graph_group", sa.Text(), nullable=True)),
        ("symbol", sa.Column("symbol", sa.Text(), nullable=True)),
        ("dependencies", sa.Column("dependencies", sa.JSON(), nullable=True)),
        ("start_line", sa.Column("start_line", sa.Integer(), nullable=True)),
        ("end_line", sa.Column("end_line", sa.Integer(), nullable=True)),
    ]

    for col_name, col_def in new_columns:
        if col_name not in existing_cols:
            op.add_column("repo_chunks", col_def)

    # Indexes for the three frequently-queried text columns
    existing_indexes = {idx["name"] for idx in inspector.get_indexes("repo_chunks")}

    for index_name, col_name in [
        ("ix_repo_chunks_chunk_type", "chunk_type"),
        ("ix_repo_chunks_graph_group", "graph_group"),
        ("ix_repo_chunks_symbol", "symbol"),
    ]:
        if index_name not in existing_indexes and col_name not in existing_cols:
            op.create_index(index_name, "repo_chunks", [col_name])


def downgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)
    existing_cols = {c["name"] for c in inspector.get_columns("repo_chunks")}
    existing_indexes = {idx["name"] for idx in inspector.get_indexes("repo_chunks")}

    for index_name in (
        "ix_repo_chunks_symbol",
        "ix_repo_chunks_graph_group",
        "ix_repo_chunks_chunk_type",
    ):
        if index_name in existing_indexes:
            op.drop_index(index_name, table_name="repo_chunks")

    for col_name in (
        "end_line", "start_line", "dependencies",
        "symbol", "graph_group", "chunk_type",
    ):
        if col_name in existing_cols:
            op.drop_column("repo_chunks", col_name)
