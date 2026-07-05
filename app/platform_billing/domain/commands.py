"""
app/platform_billing/domain/commands.py
========================================
Platform Billing command DTOs.

Commands carry the data required to invoke a domain operation.
They are plain data objects with no behaviour or external
dependencies.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class CreateCheckoutSession:
    plan_version_id: uuid.UUID
    price_id: uuid.UUID
    organization_id: uuid.UUID
    billing_account_id: uuid.UUID
    idempotency_key: str
    request_hash: str
    requested_by: uuid.UUID


@dataclass(frozen=True)
class ChangeSubscription:
    subscription_id: uuid.UUID
    organization_id: uuid.UUID
    change_type: str
    from_plan_version_id: Optional[uuid.UUID]
    to_plan_version_id: Optional[uuid.UUID]
    from_price_id: Optional[uuid.UUID]
    to_price_id: Optional[uuid.UUID]
    requested_effective_at: datetime
    idempotency_key: str
    request_hash: str
    expected_subscription_version: int
    requested_by: uuid.UUID


@dataclass(frozen=True)
class CancelSubscription:
    subscription_id: uuid.UUID
    organization_id: uuid.UUID
    cancel_at_period_end: bool
    idempotency_key: str
    request_hash: str
    expected_subscription_version: int
    requested_by: uuid.UUID


@dataclass(frozen=True)
class UndoCancelSubscription:
    subscription_id: uuid.UUID
    organization_id: uuid.UUID
    idempotency_key: str
    request_hash: str
    expected_subscription_version: int
    requested_by: uuid.UUID


@dataclass(frozen=True)
class UpdatePaymentMethod:
    billing_account_id: uuid.UUID
    organization_id: uuid.UUID
    idempotency_key: str
    request_hash: str
    expected_billing_account_version: int
    requested_by: uuid.UUID


@dataclass(frozen=True)
class IssueRefund:
    payment_attempt_id: uuid.UUID
    organization_id: uuid.UUID
    amount_minor: int
    currency_code: str
    reason_code: str
    reason_detail: str
    ticket_reference: str
    idempotency_key: str
    request_hash: str
    requested_by: uuid.UUID


@dataclass(frozen=True)
class CreateAccessOverride:
    organization_id: uuid.UUID
    override_type: str
    capability_or_feature_key: Optional[str]
    value_json: dict
    reason_code: str
    reason_detail: str
    starts_at: datetime
    expires_at: datetime
    ticket_reference: str
    requested_by: uuid.UUID
