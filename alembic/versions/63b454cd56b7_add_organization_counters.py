"""add_organization_counters

Revision ID: 63b454cd56b7
Revises: 018a6ec2ddd4
Create Date: 2026-06-07 01:24:43.702214

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '63b454cd56b7'
down_revision: Union[str, Sequence[str], None] = '018a6ec2ddd4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('organization_counters',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('counter_key', sa.String(length=50), nullable=False),
    sa.Column('current_value', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('org_id', 'counter_key', name='uix_org_counter_key')
    )
    op.create_index(op.f('ix_organization_counters_org_id'), 'organization_counters', ['org_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_organization_counters_org_id'), table_name='organization_counters')
    op.drop_table('organization_counters')
