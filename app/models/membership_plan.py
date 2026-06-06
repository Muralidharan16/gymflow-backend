import uuid
import enum
from datetime import datetime
from sqlalchemy import String, Text, ForeignKey, Numeric, Integer, Index, text, CheckConstraint, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP
from sqlalchemy import Enum as SAEnum

from app.models.base import Base, TimestampMixin, new_uuid

class PlanStatus(str, enum.Enum):
    active = "active"
    inactive = "inactive"
    archived = "archived"

class DurationUnit(str, enum.Enum):
    days = "days"
    months = "months"
    years = "years"

class MembershipPlan(Base, TimestampMixin):
    __tablename__ = "membership_plans"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    branch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("org_branches.id", ondelete="CASCADE"), nullable=True)
    
    plan_code: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    
    price: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    
    duration_value: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_unit: Mapped[DurationUnit] = mapped_column(SAEnum(DurationUnit, name="duration_unit", create_constraint=False), nullable=False)
    
    max_members: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"), nullable=False)
    
    valid_from: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    
    status: Mapped[PlanStatus] = mapped_column(SAEnum(PlanStatus, name="plan_status", create_constraint=False), default=PlanStatus.active, server_default=text("'active'"), nullable=False)
    
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("org_id", "plan_code", name="uix_org_plan_code"),
        CheckConstraint("price >= 0", name="chk_plan_price_positive"),
        CheckConstraint("duration_value > 0", name="chk_plan_duration_positive"),
        CheckConstraint("max_members >= 1", name="chk_plan_max_members_positive"),
        Index("ix_membership_plans_org_id", "org_id"),
        Index("ix_membership_plans_branch_id", "branch_id"),
    )
