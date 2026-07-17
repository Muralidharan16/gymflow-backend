from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import CHAR, SMALLINT, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, new_uuid


class OrganizationCreationIdempotency(Base):
    __tablename__ = "organization_creation_idempotency"
    __table_args__ = (
        UniqueConstraint("operation", "idempotency_key", name="uq_org_creation_idem_operation_key"),
        UniqueConstraint("operation", "organization_id", name="uq_org_creation_idem_operation_org"),
        CheckConstraint("operation = 'synthetic_organization_create'", name="chk_org_creation_idem_operation"),
        CheckConstraint("idempotency_key ~ '^[a-z0-9:_-]{1,200}$'", name="chk_org_creation_idem_key_format"),
        CheckConstraint("request_hash_sha256 ~ '^[0-9a-f]{64}$'", name="chk_org_creation_idem_request_hash"),
        CheckConstraint("canonicalization_version >= 1", name="chk_org_creation_idem_canonical_version"),
        CheckConstraint("trusted_source = 'finance_razorpay_test_precondition'", name="chk_org_creation_idem_trusted_source"),
        CheckConstraint("completed_at >= created_at", name="chk_org_creation_idem_completed_after_created"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid, server_default=text("gen_random_uuid()"))
    operation: Mapped[str] = mapped_column(String(80), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_hash_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    canonicalization_version: Mapped[int] = mapped_column(SMALLINT, nullable=False, server_default=text("1"))
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    trusted_source: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    completed_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
