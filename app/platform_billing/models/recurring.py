from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import CHAR, JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, new_uuid


class PlatformRecurringBillingJob(Base):
    __tablename__ = "platform_recurring_billing_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    subscription_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    period_start: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    run_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'scheduled'"))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_owner: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    lease_fence: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    last_payment_attempt_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    last_evidence_sha256: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    last_evidence_ref: Mapped[str | None] = mapped_column(String(300), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))

    __table_args__ = (
        ForeignKeyConstraint(
            ["subscription_id", "organization_id"],
            ["platform_subscriptions.id", "platform_subscriptions.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_recurring_jobs_subscription_org",
        ),
        ForeignKeyConstraint(
            ["invoice_id", "organization_id"],
            ["platform_invoices.id", "platform_invoices.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_recurring_jobs_invoice_org",
        ),
        ForeignKeyConstraint(
            ["last_payment_attempt_id", "organization_id"],
            ["platform_payment_attempts.id", "platform_payment_attempts.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_recurring_jobs_attempt_org",
        ),
        UniqueConstraint("id", "organization_id", name="uq_platform_recurring_jobs_id_org"),
        UniqueConstraint("subscription_id", "period_start", "period_end", name="uq_platform_recurring_jobs_period"),
        CheckConstraint("period_end > period_start", name="chk_platform_recurring_jobs_period_order"),
        CheckConstraint("max_attempts > 0", name="chk_platform_recurring_jobs_max_attempts"),
        CheckConstraint("attempt_count BETWEEN 0 AND max_attempts", name="chk_platform_recurring_jobs_attempt_count"),
        CheckConstraint(
            "status IN ('scheduled','processing','awaiting_reconciliation','retry','completed','dead_lettered','canceled')",
            name="chk_platform_recurring_jobs_status",
        ),
        CheckConstraint("(lease_owner IS NULL) = (lease_until IS NULL)", name="chk_platform_recurring_jobs_lease_pair"),
        CheckConstraint("lease_fence >= 0", name="chk_platform_recurring_jobs_lease_fence"),
        CheckConstraint(
            "last_evidence_sha256 IS NULL OR last_evidence_sha256 ~ '^[0-9a-f]{64}$'",
            name="chk_platform_recurring_jobs_evidence_sha",
        ),
        CheckConstraint(
            "status NOT IN ('completed','dead_lettered','canceled') OR completed_at IS NOT NULL",
            name="chk_platform_recurring_jobs_terminal_completed",
        ),
        Index("ix_platform_recurring_jobs_due", "status", "run_at", "next_attempt_at"),
        Index("ix_platform_recurring_jobs_org_subscription", "organization_id", "subscription_id"),
    )


class PlatformDunningCase(Base):
    __tablename__ = "platform_dunning_cases"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    subscription_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    invoice_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    policy_code: Mapped[str] = mapped_column(String(80), nullable=False)
    policy_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'open'"))
    stage: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'full_grace'"))
    first_confirmed_failure_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    full_grace_ends_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    limited_write_ends_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    read_only_ends_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    confirmed_attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    next_retry_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    last_evidence_kind: Mapped[str] = mapped_column(String(80), nullable=False)
    last_evidence_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    last_evidence_ref: Mapped[str] = mapped_column(String(300), nullable=False)
    last_evidence_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    restricted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    suspended_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    recovered_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    terminated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))

    __table_args__ = (
        ForeignKeyConstraint(
            ["subscription_id", "organization_id"],
            ["platform_subscriptions.id", "platform_subscriptions.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_dunning_cases_subscription_org",
        ),
        ForeignKeyConstraint(
            ["invoice_id", "organization_id"],
            ["platform_invoices.id", "platform_invoices.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_dunning_cases_invoice_org",
        ),
        UniqueConstraint("id", "organization_id", name="uq_platform_dunning_cases_id_org"),
        Index(
            "ux_platform_dunning_cases_open_invoice",
            "invoice_id",
            unique=True,
            postgresql_where=text("status IN ('open','suspended')"),
        ),
        CheckConstraint("status IN ('open','suspended','recovered','terminated')", name="chk_platform_dunning_cases_status"),
        CheckConstraint("stage IN ('full_grace','limited_write','read_only','billing_only','recovered')", name="chk_platform_dunning_cases_stage"),
        CheckConstraint("jsonb_typeof(policy_snapshot_json) = 'object'", name="chk_platform_dunning_cases_policy_snapshot"),
        CheckConstraint("confirmed_attempt_count BETWEEN 1 AND max_attempts", name="chk_platform_dunning_cases_attempt_count"),
        CheckConstraint("max_attempts > 0", name="chk_platform_dunning_cases_max_attempts"),
        CheckConstraint(
            "full_grace_ends_at >= first_confirmed_failure_at AND limited_write_ends_at >= full_grace_ends_at AND read_only_ends_at >= limited_write_ends_at",
            name="chk_platform_dunning_cases_boundaries",
        ),
        CheckConstraint(
            "last_evidence_kind IN ('confirmed_payment_failure','mandate_unavailable','customer_action_required','payment_succeeded')",
            name="chk_platform_dunning_cases_durable_evidence_kind",
        ),
        CheckConstraint("last_evidence_sha256 ~ '^[0-9a-f]{64}$'", name="chk_platform_dunning_cases_evidence_sha"),
        CheckConstraint("btrim(last_evidence_ref) <> ''", name="chk_platform_dunning_cases_evidence_ref"),
        CheckConstraint("status <> 'recovered' OR (stage='recovered' AND recovered_at IS NOT NULL)", name="chk_platform_dunning_cases_recovered_shape"),
        CheckConstraint("status <> 'suspended' OR (stage='billing_only' AND suspended_at IS NOT NULL)", name="chk_platform_dunning_cases_suspended_shape"),
        CheckConstraint("status <> 'terminated' OR (stage='billing_only' AND terminated_at IS NOT NULL)", name="chk_platform_dunning_cases_terminated_shape"),
        CheckConstraint("version >= 1", name="chk_platform_dunning_cases_version"),
        Index("ix_platform_dunning_cases_org_status", "organization_id", "status", "stage"),
        Index("ix_platform_dunning_cases_next_retry", "next_retry_at", postgresql_where=text("status='open'")),
    )


class PlatformDunningAttempt(Base):
    __tablename__ = "platform_dunning_attempts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    dunning_case_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    payment_attempt_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    counts_toward_dunning: Mapped[bool] = mapped_column(Boolean, nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(String(300), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    next_retry_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))

    __table_args__ = (
        ForeignKeyConstraint(
            ["dunning_case_id", "organization_id"],
            ["platform_dunning_cases.id", "platform_dunning_cases.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_dunning_attempts_case_org",
        ),
        ForeignKeyConstraint(
            ["payment_attempt_id", "organization_id"],
            ["platform_payment_attempts.id", "platform_payment_attempts.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_dunning_attempts_payment_org",
        ),
        UniqueConstraint("id", "organization_id", name="uq_platform_dunning_attempts_id_org"),
        UniqueConstraint("dunning_case_id", "attempt_number", name="uq_platform_dunning_attempts_number"),
        UniqueConstraint("dunning_case_id", "evidence_sha256", name="uq_platform_dunning_attempts_evidence"),
        CheckConstraint("attempt_number > 0", name="chk_platform_dunning_attempts_number"),
        CheckConstraint(
            "outcome IN ('confirmed_payment_failure','mandate_unavailable','customer_action_required','payment_succeeded')",
            name="chk_platform_dunning_attempts_outcome",
        ),
        CheckConstraint(
            "(outcome='payment_succeeded' AND counts_toward_dunning IS FALSE) OR (outcome<>'payment_succeeded' AND counts_toward_dunning IS TRUE)",
            name="chk_platform_dunning_attempts_counting",
        ),
        CheckConstraint("evidence_sha256 ~ '^[0-9a-f]{64}$'", name="chk_platform_dunning_attempts_evidence_sha"),
        CheckConstraint("btrim(evidence_ref) <> ''", name="chk_platform_dunning_attempts_evidence_ref"),
        Index("ix_platform_dunning_attempts_case", "dunning_case_id", "attempt_number"),
    )


class PlatformNotificationDelivery(Base):
    __tablename__ = "platform_notification_deliveries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    dunning_case_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    notification_type: Mapped[str] = mapped_column(String(80), nullable=False)
    policy_code: Mapped[str] = mapped_column(String(80), nullable=False)
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    recipient_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'queued'"))
    scheduled_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String(180), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_error_safe: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))

    __table_args__ = (
        ForeignKeyConstraint(
            ["dunning_case_id", "organization_id"],
            ["platform_dunning_cases.id", "platform_dunning_cases.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_notification_deliveries_case_org",
        ),
        UniqueConstraint("id", "organization_id", name="uq_platform_notification_deliveries_id_org"),
        UniqueConstraint("organization_id", "dedupe_key", name="uq_platform_notification_deliveries_dedupe"),
        CheckConstraint(
            "notification_type IN ('payment_failed','retry_scheduled','grace_ending','access_limited','access_read_only','subscription_suspended','subscription_terminated','payment_recovered','mandate_expiring','mandate_revoked','payment_method_replaced')",
            name="chk_platform_notification_deliveries_type",
        ),
        CheckConstraint("channel IN ('email','sms','in_app','whatsapp')", name="chk_platform_notification_deliveries_channel"),
        CheckConstraint("status IN ('queued','sending','sent','failed','suppressed')", name="chk_platform_notification_deliveries_status"),
        CheckConstraint("recipient_hash ~ '^[0-9a-f]{64}$'", name="chk_platform_notification_deliveries_recipient_hash"),
        CheckConstraint("btrim(dedupe_key) <> ''", name="chk_platform_notification_deliveries_dedupe"),
        CheckConstraint("attempt_count >= 0", name="chk_platform_notification_deliveries_attempt_count"),
        CheckConstraint("status <> 'sent' OR sent_at IS NOT NULL", name="chk_platform_notification_deliveries_sent_shape"),
        Index("ix_platform_notification_deliveries_due", "status", "scheduled_at"),
        Index("ix_platform_notification_deliveries_org", "organization_id", "created_at"),
    )


PAY12_MODEL_TABLES = frozenset(
    {
        "platform_recurring_billing_jobs",
        "platform_dunning_cases",
        "platform_dunning_attempts",
        "platform_notification_deliveries",
    }
)
