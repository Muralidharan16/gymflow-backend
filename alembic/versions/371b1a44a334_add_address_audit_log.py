"""add_address_audit_log

Revision ID: 371b1a44a334
Revises: 371b1a44a333
Create Date: 2026-05-18T14:23:05Z

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

# revision identifiers, used by Alembic.
revision = '371b1a44a334'
down_revision = '371b1a44a333'
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table(
        'organization_address_audit_log',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('address_id', UUID(as_uuid=True), nullable=False),
        sa.Column('organization_id', UUID(as_uuid=True), nullable=False),
        sa.Column('changed_by', UUID(as_uuid=True), nullable=False),
        sa.Column('changed_at', sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('old_snapshot', JSONB, nullable=False),
        sa.Column('new_snapshot', JSONB, nullable=False),
        sa.Column('change_reason', sa.Text(), nullable=True),
        sa.Column('ip_address', sa.String(45), nullable=True)
    )

def downgrade() -> None:
    op.drop_table('organization_address_audit_log')
