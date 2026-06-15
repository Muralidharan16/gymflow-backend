from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.subscription_lifecycle import (
    SubscriptionAssignmentStatus,
    SubscriptionEventType,
    SubscriptionFreezeStatus,
    SubscriptionIdempotencyState,
    SubscriptionSeriesStatus,
    SubscriptionSlotRole,
    SubscriptionTermSourceType,
    SubscriptionTermStatus,
)
from app.models.base import Base, TimestampMixin, new_uuid
from app.models.membership_plan import DurationUnit


class SubscriptionSeries(Base, TimestampMixin):
    __tablename__ = "subscription_series"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    originating_branch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    series_code: Mapped[str] = mapped_column(String(80), nullable=False)
    primary_member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("members.id", ondelete="RESTRICT"), nullable=False
    )
    lifecycle_status: Mapped[SubscriptionSeriesStatus] = mapped_column(
        SAEnum(SubscriptionSeriesStatus, name="subscription_series_status", create_constraint=False),
        server_default=text("'open'"),
        nullable=False,
    )
    opened_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"), nullable=False)
    opened_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    closed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    closure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    archived_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    archive_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSONB, server_default=text("'{}'::jsonb"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"), nullable=False)

    primary_member = relationship("Member", viewonly=True)
    originating_branch = relationship("OrgBranch", viewonly=True)
    terms = relationship(
        "SubscriptionTerm",
        back_populates="series",
        order_by="SubscriptionTerm.sequence_number",
        viewonly=True,
    )
    events = relationship(
        "SubscriptionEvent",
        back_populates="series",
        order_by="SubscriptionEvent.event_at.desc()",
        viewonly=True,
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["originating_branch_id", "org_id"],
            ["org_branches.id", "org_branches.org_id"],
            name="fk_subscription_series_origin_branch_org",
        ),
        UniqueConstraint("id", "org_id", name="uq_subscription_series_id_org"),
        UniqueConstraint("org_id", "series_code", name="uq_subscription_series_org_code"),
        Index("ix_subscription_series_org_status", "org_id", "lifecycle_status"),
        Index("ix_subscription_series_org_member", "org_id", "primary_member_id"),
        Index("ix_subscription_series_org_branch", "org_id", "originating_branch_id"),
    )


class SubscriptionTerm(Base):
    __tablename__ = "subscription_terms"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    series_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    term_code: Mapped[str] = mapped_column(String(80), nullable=False)
    renewed_from_term_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscription_terms.id", ondelete="RESTRICT"), nullable=True
    )
    source_type: Mapped[SubscriptionTermSourceType] = mapped_column(
        SAEnum(SubscriptionTermSourceType, name="subscription_term_source", create_constraint=False), nullable=False
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("membership_plans.id", ondelete="RESTRICT"), nullable=False
    )
    legacy_member_subscription_v2_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("member_subscriptions_v2.id", ondelete="RESTRICT"), nullable=True
    )
    legacy_subscription_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    plan_code_snapshot: Mapped[str] = mapped_column(String(50), nullable=False)
    plan_name_snapshot: Mapped[str] = mapped_column(String(255), nullable=False)
    duration_unit_snapshot: Mapped[DurationUnit] = mapped_column(
        SAEnum(DurationUnit, name="duration_unit", create_constraint=False), nullable=False
    )
    duration_value_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    capacity_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    list_price_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), server_default=text("0"), nullable=False)
    discount_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), server_default=text("0"), nullable=False)
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), server_default=text("0"), nullable=False)
    final_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), server_default=text("0"), nullable=False)
    starts_on: Mapped[date] = mapped_column(Date, nullable=False)
    base_ends_on: Mapped[date] = mapped_column(Date, nullable=False)
    effective_ends_on: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[SubscriptionTermStatus] = mapped_column(
        SAEnum(SubscriptionTermStatus, name="subscription_term_status", create_constraint=False), nullable=False
    )
    activated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    expired_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    cancelled_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    terminated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    terminated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    termination_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    voided_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    voided_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    void_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_metadata: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"), nullable=False)

    series = relationship("SubscriptionSeries", back_populates="terms", viewonly=True)
    branch = relationship("OrgBranch", viewonly=True)
    plan = relationship("MembershipPlan", viewonly=True)
    renewed_from_term = relationship(
        "SubscriptionTerm",
        remote_side=[id],
        foreign_keys=[renewed_from_term_id],
        viewonly=True,
    )
    slots = relationship(
        "SubscriptionTermSlot",
        back_populates="term",
        order_by="SubscriptionTermSlot.slot_index",
        viewonly=True,
    )
    freezes = relationship(
        "SubscriptionFreeze",
        back_populates="term",
        order_by="SubscriptionFreeze.requested_starts_on",
        viewonly=True,
    )
    events = relationship(
        "SubscriptionEvent",
        back_populates="term",
        order_by="SubscriptionEvent.event_at.desc()",
        viewonly=True,
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["series_id", "org_id"],
            ["subscription_series.id", "subscription_series.org_id"],
            name="fk_subscription_terms_series_org",
        ),
        ForeignKeyConstraint(
            ["branch_id", "org_id"],
            ["org_branches.id", "org_branches.org_id"],
            name="fk_subscription_terms_branch_org",
        ),
        UniqueConstraint("id", "org_id", name="uq_subscription_terms_id_org"),
        UniqueConstraint("org_id", "term_code", name="uq_subscription_terms_org_code"),
        UniqueConstraint("series_id", "sequence_number", name="uq_subscription_terms_series_sequence"),
        UniqueConstraint("legacy_member_subscription_v2_id", name="uq_subscription_terms_legacy_v2"),
        Index("ix_subscription_terms_org_status", "org_id", "status"),
        Index("ix_subscription_terms_org_branch", "org_id", "branch_id"),
        Index("ix_subscription_terms_series", "series_id", "sequence_number"),
        Index("ix_subscription_terms_legacy_source", "legacy_member_subscription_v2_id"),
        Index("ix_subscription_terms_org_plan", "org_id", "plan_id"),
    )


class SubscriptionTermSlot(Base):
    __tablename__ = "subscription_term_slots"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    term_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    slot_index: Mapped[int] = mapped_column(Integer, nullable=False)
    slot_role: Mapped[SubscriptionSlotRole] = mapped_column(
        SAEnum(SubscriptionSlotRole, name="subscription_slot_role", create_constraint=False),
        server_default=text("'standard'"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"), nullable=False)

    term = relationship("SubscriptionTerm", back_populates="slots", viewonly=True)
    assignments = relationship("SubscriptionSlotAssignment", back_populates="slot", viewonly=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["term_id", "org_id"],
            ["subscription_terms.id", "subscription_terms.org_id"],
            name="fk_subscription_term_slots_term_org",
        ),
        UniqueConstraint("id", "org_id", name="uq_subscription_term_slots_id_org"),
        UniqueConstraint("term_id", "slot_index", name="uq_subscription_term_slots_term_index"),
        Index("ix_subscription_term_slots_org_term", "org_id", "term_id"),
    )


class SubscriptionSlotAssignment(Base, TimestampMixin):
    __tablename__ = "subscription_slot_assignments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    term_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    term_slot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("members.id", ondelete="RESTRICT"), nullable=False
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    assignment_state: Mapped[SubscriptionAssignmentStatus] = mapped_column(
        SAEnum(SubscriptionAssignmentStatus, name="subscription_assignment_state", create_constraint=False),
        server_default=text("'active'"),
        nullable=False,
    )
    assigned_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"), nullable=False)
    assigned_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    released_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    release_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    term = relationship("SubscriptionTerm", viewonly=True)
    slot = relationship("SubscriptionTermSlot", back_populates="assignments", viewonly=True)
    member = relationship("Member", viewonly=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["term_id", "org_id"],
            ["subscription_terms.id", "subscription_terms.org_id"],
            name="fk_subscription_slot_assignments_term_org",
        ),
        ForeignKeyConstraint(
            ["term_slot_id", "org_id"],
            ["subscription_term_slots.id", "subscription_term_slots.org_id"],
            name="fk_subscription_slot_assignments_slot_org",
        ),
        Index("ix_subscription_slot_assignments_org_member", "org_id", "member_id"),
        Index("ix_subscription_slot_assignments_slot", "term_slot_id"),
        Index("ix_subscription_slot_assignments_term", "term_id"),
    )


class SubscriptionFreeze(Base, TimestampMixin):
    __tablename__ = "subscription_freezes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    series_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    term_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[SubscriptionFreezeStatus] = mapped_column(
        SAEnum(SubscriptionFreezeStatus, name="subscription_freeze_status", create_constraint=False), nullable=False
    )
    requested_starts_on: Mapped[date] = mapped_column(Date, nullable=False)
    planned_ends_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    actual_ended_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    extension_days: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    extension_policy: Mapped[str] = mapped_column(String(40), server_default=text("'extend_expiry'"), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"), nullable=False)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    resumed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    resumed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    cancelled_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    series = relationship("SubscriptionSeries", viewonly=True)
    term = relationship("SubscriptionTerm", back_populates="freezes", viewonly=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["series_id", "org_id"],
            ["subscription_series.id", "subscription_series.org_id"],
            name="fk_subscription_freezes_series_org",
        ),
        ForeignKeyConstraint(
            ["term_id", "org_id"],
            ["subscription_terms.id", "subscription_terms.org_id"],
            name="fk_subscription_freezes_term_org",
        ),
        Index("ix_subscription_freezes_org_term", "org_id", "term_id"),
        Index("ix_subscription_freezes_org_status", "org_id", "status"),
    )


class SubscriptionEvent(Base):
    __tablename__ = "subscription_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    branch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    series_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    term_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    event_type: Mapped[SubscriptionEventType] = mapped_column(
        SAEnum(SubscriptionEventType, name="subscription_event_type", create_constraint=False), nullable=False
    )
    event_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"), nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    event_source: Mapped[str] = mapped_column(String(50), server_default=text("'system'"), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSONB, server_default=text("'{}'::jsonb"), nullable=False)
    before_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    after_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"), nullable=False)

    branch = relationship("OrgBranch", viewonly=True)
    series = relationship("SubscriptionSeries", back_populates="events", viewonly=True)
    term = relationship("SubscriptionTerm", back_populates="events", viewonly=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["branch_id", "org_id"],
            ["org_branches.id", "org_branches.org_id"],
            name="fk_subscription_events_branch_org",
        ),
        ForeignKeyConstraint(
            ["series_id", "org_id"],
            ["subscription_series.id", "subscription_series.org_id"],
            name="fk_subscription_events_series_org",
        ),
        ForeignKeyConstraint(
            ["term_id", "org_id"],
            ["subscription_terms.id", "subscription_terms.org_id"],
            name="fk_subscription_events_term_org",
        ),
        Index("ix_subscription_events_org_series_time", "org_id", "series_id", "event_at"),
        Index("ix_subscription_events_org_term_time", "org_id", "term_id", "event_at"),
    )


class SubscriptionOperationIdempotency(Base):
    __tablename__ = "subscription_operation_idempotency"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    operation_name: Mapped[str] = mapped_column(String(80), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    processing_state: Mapped[SubscriptionIdempotencyState] = mapped_column(String(30), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "operation_name",
            "idempotency_key",
            name="uq_subscription_operation_idempotency_key",
        ),
        Index("ix_subscription_operation_idempotency_expiry", "expires_at"),
    )
