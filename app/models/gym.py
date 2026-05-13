import uuid
from decimal import Decimal
from sqlalchemy import String, Boolean, Text, ForeignKey, Numeric, Index
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import Enum as SAEnum

from app.models.base import Base, TimestampMixin, new_uuid


from app.models.enums import FacilityType

class Gym(Base, TimestampMixin):
    __tablename__ = "gyms"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    facility_type: Mapped[FacilityType] = mapped_column(
        SAEnum(FacilityType, name="facilitytype", create_constraint=False),
        nullable=False,
        default=FacilityType.gym,
    )
    gymu_id: Mapped[str] = mapped_column(
        String(20), unique=True, nullable=False, index=True
    )
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (
        Index("ix_gyms_org_id", "org_id"),
    )


class BranchTaxSettings(Base, TimestampMixin):
    __tablename__ = "branch_tax_settings"

    gym_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gyms.id", ondelete="CASCADE"),
        primary_key=True,
    )
    gst_number: Mapped[str] = mapped_column(
        String(15), unique=True, nullable=False
    )
    legal_name: Mapped[str] = mapped_column(String, nullable=False)
    gst_rate: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), default=Decimal("18.00"), nullable=False
    )
    sac_code: Mapped[str] = mapped_column(
        String(10), default="996319", nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
