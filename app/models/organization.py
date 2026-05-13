import uuid
from datetime import datetime
from decimal import Decimal
from sqlalchemy import String, Boolean, Index, text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP
from sqlalchemy import Enum as SAEnum

from typing import Optional
from app.models.base import Base, TimestampMixin, new_uuid
from app.models.enums import OrgTier, FacilityType


class Organization(Base, TimestampMixin):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    pan_number: Mapped[Optional[str]] = mapped_column(
        String(10), unique=True, nullable=True, index=True
    )
    tier: Mapped[OrgTier] = mapped_column(
        SAEnum(OrgTier, name="orgtier", create_constraint=False),
        nullable=False,
        default=OrgTier.basic,
    )
    facility_type: Mapped[FacilityType] = mapped_column(
        SAEnum(FacilityType, name="facilitytype", create_constraint=False),
        nullable=False,
        default=FacilityType.gym,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (
        Index("ix_organizations_pan", "pan_number"),
    )
