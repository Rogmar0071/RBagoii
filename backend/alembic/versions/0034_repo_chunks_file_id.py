"""Add and enforce repo_chunks.file_id.

Revision ID: 0034
Revises: 0033
Create Date: 2026-04-23
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def _derive_file_id(row: sa.RowMapping, seed_columns: list[str]) -> str:
    for col in seed_columns:
        value = str(row.get(col) or "").strip()
        if value:
            return str(uuid.uuid5(uuid.NAMESPACE_URL, value))
    return str(uuid.uuid5(uuid.NAMESPACE_URL, str(row["id"])))


def upgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)

    if "repo_chunks" not in inspector.get_table_names():
        return

    existing_cols = {c["name"] for c in inspector.get_columns("repo_chunks")}

    # Step 1: Ensure file_id column exists (temporary nullable for safe backfill).
    if "file_id" not in existing_cols:
        op.add_column("repo_chunks", sa.Column("file_id", sa.Uuid(), nullable=True))

    # Refresh inspector after DDL changes.
    inspector = sa.inspect(bind)
    existing_cols = {c["name"] for c in inspector.get_columns("repo_chunks")}

    # Step 2: Backfill any legacy rows where file_id is still NULL.
    if "file_id" in existing_cols:
        seed_columns = [c for c in ("graph_group", "lineage") if c in existing_cols]
        select_cols = ", ".join(["id", *seed_columns])
        rows = bind.execute(
            sa.text(
                f"SELECT {select_cols} FROM repo_chunks WHERE file_id IS NULL"  # noqa: S608
            )
        ).mappings()

        for row in rows:
            bind.execute(
                sa.text(
                    """
                    UPDATE repo_chunks
                    SET file_id = :file_id
                    WHERE id = :id AND file_id IS NULL
                    """
                ),
                {"file_id": _derive_file_id(row, seed_columns), "id": row["id"]},
            )

    # Step 3: Ensure index exists.
    inspector = sa.inspect(bind)
    existing_indexes = {idx["name"] for idx in inspector.get_indexes("repo_chunks")}
    if "ix_repo_chunks_file_id" not in existing_indexes:
        op.create_index("ix_repo_chunks_file_id", "repo_chunks", ["file_id"])

    # Step 4: Enforce NOT NULL invariant.
    file_col = next(
        (c for c in inspector.get_columns("repo_chunks") if c["name"] == "file_id"),
        None,
    )
    if file_col and file_col.get("nullable", True):
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table("repo_chunks", recreate="always") as batch_op:
                batch_op.alter_column("file_id", existing_type=sa.Uuid(), nullable=False)
        else:
            op.alter_column(
                "repo_chunks",
                "file_id",
                existing_type=sa.Uuid(),
                nullable=False,
            )


def downgrade() -> None:
    bind = op.get_context().bind
    inspector = sa.inspect(bind)

    if "repo_chunks" not in inspector.get_table_names():
        return

    existing_cols = {c["name"] for c in inspector.get_columns("repo_chunks")}
    existing_indexes = {idx["name"] for idx in inspector.get_indexes("repo_chunks")}

    if "ix_repo_chunks_file_id" in existing_indexes:
        op.drop_index("ix_repo_chunks_file_id", table_name="repo_chunks")

    if "file_id" in existing_cols:
        op.drop_column("repo_chunks", "file_id")
