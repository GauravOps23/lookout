"""view_runs.rows_blob: gzip of the full result rows when a run returned more than the stored preview

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('view_runs', sa.Column('rows_blob', sa.LargeBinary(), nullable=True))


def downgrade() -> None:
    op.drop_column('view_runs', 'rows_blob')
