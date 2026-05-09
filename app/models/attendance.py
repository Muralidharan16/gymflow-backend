import uuid
from datetime import datetime
from sqlalchemy import Boolean, ForeignKey, Index, text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP
from sqlalchemy import Enum as SAEnum

from app.models.base import Base, TimestampMixin, new_uuid
from app.models.enums import CheckInMethod, AttendanceDenialReason


class AttendanceLog(Base, TimestampMixin):
    __tablename__ = "attendance_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    gym_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gyms.id", ondelete="CASCADE"),
        nullable=False,
    )
    member_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("members.id", ondelete="SET NULL"),
        nullable=True,
    )
    scan_time: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()"), nullable=False
    )
    check_out_time: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    check_in_method: Mapped[CheckInMethod] = mapped_column(
        SAEnum(CheckInMethod, name="checkinmethod", create_constraint=False),
        nullable=False,
    )
    access_granted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    denial_reason: Mapped[AttendanceDenialReason | None] = mapped_column(
        SAEnum(
            AttendanceDenialReason,
            name="attendancedenialreason",
            create_constraint=False,
        ),
        nullable=True,
    )

    __table_args__ = (
        Index("ix_attendance_gym_scan", "gym_id", scan_time.desc()),
        Index("ix_attendance_gym_id", "gym_id"),
        Index("ix_attendance_member_id", "member_id"),
        Index("ix_attendance_scan_time", "scan_time"),
    )
