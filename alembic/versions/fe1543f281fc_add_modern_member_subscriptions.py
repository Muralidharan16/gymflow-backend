"""add modern member subscriptions

Revision ID: fe1543f281fc
Revises: 157e09159795
Create Date: 2026-06-11 12:53:57.493479

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "fe1543f281fc"
down_revision: Union[str, Sequence[str], None] = "157e09159795"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "CREATE TYPE modern_subscription_status AS ENUM "
        "('pending', 'active', 'expired', 'cancelled', 'frozen', 'archived')"
    )
    op.execute("CREATE TYPE subscription_member_role AS ENUM ('primary', 'additional')")

    op.create_table(
        "member_subscriptions_v2",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("branch_id", sa.UUID(), nullable=False),
        sa.Column("membership_plan_id", sa.UUID(), nullable=False),
        sa.Column("primary_member_id", sa.UUID(), nullable=False),
        sa.Column("subscription_code", sa.String(length=50), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "pending",
                "active",
                "expired",
                "cancelled",
                "frozen",
                "archived",
                name="modern_subscription_status",
                create_type=False,
            ),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column("price_snapshot", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency_code", sa.String(length=3), nullable=False),
        sa.Column("duration_value_snapshot", sa.Integer(), nullable=False),
        sa.Column(
            "duration_unit_snapshot",
            postgresql.ENUM("days", "months", "years", name="duration_unit", create_type=False),
            nullable=False,
        ),
        sa.Column("max_members_snapshot", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("updated_by", sa.UUID(), nullable=True),
        sa.Column("cancelled_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("archived_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["branch_id"], ["org_branches.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["membership_plan_id"], ["membership_plans.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["primary_member_id"], ["members.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "subscription_code", name="uix_org_subscription_code_v2"),
    )
    op.create_index(
        "ix_member_subscriptions_v2_org_status",
        "member_subscriptions_v2",
        ["org_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_member_subscriptions_v2_org_branch",
        "member_subscriptions_v2",
        ["org_id", "branch_id"],
        unique=False,
    )
    op.create_index(
        "ix_member_subscriptions_v2_org_primary_member",
        "member_subscriptions_v2",
        ["org_id", "primary_member_id"],
        unique=False,
    )
    op.create_index(
        "ix_member_subscriptions_v2_org_code",
        "member_subscriptions_v2",
        ["org_id", "subscription_code"],
        unique=False,
    )

    op.create_table(
        "subscription_members",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("subscription_id", sa.UUID(), nullable=False),
        sa.Column("member_id", sa.UUID(), nullable=False),
        sa.Column("slot_number", sa.Integer(), nullable=False),
        sa.Column(
            "role",
            postgresql.ENUM("primary", "additional", name="subscription_member_role", create_type=False),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("joined_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("left_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["member_id"], ["members.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["subscription_id"], ["member_subscriptions_v2.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("subscription_id", "slot_number", name="uix_subscription_slot_number"),
        sa.UniqueConstraint("subscription_id", "member_id", name="uix_subscription_member_once"),
    )
    op.create_index(
        "ix_subscription_members_subscription_slot",
        "subscription_members",
        ["subscription_id", "slot_number"],
        unique=False,
    )
    op.create_index(
        "ix_subscription_members_org_member",
        "subscription_members",
        ["org_id", "member_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_subscription_members_org_member", table_name="subscription_members")
    op.drop_index("ix_subscription_members_subscription_slot", table_name="subscription_members")
    op.drop_table("subscription_members")

    op.drop_index("ix_member_subscriptions_v2_org_code", table_name="member_subscriptions_v2")
    op.drop_index("ix_member_subscriptions_v2_org_primary_member", table_name="member_subscriptions_v2")
    op.drop_index("ix_member_subscriptions_v2_org_branch", table_name="member_subscriptions_v2")
    op.drop_index("ix_member_subscriptions_v2_org_status", table_name="member_subscriptions_v2")
    op.drop_table("member_subscriptions_v2")

    op.execute("DROP TYPE IF EXISTS subscription_member_role")
    op.execute("DROP TYPE IF EXISTS modern_subscription_status")
