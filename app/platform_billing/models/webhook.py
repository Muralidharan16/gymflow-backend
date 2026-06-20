from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.types import CHAR
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, new_uuid


class PlatformWebhookInbox(Base):
    __tablename__ = "platform_webhook_inbox"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    provider_code: Mapped[str] = mapped_column(String(40), nullable=False)
    provider_event_id: Mapped[str] = mapped_column(String(200), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    encrypted_payload_ref: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    processing_status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    received_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    processed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    error_classification: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_detail_safe: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))

    __table_args__ = (
        UniqueConstraint("provider_code", "provider_event_id", name="uq_platform_webhook_inbox_provider_event"),
        UniqueConstraint("provider_code", "provider_event_id", "payload_sha256", name="uq_platform_webhook_inbox_provider_event_hash"),
        CheckConstraint("provider_code ~ '^[a-z0-9_]+$'", name="chk_platform_webhook_inbox_provider_code"),
        CheckConstraint("btrim(provider_event_id) <> ''", name="chk_platform_webhook_inbox_event_id_nonempty"),
        CheckConstraint("payload_sha256 ~ '^[0-9a-f]{64}$'", name="chk_platform_webhook_inbox_payload_hash"),
        CheckConstraint("btrim(encrypted_payload_ref) <> ''", name="chk_platform_webhook_inbox_payload_ref_nonempty"),
        CheckConstraint("btrim(normalized_event_type) <> ''", name="chk_platform_webhook_inbox_event_type_nonempty"),
        CheckConstraint(
            "processing_status IN ('pending', 'processing', 'processed', 'failed_retryable', 'failed_final', 'ignored')",
            name="chk_platform_webhook_inbox_status",
        ),
        CheckConstraint("attempt_count >= 0", name="chk_platform_webhook_inbox_attempt_count"),
        CheckConstraint("processing_status <> 'processed' OR processed_at IS NOT NULL", name="chk_platform_webhook_inbox_processed_metadata"),
        CheckConstraint(
            "processing_status NOT IN ('failed_retryable', 'failed_final') OR error_classification IS NOT NULL",
            name="chk_platform_webhook_inbox_failure_metadata",
        ),
        CheckConstraint("version >= 1", name="chk_platform_webhook_inbox_version_positive"),
        Index(
            "ix_platform_webhook_inbox_status_retry",
            "processing_status",
            "received_at",
            postgresql_where=text("processing_status IN ('pending', 'failed_retryable')"),
        ),
        Index("ix_platform_webhook_inbox_event_type", "provider_code", "normalized_event_type", "received_at"),
    )
