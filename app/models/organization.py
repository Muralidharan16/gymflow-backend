import uuid
from datetime import datetime
from decimal import Decimal
from sqlalchemy import String, Boolean, Index, text, Text, SMALLINT, CheckConstraint, extract, ForeignKey, UniqueConstraint, Integer
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP, JSONB
from sqlalchemy import Enum as SAEnum

from typing import Optional
from app.models.base import Base, TimestampMixin, new_uuid
from app.models.enums import OrgTier
import enum

class AssetStatus(str, enum.Enum):
    pending = "pending"
    processing = "processing"
    ready = "ready"
    failed = "failed"

class Organization(Base, TimestampMixin):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), unique=True, nullable=True, index=True)
    tier: Mapped[OrgTier] = mapped_column(
        SAEnum(OrgTier, name="orgtier", create_constraint=False),
        nullable=False,
        default=OrgTier.basic,
    )
    business_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    max_branches: Mapped[int] = mapped_column(Integer, default=10, server_default=text("10"), nullable=False)
    default_currency_code: Mapped[str] = mapped_column(String(3), server_default=text("'INR'"), default="INR", nullable=False)
    
    # Branding
    tagline: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    year_established: Mapped[Optional[int]] = mapped_column(SMALLINT, nullable=True)
    
    # Online Presence
    website_url: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    website_verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("FALSE"), nullable=False)
    social_links: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'"), nullable=False)
    
    # Address and Profile
    phone: Mapped[Optional[str]] = mapped_column(String(15), nullable=True) # E.164 format +91XXXXXXXXXX
    address_line1: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    address_line2: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    city: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    state: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    document_url: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    verification_status: Mapped[str] = mapped_column(String(20), server_default=text("'pending'"), default="pending", nullable=False)
    pincode: Mapped[Optional[str]] = mapped_column(String(6), nullable=True)
    country: Mapped[str] = mapped_column(String(60), server_default=text("'India'"), default="India", nullable=False)
    profile_completed: Mapped[bool] = mapped_column(Boolean, server_default=text("FALSE"), default=False, nullable=False)

    # Logo fields
    logo_key: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    logo_thumb_key: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    logo_medium_key: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    logo_full_key: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    logo_meta: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    logo_status: Mapped[Optional[AssetStatus]] = mapped_column(
        SAEnum(AssetStatus, name="asset_status_enum", create_constraint=False), nullable=True
    )
    logo_updated_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    logo_updated_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)

    # Cover fields
    cover_key: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    cover_mobile_key: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    cover_tablet_key: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    cover_desktop_key: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    cover_meta: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    cover_status: Mapped[Optional[AssetStatus]] = mapped_column(
        SAEnum(AssetStatus, name="asset_status_enum", create_constraint=False), nullable=True
    )
    cover_updated_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    cover_updated_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "year_established >= 1800 AND year_established <= EXTRACT(YEAR FROM CURRENT_DATE)",
            name="check_year_established"
        ),
    )

class OrganizationRegistration(Base, TimestampMixin):
    __tablename__ = "organization_registrations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )

    id_type: Mapped[str] = mapped_column(String(20), nullable=False)  # PAN, VAT, EIN, GST
    # Legacy Fernet payload retained only for the P3B expand/backfill window.
    # New crypto_version=1 ciphertext lives in the separate secure payload table
    # and this field is NULL.
    id_number_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    id_number_masked: Mapped[str] = mapped_column(String(50), nullable=False)
    country_code: Mapped[str] = mapped_column(String(2), nullable=False)
    entity_type: Mapped[Optional[str]] = mapped_column(String(1), nullable=True)  # PAN 4th char
    crypto_version: Mapped[int] = mapped_column(
        SMALLINT,
        nullable=False,
        default=0,
        server_default=text("0"),
    )

    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    verified_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_org_reg_org_id", "org_id"),
        # Retained until the P3B contract migration removes the legacy
        # randomized-ciphertext uniqueness artifact.
        UniqueConstraint(
            "country_code",
            "id_type",
            "id_number_encrypted",
            name="uix_org_reg_type_country",
        ),
        UniqueConstraint(
            "org_id",
            "country_code",
            "id_type",
            name="uq_org_reg_org_country_type",
        ),
        UniqueConstraint("id", "org_id", name="uq_org_reg_id_org"),
        CheckConstraint(
            "(crypto_version = 0 AND id_number_encrypted IS NOT NULL) OR "
            "(crypto_version = 1 AND id_number_encrypted IS NULL)",
            name="ck_org_reg_crypto_material",
        ),
        CheckConstraint(
            "id_type = upper(btrim(id_type)) AND id_type <> '' AND "
            "country_code = upper(btrim(country_code)) AND length(country_code) = 2",
            name="ck_org_reg_canonical_identity",
        ),
    )

class OrganizationAssetAudit(Base, TimestampMixin):
    __tablename__ = "organization_asset_audit"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    changed_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    asset_type: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'logo'"), default="logo")
    old_s3_key: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    new_s3_key: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    action_detail: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(Text, nullable=True)