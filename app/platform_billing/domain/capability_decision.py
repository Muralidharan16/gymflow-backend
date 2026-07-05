from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class CapabilityDecisionCode(str, Enum):
    ALLOWED = "ALLOWED"
    PLATFORM_ACCESS_DENIED = "PLATFORM_ACCESS_DENIED"
    PLATFORM_ENTITLEMENT_REQUIRED = "PLATFORM_ENTITLEMENT_REQUIRED"
    PLATFORM_USAGE_LIMIT_REACHED = "PLATFORM_USAGE_LIMIT_REACHED"
    ACCESS_DECISION_UNAVAILABLE = "ACCESS_DECISION_UNAVAILABLE"
    PLATFORM_PROJECTION_INVALID = "PLATFORM_PROJECTION_INVALID"
    PLATFORM_CAPABILITY_UNKNOWN = "PLATFORM_CAPABILITY_UNKNOWN"


@dataclass(frozen=True)
class CapabilityEntitlementValue:
    feature_key: str
    value_type: str
    value: bool | int | str | dict[str, Any]


@dataclass(frozen=True)
class CapabilityUsageValue:
    metric_key: str
    current_value: int


@dataclass(frozen=True)
class CapabilityDecisionInput:
    organization_id: str
    capability_key: str
    operation_class: str
    decision_timestamp: datetime
    access_mode: str | None
    projection_freshness: str
    entitlements: tuple[CapabilityEntitlementValue, ...] = ()
    usage: tuple[CapabilityUsageValue, ...] = ()
    fallback_used: bool = False
    recompute_attempted: bool = False
    source_subscription_version: int | None = None
    unsupported_addon_composition: bool = False


@dataclass(frozen=True)
class CapabilityDecision:
    allowed: bool
    decision_code: str
    safe_reason_code: str
    capability_key: str
    operation_class: str
    access_mode: str | None
    required_feature_key: str | None
    entitlement_value: bool | int | str | dict[str, Any] | None
    usage_value: int | None
    limit_value: int | None
    projection_freshness: str
    fallback_used: bool
    recompute_attempted: bool
    source_subscription_version: int | None
    decision_timestamp: datetime
