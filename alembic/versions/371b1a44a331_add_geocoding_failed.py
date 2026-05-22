"""add_geocoding_failed

Revision ID: 371b1a44a331
Revises: 371b1a44a330
Create Date: 2026-05-18T14:20:01Z

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision = '371b1a44a331'
down_revision = '371b1a44a330'
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column('organization_addresses', sa.Column('geocoding_failed', sa.Boolean(), nullable=False, server_default=sa.text('FALSE')))
    op.add_column('member_addresses', sa.Column('geocoding_failed', sa.Boolean(), nullable=False, server_default=sa.text('FALSE')))
    
    op.create_table(
        'notifications',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('org_id', UUID(as_uuid=True), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('is_read', sa.Boolean(), nullable=False, server_default=sa.text('FALSE')),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('now()'))
    )

def downgrade() -> None:
    op.drop_table('notifications')
    op.drop_column('member_addresses', 'geocoding_failed')
    op.drop_column('organization_addresses', 'geocoding_failed')
