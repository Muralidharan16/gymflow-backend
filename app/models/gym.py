import uuid
from typing import Optional
from decimal import Decimal
from sqlalchemy import String, Boolean, Text, ForeignKey, Numeric, Index, UniqueConstraint, Integer, Table, Column
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import Enum as SAEnum

from app.models.base import Base, TimestampMixin, new_uuid


from app.models.enums import FacilityType as FacilityTypeEnum

# Junction Table for Many-to-Many relationship between Gym and FacilityType
gym_facility_types = Table(
    "gym_facility_types",
    Base.metadata,
    Column("gym_id", UUID(as_uuid=True), ForeignKey("gyms.id", ondelete="CASCADE"), primary_key=True),
    Column("facility_type_id", Integer, ForeignKey("facility_types.id", ondelete="CASCADE"), primary_key=True),
)

class FacilityType(Base):
    __tablename__ = "facility_types"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    system_name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False) # e.g. 'crossfit_box'
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)           # e.g. 'CrossFit Box'
    icon_key: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)       # e.g. 'icon-weights'
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

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
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # Deprecated: use facility_types relationship instead
    facility_type: Mapped[FacilityTypeEnum] = mapped_column(
        SAEnum(FacilityTypeEnum, name="facilitytype", create_constraint=False),
        nullable=False,
        default=FacilityTypeEnum.gym,
    )

    facility_types: Mapped[list["FacilityType"]] = relationship(
        "FacilityType", secondary=gym_facility_types, backref="gyms"
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
        UniqueConstraint("org_id", "name", name="uix_gym_org_name"),
    )


class BranchTaxSettings(Base, TimestampMixin):
    __tablename__ = "branch_tax_settings"

    gym_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gyms.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # Tax Identity
    tax_type: Mapped[str] = mapped_column(String(10), default="GST", nullable=False) # GST, VAT, SalesTax
    tax_id_encrypted: Mapped[str] = mapped_column(Text, nullable=False)              # AES-256
    tax_id_masked: Mapped[str] = mapped_column(String(20), nullable=False)             # XXXXXX1234
    
    legal_name: Mapped[str] = mapped_column(String(100), nullable=False)
    gst_rate: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), default=Decimal("18.00"), nullable=False
    )
    sac_code: Mapped[str] = mapped_column(
        String(10), default="996319", nullable=False
    )
    
    # Compliance
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    filing_frequency: Mapped[Optional[str]] = mapped_column(String(20), nullable=True) # monthly, quarterly
    
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
