import uuid
from datetime import date, datetime
from decimal import Decimal
# pyrefly: ignore [missing-import]
from sqlalchemy import (
    String, Boolean, Integer, Date, Text, ForeignKey, Numeric, Index,
)
# pyrefly: ignore [missing-import]
from sqlalchemy.orm import Mapped, mapped_column
# pyrefly: ignore [missing-import]
from sqlalchemy.dialects.postgresql import UUID, JSONB, TIMESTAMP
# pyrefly: ignore [missing-import]
from sqlalchemy import Enum as SAEnum

from app.models.base import Base, TimestampMixin, new_uuid
from app.models.enums import SubscriptionStatus, FreezeStatus


class SubscriptionPlan(Base, TimestampMixin):
    __tablename__ = "subscription_plans"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    gym_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gyms.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_days: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    max_freeze_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    features: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (
        Index("ix_subscription_plans_gym_id", "gym_id"),
    )


class MemberSubscription(Base, TimestampMixin):
    __tablename__ = "member_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    gym_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gyms.id", ondelete="CASCADE"),
        nullable=False,
    )
    member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("members.id", ondelete="CASCADE"),
        nullable=False,
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscription_plans.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gym_owners.id", ondelete="SET NULL"),
        nullable=True,
    )
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    freeze_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    freeze_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    total_freeze_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[SubscriptionStatus] = mapped_column(
        SAEnum(SubscriptionStatus, name="subscriptionstatus", create_constraint=False),
        default=SubscriptionStatus.active,
        nullable=False,
    )
    reminder_sent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    mandate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_member_subs_gym_status_end", "gym_id", "status", "end_date"),
        Index("ix_member_subs_member_status", "member_id", "status"),
        Index("ix_member_subs_gym_id", "gym_id"),
    )


class MemberFreezeLog(Base, TimestampMixin):
    __tablename__ = "member_freeze_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    gym_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gyms.id", ondelete="CASCADE"),
        nullable=False,
    )
    member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("members.id", ondelete="CASCADE"),
        nullable=False,
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("member_subscriptions.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gym_owners.id", ondelete="SET NULL"),
        nullable=True,
    )
    freeze_start: Mapped[date] = mapped_column(Date, nullable=False)
    freeze_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[FreezeStatus] = mapped_column(
        SAEnum(FreezeStatus, name="freezestatus", create_constraint=False),
        default=FreezeStatus.requested,
        nullable=False,
    )

    __table_args__ = (
        Index("ix_freeze_logs_member_sub", "member_id", "subscription_id"),
    )
