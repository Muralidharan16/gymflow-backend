import uuid
from datetime import datetime, time, date, timezone
from typing import Optional, List
from sqlalchemy import String, Boolean, ForeignKey, Index, text, SmallInteger, Time, Date, BigInteger, CheckConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP, JSONB

from app.models.base import Base, TimestampMixin, new_uuid

class OrganizationOperatingHours(Base, TimestampMixin):
    __tablename__ = "organization_operating_hours"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    
    day_of_week: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    slot_index: Mapped[int] = mapped_column(SmallInteger, default=1, nullable=False)
    
    valid_from: Mapped[date] = mapped_column(Date, nullable=False, server_default=text("CURRENT_DATE"))
    valid_until: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    
    open_time: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    close_time: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    is_closed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_24_hours: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    
    # Generated columns are typically read-only in SQLAlchemy
    is_overnight: Mapped[bool] = mapped_column(Boolean, server_default=text("FALSE"))

    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

class BranchOperatingHours(Base, TimestampMixin):
    __tablename__ = "branch_operating_hours"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("org_branches.id", ondelete="RESTRICT"), nullable=False)
    
    day_of_week: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    slot_index: Mapped[int] = mapped_column(SmallInteger, default=1, nullable=False)
    
    valid_from: Mapped[date] = mapped_column(Date, nullable=False, server_default=text("CURRENT_DATE"))
    valid_until: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    
    open_time: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    close_time: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    is_closed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_24_hours: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    
    is_overnight: Mapped[bool] = mapped_column(Boolean, server_default=text("FALSE"))

    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

class BranchSpecialHours(Base, TimestampMixin):
    __tablename__ = "branch_special_hours"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("org_branches.id", ondelete="RESTRICT"), nullable=False)
    special_date: Mapped[date] = mapped_column(Date, nullable=False)
    
    open_time: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    close_time: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    is_closed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_24_hours: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    
    is_overnight: Mapped[bool] = mapped_column(Boolean, server_default=text("FALSE"))
    reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

class BranchHoursProjection(Base):
    __tablename__ = "branch_hours_projection"

    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("org_branches.id", ondelete="RESTRICT"), primary_key=True)
    projection_version: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)
    last_rebuilt_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("clock_timestamp()"), nullable=False)
    source_hash: Mapped[str] = mapped_column(String, nullable=False)
    timezone: Mapped[str] = mapped_column(String, nullable=False)
    current_status: Mapped[str] = mapped_column(String(20), nullable=False)
    
    next_open_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    next_close_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    
    weekly_schedule: Mapped[dict] = mapped_column(JSONB, nullable=False)
    upcoming_exceptions: Mapped[dict] = mapped_column(JSONB, nullable=False)

class BranchHoursAuditLog(Base):
    __tablename__ = "branch_hours_audit_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    table_name: Mapped[str] = mapped_column(String, nullable=False)
    record_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    operation: Mapped[str] = mapped_column(String, nullable=False)
    
    changed_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    changed_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("clock_timestamp()"), primary_key=True, nullable=False)
    
    old_data: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
from sqlalchemy import event

def emit_branch_hours_outbox_event(mapper, connection, target):
    """
    SQLAlchemy event listener that writes to the outbox when hours change.
    Since this fires within the active session flush, it runs in the same transaction.
    """
    # Note: We use execute so it bypasses flush loops
    
    # We need the branch_id. 
    # For OrganizationOperatingHours, we emit events for ALL branches in that org.
    # In a real system, you might enqueue an org-level event that explodes into branch-level outboxes.
    # For simplicity, we just emit an event per branch modified.
    
    branch_ids = []
    
    if isinstance(target, OrganizationOperatingHours):
        # We need to notify all branches of this org
        res = connection.execute(
            text("SELECT id FROM public.org_branches WHERE org_id = :org_id AND deleted_at IS NULL"),
            {"org_id": target.org_id}
        )
        branch_ids = [row[0] for row in res]
    elif hasattr(target, "branch_id"):
        branch_ids = [target.branch_id]
        
    for b_id in branch_ids:
        # We generate a deterministic dedupe key based on the branch and timestamp (truncated to minute to avoid bursts)
        dedupe_key = f"branch_hours_{b_id}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M')}"
        
        # INSERT ... ON CONFLICT DO NOTHING (if dedupe_key has a unique index, else just insert)
        # Assuming dedupe_key is just used by the processor, we'll just insert.
        connection.execute(
            text("""
                INSERT INTO public.transactional_outbox (event_type, payload, dedupe_key)
                VALUES (:evt, :pay, :dedupe)
            """),
            {
                "evt": "branch_hours.changed",
                "pay": '{"branch_id": "' + str(b_id) + '"}',
                "dedupe": dedupe_key
            }
        )

# Attach listeners
for model in [OrganizationOperatingHours, BranchOperatingHours, BranchSpecialHours]:
    event.listen(model, 'after_insert', emit_branch_hours_outbox_event)
    event.listen(model, 'after_update', emit_branch_hours_outbox_event)
    event.listen(model, 'after_delete', emit_branch_hours_outbox_event)
