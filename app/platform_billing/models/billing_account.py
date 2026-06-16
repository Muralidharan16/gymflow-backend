from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, ForeignKey, Index, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.types import CHAR
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, new_uuid


class PlatformBillingAccount(Base):
    __tablename__ = "platform_billing_accounts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    legal_name: Mapped[str] = mapped_column(String(200), nullable=False)
    billing_email: Mapped[str] = mapped_column(String(320), nullable=False)
    billing_phone_e164: Mapped[str | None] = mapped_column(String(20), nullable=True)
    country_code: Mapped[str] = mapped_column(CHAR(2), nullable=False)
    default_currency_code: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    address_line1: Mapped[str | None] = mapped_column(Text, nullable=True)
    address_line2: Mapped[str | None] = mapped_column(Text, nullable=True)
    city: Mapped[str] = mapped_column(String(120), nullable=False)
    subdivision: Mapped[str | None] = mapped_column(String(120), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    tax_registration_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    tax_registration_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    tax_registration_masked: Mapped[str | None] = mapped_column(String(40), nullable=True)
    tax_registration_hash: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    tax_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    tax_verified_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    invoice_locale: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'en-IN'"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))

    __table_args__ = (
        CheckConstraint("status IN ('active', 'closed')", name="chk_platform_billing_accounts_status"),
        CheckConstraint("version >= 1", name="chk_platform_billing_accounts_version_positive"),
        UniqueConstraint("id", "organization_id", name="uq_platform_billing_accounts_id_org"),
        Index("ux_platform_billing_accounts_one_active_per_org", "organization_id", unique=True, postgresql_where=text("status = 'active'")),
    )
