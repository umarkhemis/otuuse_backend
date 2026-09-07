"""add password auth fields to users

Revision ID: 005
Revises: 004
Create Date: 2026-07-20
"""
from alembic import op
import sqlalchemy as sa

revision = '005'
down_revision = '004'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'users',
        sa.Column('password_hash', sa.String(200), nullable=True),
    )
    op.add_column(
        'users',
        sa.Column('must_change_password', sa.Boolean, server_default='false', nullable=False),
    )


def downgrade():
    op.drop_column('users', 'must_change_password')
    op.drop_column('users', 'password_hash')
