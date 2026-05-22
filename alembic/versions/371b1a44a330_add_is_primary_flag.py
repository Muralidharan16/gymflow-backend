"""add_is_primary_flag

Revision ID: 371b1a44a330
Revises: 371b1a44a329
Create Date: 2026-05-18T14:19:10Z

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '371b1a44a330'
down_revision = '371b1a44a329'
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column('organization_addresses', sa.Column('is_primary', sa.Boolean(), nullable=False, server_default=sa.text('FALSE')))
    op.create_index(
        'uq_org_primary_address',
        'organization_addresses',
        ['org_id'],
        unique=True,
        postgresql_where=sa.text('is_primary = TRUE AND deleted_at IS NULL')
    )
    
    op.add_column('member_addresses', sa.Column('is_primary', sa.Boolean(), nullable=False, server_default=sa.text('FALSE')))
    op.create_index(
        'uq_member_primary_address',
        'member_addresses',
        ['member_id'],
        unique=True,
        postgresql_where=sa.text('is_primary = TRUE AND deleted_at IS NULL')
    )

def downgrade() -> None:
    op.drop_index('uq_member_primary_address', 'member_addresses')
    op.drop_column('member_addresses', 'is_primary')
    op.drop_index('uq_org_primary_address', 'organization_addresses')
    op.drop_column('organization_addresses', 'is_primary')
