import uuid
from datetime import datetime
from typing import Optional, Any
from sqlalchemy import String, Boolean, ForeignKey, Index, BigInteger, text, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP, JSONB, ARRAY
import sqlalchemy as sa

from app.models.base import Base

class BranchStatusDefinition(Base):
    __tablename__ = "branch_status_definitions"

    code: Mapped[str] = mapped_column(String, primary_key=True)
    is_operational: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_terminal: Mapped[bool] = mapped_column(Boolean, nullable=False)
    system_health_state: Mapped[str] = mapped_column(String, nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False)


class BranchStatusTransition(Base):
    __tablename__ = "branch_status_transitions"

    from_status: Mapped[str] = mapped_column(String, ForeignKey("branch_status_definitions.code"), primary_key=True)
    to_status: Mapped[str] = mapped_column(String, ForeignKey("branch_status_definitions.code"), primary_key=True)
    allowed_roles: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
    requires_reason: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("FALSE"), nullable=False)


class BranchDeactivationPolicy(Base):
    __tablename__ = "branch_deactivation_policies"

    from_status: Mapped[str] = mapped_column(String, primary_key=True)
    to_status: Mapped[str] = mapped_column(String, primary_key=True)
    booking_grace_hours: Mapped[int] = mapped_column(Integer, default=24, server_default=text("24"), nullable=False)
    auto_cancel_bookings: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("FALSE"), nullable=False)
    notify_members: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("TRUE"), nullable=False)
    refund_policy: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["from_status", "to_status"],
            ["branch_status_transitions.from_status", "branch_status_transitions.to_status"]
        ),
    )


class BranchStatusHistory(Base):
    __tablename__ = "branch_status_history"

    history_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("org_branches.id"), nullable=False)
    from_status: Mapped[Optional[str]] = mapped_column(String, ForeignKey("branch_status_definitions.code"), nullable=True)
    to_status: Mapped[str] = mapped_column(String, ForeignKey("branch_status_definitions.code"), nullable=False)
    changed_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("organization_users.id"), nullable=True)
    changed_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("clock_timestamp()"), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    transition_source: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    correlation_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    correlation_emitted_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_branch_history_lookup", "branch_id", "changed_at"),
    )


class BranchLifecycleEvent(Base):
    __tablename__ = "branch_lifecycle_events"

    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("org_branches.id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    event_version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    emitted_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), primary_key=True, server_default=text("clock_timestamp()"))
    correlation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    step_sequence: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"), nullable=False)


class BranchOutboxEvent(Base):
    __tablename__ = "branch_outbox_events"

    outbox_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("org_branches.id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("clock_timestamp()"), nullable=False)
    process_after: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("clock_timestamp()"), nullable=False)
    status: Mapped[str] = mapped_column(String, default="pending", server_default=text("'pending'"), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5, server_default=text("5"), nullable=False)
    last_attempted_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    correlation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)


class BranchWatchdogAlert(Base):
    __tablename__ = "branch_watchdog_alerts"

    alert_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("org_branches.id"), nullable=False)
    alert_type: Mapped[str] = mapped_column(String, nullable=False)
    triggered_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("clock_timestamp()"), nullable=False)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    resolution_notes: Mapped[Optional[str]] = mapped_column(String, nullable=True)
