"""
app/platform_billing/domain/enums.py
=====================================
Platform Billing domain enumerations.

These are separate from facility-commerce enums (app.models.enums)
and must never be imported into member-subscription or payment code.

V3.1 requires aggregate-specific catalogue status enums and the
canonical PAYMENT_OVERDUE access reason (not PAYMENT_PAST_DUE).
"""

from enum import Enum


# ── Subscription contract status ────────────────────────────────────

class SubscriptionContractStatus(str, Enum):
    trialing = "trialing"
    active = "active"
    past_due = "past_due"
    pause_scheduled = "pause_scheduled"
    paused = "paused"
    cancel_scheduled = "cancel_scheduled"
    canceled = "canceled"
    expired = "expired"


# ── Subscription change status ──────────────────────────────────────

class SubscriptionChangeStatus(str, Enum):
    requested = "requested"
    validated = "validated"
    provider_pending = "provider_pending"
    scheduled = "scheduled"
    applied = "applied"
    canceled = "canceled"
    failed_retryable = "failed_retryable"
    failed_final = "failed_final"


# ── Subscription change type ────────────────────────────────────────

class SubscriptionChangeType(str, Enum):
    upgrade = "upgrade"
    downgrade = "downgrade"
    cancel = "cancel"
    undo_cancel = "undo_cancel"
    pause = "pause"
    resume = "resume"
    reactivate = "reactivate"


# ── Subscription period type ────────────────────────────────────────

class SubscriptionPeriodType(str, Enum):
    trial = "trial"
    paid = "paid"
    grace = "grace"
    extension = "extension"
    post_cancel_read_only = "post_cancel_read_only"


# ── Invoice status ──────────────────────────────────────────────────

class PlatformInvoiceStatus(str, Enum):
    draft = "draft"
    open = "open"
    paid = "paid"
    void = "void"
    uncollectible = "uncollectible"


# ── Payment attempt status ──────────────────────────────────────────

class PaymentAttemptStatus(str, Enum):
    created = "created"
    requires_customer_action = "requires_customer_action"
    processing = "processing"
    succeeded = "succeeded"
    failed = "failed"
    canceled = "canceled"
    partially_refunded = "partially_refunded"
    refunded = "refunded"


# ── Platform access mode ────────────────────────────────────────────

class PlatformAccessMode(str, Enum):
    full = "full"
    limited_write = "limited_write"
    read_only = "read_only"
    billing_only = "billing_only"
    blocked = "blocked"


# ── Access reason codes (V3.1 canonical identifiers) ─────────────────

class AccessReasonCode(str, Enum):
    trial_active = "TRIAL_ACTIVE"
    paid_period_active = "PAID_PERIOD_ACTIVE"
    payment_grace = "PAYMENT_GRACE"
    trial_grace = "TRIAL_GRACE"
    over_limit = "OVER_LIMIT"
    payment_overdue = "PAYMENT_OVERDUE"
    subscription_expired = "SUBSCRIPTION_EXPIRED"
    compliance_suspension = "COMPLIANCE_SUSPENSION"
    security_suspension = "SECURITY_SUSPENSION"
    manual_override = "MANUAL_OVERRIDE"
    organization_closed = "ORGANIZATION_CLOSED"
    no_active_service_period = "NO_ACTIVE_SERVICE_PERIOD"
    state_inconsistent = "BILLING_STATE_REVIEW_REQUIRED"


# ── Billing interval ────────────────────────────────────────────────

class BillingInterval(str, Enum):
    month = "month"
    year = "year"
    one_time = "one_time"


# ── Tax behaviour ───────────────────────────────────────────────────

class TaxBehavior(str, Enum):
    exclusive = "exclusive"
    inclusive = "inclusive"
    not_applicable = "not_applicable"


# ── Aggregate-specific catalogue statuses (V3.1 §5.1) ───────────────

class ProductStatus(str, Enum):
    draft = "draft"
    active = "active"
    retired = "retired"


class PolicyVersionStatus(str, Enum):
    draft = "draft"
    published = "published"
    retired = "retired"


class PlanVersionStatus(str, Enum):
    draft = "draft"
    published = "published"
    retired = "retired"


class PriceStatus(str, Enum):
    draft = "draft"
    active = "active"
    retired = "retired"


class FeatureDefinitionStatus(str, Enum):
    active = "active"
    retired = "retired"


# ── Billing account status ──────────────────────────────────────────

class BillingAccountStatus(str, Enum):
    active = "active"
    closed = "closed"


# ── Provider operation status ───────────────────────────────────────

class ProviderOperationStatus(str, Enum):
    reserved = "reserved"
    in_flight = "in_flight"
    succeeded = "succeeded"
    failed_retryable = "failed_retryable"
    failed_final = "failed_final"
    unknown = "unknown"


# ── Webhook processing status ──────────────────────────────────────

class WebhookProcessingStatus(str, Enum):
    received = "received"
    processing = "processing"
    processed = "processed"
    ignored = "ignored"
    retry = "retry"
    dead_letter = "dead_letter"


# ── Capability operation class ──────────────────────────────────────

class CapabilityOperationClass(str, Enum):
    read = "read"
    modify_existing = "modify_existing"
    increase_capacity = "increase_capacity"
    destructive = "destructive"
    financial = "financial"
    recovery = "recovery"


# ── Recovery action identifiers ────────────────────────────────────

class RecoveryAction(str, Enum):
    VIEW_PLAN_BILLING = "VIEW_PLAN_BILLING"
    UPDATE_PAYMENT_METHOD = "UPDATE_PAYMENT_METHOD"
    COMPLETE_PAYMENT_ACTION = "COMPLETE_PAYMENT_ACTION"
    DOWNLOAD_INVOICES = "DOWNLOAD_INVOICES"
    CONTACT_SUPPORT = "CONTACT_SUPPORT"
    EXPORT_DATA = "EXPORT_DATA"
    UNDO_CANCELLATION = "UNDO_CANCELLATION"
