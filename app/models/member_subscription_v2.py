import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, new_uuid
from app.models.membership_plan import DurationUnit


class ModernSubscriptionStatus(str, enum.Enum):
    pending = "pending"
    active = "active"
    expired = "expired"
    cancelled = "cancelled"
    frozen = "frozen"
    archived = "archived"


class SubscriptionMemberRole(str, enum.Enum):
    primary = "primary"
    additional = "additional"


class MemberSubscriptionV2(Base, TimestampMixin):
    __tablename__ = "member_subscriptions_v2"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("org_branches.id", ondelete="RESTRICT"), nullable=False)
    membership_plan_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("membership_plans.id", ondelete="RESTRICT"), nullable=False)
    primary_member_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("members.id", ondelete="RESTRICT"), nullable=False)

    subscription_code: Mapped[str] = mapped_column(String(50), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[ModernSubscriptionStatus] = mapped_column(
        SAEnum(ModernSubscriptionStatus, name="modern_subscription_status", create_constraint=False),
        default=ModernSubscriptionStatus.active,
        server_default=text("'active'"),
        nullable=False,
    )

    price_snapshot: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    duration_value_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_unit_snapshot: Mapped[DurationUnit] = mapped_column(
        SAEnum(DurationUnit, name="duration_unit", create_constraint=False),
        nullable=False,
    )
    max_members_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)

    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("org_id", "subscription_code", name="uix_org_subscription_code_v2"),
        Index("ix_member_subscriptions_v2_org_status", "org_id", "status"),
        Index("ix_member_subscriptions_v2_org_branch", "org_id", "branch_id"),
        Index("ix_member_subscriptions_v2_org_primary_member", "org_id", "primary_member_id"),
        Index("ix_member_subscriptions_v2_org_code", "org_id", "subscription_code"),
    )


class SubscriptionMember(Base, TimestampMixin):
    __tablename__ = "subscription_members"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    subscription_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("member_subscriptions_v2.id", ondelete="CASCADE"), nullable=False)
    member_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("members.id", ondelete="RESTRICT"), nullable=False)
    slot_number: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[SubscriptionMemberRole] = mapped_column(
        SAEnum(SubscriptionMemberRole, name="subscription_member_role", create_constraint=False),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"), nullable=False)
    joined_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"), nullable=False)
    left_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("subscription_id", "slot_number", name="uix_subscription_slot_number"),
        UniqueConstraint("subscription_id", "member_id", name="uix_subscription_member_once"),
        Index("ix_subscription_members_subscription_slot", "subscription_id", "slot_number"),
        Index("ix_subscription_members_org_member", "org_id", "member_id"),
    )
