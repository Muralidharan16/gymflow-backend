"""add_membership_plans

Revision ID: 018a6ec2ddd4
Revises: 361c32e72e93
Create Date: 2026-06-07 01:10:11.259436

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '018a6ec2ddd4'
down_revision: Union[str, Sequence[str], None] = '361c32e72e93'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add default_currency_code to organizations
    op.add_column('organizations', sa.Column('default_currency_code', sa.String(length=3), server_default=sa.text("'INR'"), nullable=False))

    # 2. Create enums for MembershipPlan
    op.execute("CREATE TYPE plan_status AS ENUM ('active', 'inactive', 'archived');")
    op.execute("CREATE TYPE duration_unit AS ENUM ('days', 'months', 'years');")

    # 3. Create membership_plans table
    op.create_table('membership_plans',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('branch_id', sa.UUID(), nullable=True),
    sa.Column('plan_code', sa.String(length=50), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('price', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('duration_value', sa.Integer(), nullable=False),
    sa.Column('duration_unit', postgresql.ENUM('days', 'months', 'years', name='duration_unit', create_type=False), nullable=False),
    sa.Column('max_members', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('valid_from', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('valid_until', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('status', postgresql.ENUM('active', 'inactive', 'archived', name='plan_status', create_type=False), server_default=sa.text("'active'"), nullable=False),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('archived_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('duration_value > 0', name='chk_plan_duration_positive'),
    sa.CheckConstraint('max_members >= 1', name='chk_plan_max_members_positive'),
    sa.CheckConstraint('price >= 0', name='chk_plan_price_positive'),
    sa.ForeignKeyConstraint(['branch_id'], ['org_branches.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('org_id', 'plan_code', name='uix_org_plan_code')
    )
    op.create_index('ix_membership_plans_branch_id', 'membership_plans', ['branch_id'], unique=False)
    op.create_index('ix_membership_plans_org_id', 'membership_plans', ['org_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_membership_plans_org_id', table_name='membership_plans')
    op.drop_index('ix_membership_plans_branch_id', table_name='membership_plans')
    op.drop_table('membership_plans')
    
    op.execute("DROP TYPE IF EXISTS duration_unit;")
    op.execute("DROP TYPE IF EXISTS plan_status;")

    op.drop_column('organizations', 'default_currency_code')
