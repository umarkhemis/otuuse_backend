"""add photo urls to deliveries

Revision ID: 004
Revises: 003
Create Date: 2026-07-15
"""
from alembic import op
import sqlalchemy as sa

revision = '004'
down_revision = '003'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'deliveries',
        sa.Column('passenger_photo_url', sa.Text, nullable=True),
    )
    op.add_column(
        'deliveries',
        sa.Column('admin_photo_url', sa.Text, nullable=True),
    )


def downgrade():
    op.drop_column('deliveries', 'admin_photo_url')
    op.drop_column('deliveries', 'passenger_photo_url')
