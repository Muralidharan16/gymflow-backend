import uuid
from datetime import datetime, time, date
from typing import Optional

from sqlalchemy import String, Boolean, ForeignKey, text, SmallInteger, Time, Date, BigInteger
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP, JSONB

from app.models.base import Base, TimestampMixin, new_uuid


class OrganizationOperatingHours(Base, TimestampMixin):
    __tablename__ = "organization_operating_hours"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )

    day_of_week: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    slot_index: Mapped[int] = mapped_column(SmallInteger, default=1, nullable=False)

    valid_from: Mapped[date] = mapped_column(
        Date, nullable=False, server_default=text("CURRENT_DATE")
    )
    valid_until: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    open_time: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    close_time: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    is_closed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_24_hours: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Generated columns are read-only at the application layer.
    is_overnight: Mapped[bool] = mapped_column(Boolean, server_default=text("FALSE"))

    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class BranchOperatingHours(Base, TimestampMixin):
    __tablename__ = "branch_operating_hours"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("org_branches.id", ondelete="RESTRICT"), nullable=False
    )

    day_of_week: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    slot_index: Mapped[int] = mapped_column(SmallInteger, default=1, nullable=False)

    valid_from: Mapped[date] = mapped_column(
        Date, nullable=False, server_default=text("CURRENT_DATE")
    )
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
    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("org_branches.id", ondelete="RESTRICT"), nullable=False
    )
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

    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("org_branches.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    projection_version: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)
    last_rebuilt_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("clock_timestamp()"), nullable=False
    )
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
    changed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("clock_timestamp()"), primary_key=True, nullable=False
    )

    old_data: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    new_data: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
