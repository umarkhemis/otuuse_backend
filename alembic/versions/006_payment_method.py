"""add payment_method to rides

Revision ID: 006
Revises: 005
Create Date: 2026-08-20
"""
from alembic import op
import sqlalchemy as sa

revision = '006'
down_revision = '005'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'rides',
        sa.Column('payment_method', sa.String(10), nullable=True),  # 'cash' | 'momo'
    )


def downgrade():
    op.drop_column('rides', 'payment_method')
