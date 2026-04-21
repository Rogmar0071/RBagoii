"""Graph layer correction — no-op migration (canonical tables created in 0026).

MQP-STEERING-CONTRACT: GRAPH-LAYER-CORRECTION v1.1

Migration 0026 was updated to create the canonical graph tables directly
(repo_files, code_symbols, file_dependencies, symbol_call_edges, entry_points).
This migration exists to preserve the revision chain and documents the
correction contract.

Revision ID: 0027
Revises: 0026
Create Date: 2026-04-21 00:00:01.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # All canonical graph tables were created in migration 0026.
    # This migration is intentionally a no-op.
    pass


def downgrade() -> None:
    # All canonical graph tables are removed in migration 0026 downgrade.
    pass
