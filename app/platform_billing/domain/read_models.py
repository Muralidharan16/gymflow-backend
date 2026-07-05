from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Mapping

from app.platform_billing.domain.money import Money


def _immutable_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return MappingProxyType(dict(value or {}))


def _require_aware_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value


@dataclass(frozen=True)
class ProductRead:
    id: uuid.UUID
    code: str
    name: str
    description: str | None
    status: str
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _require_aware_utc(self.created_at, "created_at")
        _require_aware_utc(self.updated_at, "updated_at")


@dataclass(frozen=True)
class PolicyVersionRead:
    id: uuid.UUID
    code: str
    policy_type: str
    version: int
    payload: Mapping[str, Any]
    status: str
    payload_sha256: str | None
    published_at: datetime | None
    created_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _immutable_mapping(self.payload))
        _require_aware_utc(self.created_at, "created_at")
        if self.published_at is not None:
            _require_aware_utc(self.published_at, "published_at")


@dataclass(frozen=True)
class PriceRead:
    id: uuid.UUID
    plan_version_id: uuid.UUID
    code: str
    money: Money
    country_code: str | None
    billing_interval: str
    interval_count: int
    tax_behavior: str
    status: str
    valid_from: datetime | None
    valid_until: datetime | None
    provider_price_hint: str | None
    published_at: datetime | None
    created_at: datetime

    def __post_init__(self) -> None:
        _require_aware_utc(self.created_at, "created_at")
        for field_name in ("valid_from", "valid_until", "published_at"):
            value = getattr(self, field_name)
            if value is not None:
                _require_aware_utc(value, field_name)


@dataclass(frozen=True)
class FeatureDefinitionRead:
    id: uuid.UUID
    key: str
    display_name: str
    value_type: str
    enforcement_mode: str
    unit: str | None
    description: str
    status: str
    created_at: datetime

    def __post_init__(self) -> None:
        _require_aware_utc(self.created_at, "created_at")


@dataclass(frozen=True)
class PlanEntitlementRead:
    id: uuid.UUID
    plan_version_id: uuid.UUID
    feature_definition_id: uuid.UUID
    feature_key: str | None
    value_type: str
    value_boolean: bool | None
    value_integer: int | None
    value_string: str | None
    value_json: Mapping[str, Any] | None
    created_at: datetime

    def __post_init__(self) -> None:
        if self.value_json is not None:
            object.__setattr__(self, "value_json", _immutable_mapping(self.value_json))
        _require_aware_utc(self.created_at, "created_at")


@dataclass(frozen=True)
class PlanVersionRead:
    id: uuid.UUID
    product_id: uuid.UUID
    version: int
    code: str
    display_name: str
    description: str | None
    status: str
    trial_policy_version_id: uuid.UUID | None
    dunning_policy_version_id: uuid.UUID | None
    cancellation_policy_version_id: uuid.UUID | None
    downgrade_policy_version_id: uuid.UUID | None
    metadata_json: Mapping[str, Any]
    published_at: datetime | None
    retired_at: datetime | None
    created_at: datetime
    updated_at: datetime
    prices: tuple[PriceRead, ...] = ()
    entitlements: tuple[PlanEntitlementRead, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata_json", _immutable_mapping(self.metadata_json))
        _require_aware_utc(self.created_at, "created_at")
        _require_aware_utc(self.updated_at, "updated_at")
        for field_name in ("published_at", "retired_at"):
            value = getattr(self, field_name)
            if value is not None:
                _require_aware_utc(value, field_name)


@dataclass(frozen=True)
class BillingAccountRead:
    id: uuid.UUID
    organization_id: uuid.UUID
    status: str
    legal_name: str
    billing_email: str
    billing_phone_e164: str | None
    country_code: str
    default_currency_code: str
    address_line1: str | None
    address_line2: str | None
    city: str
    subdivision: str | None
    postal_code: str | None
    tax_registration_type: str | None
    tax_registration_masked: str | None
    tax_verified: bool
    tax_verified_at: datetime | None
    invoice_locale: str
    created_at: datetime
    updated_at: datetime
    version: int

    def __post_init__(self) -> None:
        _require_aware_utc(self.created_at, "created_at")
        _require_aware_utc(self.updated_at, "updated_at")
        if self.tax_verified_at is not None:
            _require_aware_utc(self.tax_verified_at, "tax_verified_at")


@dataclass(frozen=True)
class SubscriptionRead:
    id: uuid.UUID
    organization_id: uuid.UUID
    billing_account_id: uuid.UUID
    status: str
    current_plan_version_id: uuid.UUID
    current_price_id: uuid.UUID | None
    policy_snapshot_json: Mapping[str, Any]
    started_at: datetime
    current_period_start: datetime
    current_period_end: datetime
    cancel_at_period_end: bool
    cancellation_requested_at: datetime | None
    cancellation_effective_at: datetime | None
    canceled_at: datetime | None
    ended_at: datetime | None
    created_at: datetime
    updated_at: datetime
    version: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "policy_snapshot_json", _immutable_mapping(self.policy_snapshot_json))
        for field_name in (
            "started_at",
            "current_period_start",
            "current_period_end",
            "created_at",
            "updated_at",
            "cancellation_requested_at",
            "cancellation_effective_at",
            "canceled_at",
            "ended_at",
        ):
            value = getattr(self, field_name)
            if value is not None:
                _require_aware_utc(value, field_name)


@dataclass(frozen=True)
class SubscriptionItemRead:
    id: uuid.UUID
    organization_id: uuid.UUID
    subscription_id: uuid.UUID
    item_type: str
    plan_version_id: uuid.UUID
    price_id: uuid.UUID | None
    quantity: int
    effective_from: datetime
    effective_until: datetime | None
    status: str
    created_at: datetime
    updated_at: datetime
    version: int

    def __post_init__(self) -> None:
        for field_name in ("effective_from", "effective_until", "created_at", "updated_at"):
            value = getattr(self, field_name)
            if value is not None:
                _require_aware_utc(value, field_name)


@dataclass(frozen=True)
class SubscriptionPeriodRead:
    id: uuid.UUID
    organization_id: uuid.UUID
    subscription_id: uuid.UUID
    period_type: str
    status: str
    starts_at: datetime
    ends_at: datetime
    source_invoice_id: uuid.UUID | None
    source_change_id: uuid.UUID | None
    source_override_id: uuid.UUID | None
    metadata_json: Mapping[str, Any]
    created_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata_json", _immutable_mapping(self.metadata_json))
        for field_name in ("starts_at", "ends_at", "created_at"):
            _require_aware_utc(getattr(self, field_name), field_name)


@dataclass(frozen=True)
class SubscriptionEventRead:
    id: uuid.UUID
    organization_id: uuid.UUID
    subscription_id: uuid.UUID
    sequence_number: int
    event_type: str
    occurred_at: datetime
    recorded_at: datetime
    actor_type: str
    actor_id: uuid.UUID | None
    source_type: str
    source_id: uuid.UUID | None
    evidence_sha256: str | None
    payload_json: Mapping[str, Any]
    payload_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload_json", _immutable_mapping(self.payload_json))
        _require_aware_utc(self.occurred_at, "occurred_at")
        _require_aware_utc(self.recorded_at, "recorded_at")


@dataclass(frozen=True)
class AuditEventRead:
    id: uuid.UUID
    recorded_at: datetime
    organization_id: uuid.UUID
    actor_type: str
    actor_id: uuid.UUID | None
    action: str
    target_type: str
    target_id: uuid.UUID | None
    request_id: uuid.UUID | None
    correlation_id: uuid.UUID | None
    before_hash: str | None
    after_hash: str | None
    metadata_redacted_json: Mapping[str, Any]
    outcome: str
    reason_code: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata_redacted_json", _immutable_mapping(self.metadata_redacted_json))
        _require_aware_utc(self.recorded_at, "recorded_at")
