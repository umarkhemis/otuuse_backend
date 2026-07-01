"""admin pin and audit log

Revision ID: 002
Revises: 001
Create Date: 2026-06-30
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '002'
down_revision = '001'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'users',
        sa.Column('pin_hash', sa.String(200), nullable=True),
    )

    op.create_table(
        'admin_audit_log',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('admin_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('action', sa.String(100), nullable=False),
        sa.Column('target_type', sa.String(50), nullable=True),
        sa.Column('target_id', sa.String(100), nullable=True),
        sa.Column('details', sa.Text, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('idx_audit_admin', 'admin_audit_log', ['admin_id'])
    op.create_index('idx_audit_created', 'admin_audit_log', ['created_at'])


def downgrade():
    op.drop_index('idx_audit_created', table_name='admin_audit_log')
    op.drop_index('idx_audit_admin', table_name='admin_audit_log')
    op.drop_table('admin_audit_log')
    op.drop_column('users', 'pin_hash')
