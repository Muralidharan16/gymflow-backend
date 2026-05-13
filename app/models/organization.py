import uuid
from datetime import datetime
from decimal import Decimal
from sqlalchemy import String, Boolean, Index, text, Text
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
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    
    # Address and Profile
    phone: Mapped[Optional[str]] = mapped_column(String(15), nullable=True) # E.164 format +91XXXXXXXXXX
    address_line1: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    address_line2: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    city: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    state: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    pincode: Mapped[Optional[str]] = mapped_column(String(6), nullable=True)
    country: Mapped[str] = mapped_column(String(60), server_default=text("'India'"), default="India", nullable=False)
    profile_completed: Mapped[bool] = mapped_column(Boolean, server_default=text("FALSE"), default=False, nullable=False)

    __table_args__ = (
        Index("ix_organizations_pan", "pan_number"),
    )
