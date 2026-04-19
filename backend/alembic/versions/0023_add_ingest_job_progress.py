"""add ingest_job progress

Revision ID: 0023_add_ingest_job_progress
Revises: 0022_repo_chunk_graph_fields
Create Date: 2026-04-19

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '0023_add_ingest_job_progress'
down_revision = '0022_repo_chunk_graph_fields'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add progress column to ingest_jobs table
    with op.batch_alter_table('ingest_jobs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('progress', sa.Integer(), nullable=False, server_default='0'))


def downgrade() -> None:
    # Remove progress column from ingest_jobs table
    with op.batch_alter_table('ingest_jobs', schema=None) as batch_op:
        batch_op.drop_column('progress')
