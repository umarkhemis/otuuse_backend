"""add plate_number to driver_profiles

Revision ID: 003
Revises: 002
Create Date: 2026-07-10
"""
from alembic import op
import sqlalchemy as sa

revision = '003'
down_revision = '002'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'driver_profiles',
        sa.Column('plate_number', sa.String(20), nullable=True),
    )


def downgrade():
    op.drop_column('driver_profiles', 'plate_number')
