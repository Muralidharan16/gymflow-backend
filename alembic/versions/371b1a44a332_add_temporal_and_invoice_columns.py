"""add_temporal_and_invoice_columns

Revision ID: 371b1a44a332
Revises: 371b1a44a331
Create Date: 2026-05-18T14:21:05Z

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision = '371b1a44a332'
down_revision = '371b1a44a331'
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column('invoices', sa.Column('billing_address_snapshot', JSONB, nullable=True))
    
    op.add_column('organization_addresses', sa.Column('effective_from', sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('now()')))
    op.add_column('organization_addresses', sa.Column('effective_until', sa.TIMESTAMP(timezone=True), nullable=True))

def downgrade() -> None:
    op.drop_column('organization_addresses', 'effective_until')
    op.drop_column('organization_addresses', 'effective_from')
    op.drop_column('invoices', 'billing_address_snapshot')
