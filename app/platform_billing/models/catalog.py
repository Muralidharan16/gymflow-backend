from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, CheckConstraint, ForeignKey, Index, Integer, SmallInteger, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.types import CHAR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, new_uuid


class PlatformProduct(Base):
    __tablename__ = "platform_products"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    code: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))

    plan_versions: Mapped[list["PlatformPlanVersion"]] = relationship(back_populates="product")

    __table_args__ = (
        CheckConstraint("status IN ('draft', 'active', 'retired')", name="chk_platform_products_status"),
        CheckConstraint("code = upper(code)", name="chk_platform_products_code_upper"),
    )


class PlatformPolicyVersion(Base):
    __tablename__ = "platform_policy_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    code: Mapped[str] = mapped_column(String(60), nullable=False, unique=True)
    policy_type: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    payload_sha256: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))

    __table_args__ = (
        CheckConstraint(
            "policy_type IN ('trial', 'dunning', 'cancellation', 'downgrade', 'refund', 'retention')",
            name="chk_platform_policy_versions_policy_type",
        ),
        CheckConstraint("version > 0", name="chk_platform_policy_versions_version_positive"),
        CheckConstraint("status IN ('draft', 'published', 'retired')", name="chk_platform_policy_versions_status"),
        UniqueConstraint("policy_type", "version", name="uq_platform_policy_versions_type_version"),
    )


class PlatformPlanVersion(Base):
    __tablename__ = "platform_plan_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("platform_products.id", ondelete="RESTRICT"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    code: Mapped[str] = mapped_column(String(60), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    trial_policy_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("platform_policy_versions.id", ondelete="RESTRICT"), nullable=True)
    dunning_policy_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("platform_policy_versions.id", ondelete="RESTRICT"), nullable=True)
    cancellation_policy_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("platform_policy_versions.id", ondelete="RESTRICT"), nullable=True)
    downgrade_policy_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("platform_policy_versions.id", ondelete="RESTRICT"), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    published_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))

    product: Mapped[PlatformProduct] = relationship(back_populates="plan_versions")
    prices: Mapped[list["PlatformPrice"]] = relationship(back_populates="plan_version")
    entitlements: Mapped[list["PlatformPlanEntitlement"]] = relationship(back_populates="plan_version")

    __table_args__ = (
        CheckConstraint("version > 0", name="chk_platform_plan_versions_version_positive"),
        CheckConstraint("status IN ('draft', 'published', 'retired')", name="chk_platform_plan_versions_status"),
        UniqueConstraint("product_id", "version", name="uq_platform_plan_versions_product_version"),
        Index("ix_platform_plan_versions_product_status", "product_id", "status"),
    )


class PlatformPrice(Base):
    __tablename__ = "platform_prices"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    plan_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("platform_plan_versions.id", ondelete="RESTRICT"), nullable=False)
    code: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    currency_code: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    country_code: Mapped[str | None] = mapped_column(CHAR(2), nullable=True)
    billing_interval: Mapped[str] = mapped_column(Text, nullable=False)
    interval_count: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tax_behavior: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    valid_from: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    provider_price_hint: Mapped[str | None] = mapped_column(String(120), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))

    plan_version: Mapped[PlatformPlanVersion] = relationship(back_populates="prices")

    __table_args__ = (
        CheckConstraint("billing_interval IN ('month', 'year', 'one_time')", name="chk_platform_prices_billing_interval"),
        CheckConstraint("interval_count > 0", name="chk_platform_prices_interval_count_positive"),
        CheckConstraint("amount_minor >= 0", name="chk_platform_prices_amount_nonnegative"),
        CheckConstraint("tax_behavior IN ('exclusive', 'inclusive', 'not_applicable')", name="chk_platform_prices_tax_behavior"),
        CheckConstraint("status IN ('draft', 'active', 'retired')", name="chk_platform_prices_status"),
        Index("ix_platform_prices_plan_status", "plan_version_id", "status"),
        Index("ix_platform_prices_availability", "country_code", "currency_code", "billing_interval"),
    )


class PlatformFeatureDefinition(Base):
    __tablename__ = "platform_feature_definitions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    key: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    value_type: Mapped[str] = mapped_column(Text, nullable=False)
    enforcement_mode: Mapped[str] = mapped_column(Text, nullable=False)
    unit: Mapped[str | None] = mapped_column(String(40), nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))

    entitlements: Mapped[list["PlatformPlanEntitlement"]] = relationship(back_populates="feature_definition")

    __table_args__ = (
        CheckConstraint("value_type IN ('boolean', 'integer', 'string', 'json')", name="chk_platform_feature_definitions_value_type"),
        CheckConstraint(
            "enforcement_mode IN ('hard', 'soft', 'metered', 'informational')",
            name="chk_platform_feature_definitions_enforcement_mode",
        ),
        CheckConstraint("status IN ('active', 'retired')", name="chk_platform_feature_definitions_status"),
    )


class PlatformPlanEntitlement(Base):
    __tablename__ = "platform_plan_entitlements"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    plan_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("platform_plan_versions.id", ondelete="RESTRICT"), nullable=False)
    feature_definition_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("platform_feature_definitions.id", ondelete="RESTRICT"), nullable=False)
    value_type: Mapped[str] = mapped_column(Text, nullable=False)
    value_boolean: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    value_integer: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    value_string: Mapped[str | None] = mapped_column(Text, nullable=True)
    value_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))

    plan_version: Mapped[PlatformPlanVersion] = relationship(back_populates="entitlements")
    feature_definition: Mapped[PlatformFeatureDefinition] = relationship(back_populates="entitlements")

    __table_args__ = (
        CheckConstraint("value_type IN ('boolean', 'integer', 'string', 'json')", name="chk_platform_plan_entitlements_value_type"),
        UniqueConstraint("plan_version_id", "feature_definition_id", name="uq_platform_plan_entitlements_plan_feature"),
    )
