import uuid
from datetime import date, datetime
from decimal import Decimal
# pyrefly: ignore [missing-import]
from sqlalchemy import (
    String, Boolean, Date, Text, ForeignKey, Numeric, Index, Integer,
    UniqueConstraint,
)
# pyrefly: ignore [missing-import]
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import Enum as SAEnum

from app.models.base import Base, TimestampMixin, new_uuid
from app.models.enums import MemberStatus


class Member(Base, TimestampMixin):
    __tablename__ = "members"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    gym_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gyms.id", ondelete="CASCADE"),
        nullable=True,
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    home_branch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("org_branches.id", ondelete="SET NULL"),
        nullable=True,
    )
    member_uid: Mapped[str] = mapped_column(
        String(20), unique=True, nullable=False
    )
    member_number: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)
    gender: Mapped[str | None] = mapped_column(String(10), nullable=True)
    blood_group: Mapped[str | None] = mapped_column(String(5), nullable=True)
    emergency_contact_name: Mapped[str | None] = mapped_column(String, nullable=True)
    emergency_contact_phone: Mapped[str | None] = mapped_column(String, nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    height_cm: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    weight_kg: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    fingerprint_id: Mapped[str | None] = mapped_column(String, nullable=True)
    photo_url: Mapped[str | None] = mapped_column(String, nullable=True)
    qr_token: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    status: Mapped[MemberStatus] = mapped_column(
        SAEnum(MemberStatus, name="memberstatus", create_constraint=False),
        default=MemberStatus.active,
        nullable=False,
    )
    source: Mapped[str | None] = mapped_column(String(30), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_migrated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    migrated_source: Mapped[str | None] = mapped_column(String(50), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )

    __table_args__ = (
        Index("ix_members_org_id", "org_id"),
        Index("ix_members_org_branch_id", "org_id", "home_branch_id"),
        Index("ix_members_org_status", "org_id", "status"),
        Index("ix_members_org_branch_status", "org_id", "home_branch_id", "status"),
        Index("ix_members_org_phone", "org_id", "phone"),
        UniqueConstraint("org_id", "member_number", name="uq_members_org_member_number"),
        Index("ix_members_gym_id", "gym_id"),
        Index("ix_members_gym_member_uid", "gym_id", "member_uid", unique=True),
        Index("ix_members_gym_phone", "gym_id", "phone"),
        Index("ix_members_gym_qr_token", "gym_id", "qr_token", unique=True),
        Index("ix_members_gym_fingerprint", "gym_id", "fingerprint_id"),
        Index("ix_members_gym_status", "gym_id", "status"),
        Index("ix_members_email", "email"),
    )


class MemberMeasurement(Base, TimestampMixin):
    __tablename__ = "member_measurements"

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
    measured_on: Mapped[date] = mapped_column(Date, nullable=False)
    weight_kg: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    height_cm: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    body_fat_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gym_owners.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        Index("ix_measurements_member_date", "member_id", measured_on.desc()),
    )
