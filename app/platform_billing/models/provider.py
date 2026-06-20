from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, ForeignKey, ForeignKeyConstraint, Index, Integer, SmallInteger, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.types import CHAR
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, new_uuid


class PlatformProviderCustomer(Base):
    __tablename__ = "platform_provider_customers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    provider_code: Mapped[str] = mapped_column(String(40), nullable=False)
    external_customer_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))

    __table_args__ = (
        UniqueConstraint("id", "organization_id", name="uq_platform_provider_customers_id_org"),
        UniqueConstraint("organization_id", "provider_code", name="uq_platform_provider_customers_org_provider"),
        UniqueConstraint("provider_code", "external_customer_ref", name="uq_platform_provider_customers_provider_external"),
        CheckConstraint("provider_code ~ '^[a-z0-9_]+$'", name="chk_platform_provider_customers_provider_code"),
        CheckConstraint("btrim(external_customer_ref) <> ''", name="chk_platform_provider_customers_external_ref_nonempty"),
        CheckConstraint("status IN ('active', 'inactive', 'deleted')", name="chk_platform_provider_customers_status"),
        CheckConstraint("version >= 1", name="chk_platform_provider_customers_version_positive"),
        Index("ix_platform_provider_customers_org_status", "organization_id", "status"),
    )


class PlatformPaymentMethod(Base):
    __tablename__ = "platform_payment_methods"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    provider_customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    provider_code: Mapped[str] = mapped_column(String(40), nullable=False)
    external_payment_method_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    method_type: Mapped[str] = mapped_column(String(40), nullable=False)
    brand: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_four: Mapped[str | None] = mapped_column(CHAR(4), nullable=True)
    expiry_month: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    expiry_year: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    display_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))

    __table_args__ = (
        ForeignKeyConstraint(
            ["provider_customer_id", "organization_id"],
            ["platform_provider_customers.id", "platform_provider_customers.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_payment_methods_customer_org",
        ),
        UniqueConstraint("id", "organization_id", name="uq_platform_payment_methods_id_org"),
        UniqueConstraint(
            "organization_id",
            "provider_code",
            "external_payment_method_ref",
            name="uq_platform_payment_methods_org_provider_external",
        ),
        CheckConstraint("provider_code ~ '^[a-z0-9_]+$'", name="chk_platform_payment_methods_provider_code"),
        CheckConstraint("btrim(external_payment_method_ref) <> ''", name="chk_platform_payment_methods_external_ref_nonempty"),
        CheckConstraint("btrim(method_type) <> ''", name="chk_platform_payment_methods_type_nonempty"),
        CheckConstraint("last_four IS NULL OR last_four ~ '^[0-9]{4}$'", name="chk_platform_payment_methods_last_four"),
        CheckConstraint("expiry_month IS NULL OR expiry_month BETWEEN 1 AND 12", name="chk_platform_payment_methods_expiry_month"),
        CheckConstraint("expiry_year IS NULL OR expiry_year BETWEEN 2020 AND 2200", name="chk_platform_payment_methods_expiry_year"),
        CheckConstraint("status IN ('active', 'inactive', 'expired', 'detached')", name="chk_platform_payment_methods_status"),
        CheckConstraint("is_default = false OR status = 'active'", name="chk_platform_payment_methods_default_active"),
        CheckConstraint("version >= 1", name="chk_platform_payment_methods_version_positive"),
        Index("ix_platform_payment_methods_org_customer", "organization_id", "provider_customer_id"),
        Index(
            "ux_platform_payment_methods_one_default_per_provider",
            "organization_id",
            "provider_code",
            unique=True,
            postgresql_where=text("status = 'active' AND is_default = true"),
        ),
    )


class PlatformProviderOperation(Base):
    __tablename__ = "platform_provider_operations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    provider_code: Mapped[str] = mapped_column(String(40), nullable=False)
    operation_type: Mapped[str] = mapped_column(String(80), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    canonical_request_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'reserved'"))
    external_operation_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    next_retry_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    result_evidence_sha256: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    result_reference: Mapped[str | None] = mapped_column(String(240), nullable=True)
    error_classification: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_detail_safe: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))

    __table_args__ = (
        UniqueConstraint("id", "organization_id", name="uq_platform_provider_operations_id_org"),
        UniqueConstraint("organization_id", "provider_code", "idempotency_key", name="uq_platform_provider_operations_idempotency"),
        CheckConstraint("provider_code ~ '^[a-z0-9_]+$'", name="chk_platform_provider_operations_provider_code"),
        CheckConstraint("btrim(operation_type) <> ''", name="chk_platform_provider_operations_operation_type_nonempty"),
        CheckConstraint("btrim(idempotency_key) <> ''", name="chk_platform_provider_operations_idempotency_nonempty"),
        CheckConstraint("canonical_request_sha256 ~ '^[0-9a-f]{64}$'", name="chk_platform_provider_operations_request_hash"),
        CheckConstraint("status IN ('reserved', 'in_progress', 'succeeded', 'failed', 'unknown')", name="chk_platform_provider_operations_status"),
        CheckConstraint("attempt_count >= 0", name="chk_platform_provider_operations_attempt_count"),
        CheckConstraint("result_evidence_sha256 IS NULL OR result_evidence_sha256 ~ '^[0-9a-f]{64}$'", name="chk_platform_provider_operations_result_hash"),
        CheckConstraint("status NOT IN ('succeeded', 'failed', 'unknown') OR completed_at IS NOT NULL", name="chk_platform_provider_operations_terminal_completed"),
        CheckConstraint("status <> 'failed' OR error_classification IS NOT NULL", name="chk_platform_provider_operations_failure_metadata"),
        CheckConstraint("version >= 1", name="chk_platform_provider_operations_version_positive"),
        Index("ix_platform_provider_operations_org_status", "organization_id", "status"),
        Index("ix_platform_provider_operations_external_ref", "provider_code", "external_operation_ref", postgresql_where=text("external_operation_ref IS NOT NULL")),
        Index("ix_platform_provider_operations_retry", "status", "next_retry_at", postgresql_where=text("status IN ('reserved', 'in_progress', 'unknown')")),
    )
