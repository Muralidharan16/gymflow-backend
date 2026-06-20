from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class PlatformBillingAccessSummary(BaseModel):
    mode: str
    safe_reason_code: str
    effective_from: datetime | None = None
    next_transition_at: datetime | None = None
    recovery_actions: list[str]
    projection_freshness: str


class PlatformBillingPlanSummary(BaseModel):
    code: str | None = None
    display_name: str | None = None
    status: str | None = None


class PlatformBillingPeriodSummary(BaseModel):
    period_start: datetime | None = None
    period_end: datetime | None = None
    subscription_status: str | None = None
    cancel_at_period_end: bool = False


class PlatformBillingEntitlementSummary(BaseModel):
    key: str
    value_type: str
    value: bool | int | str | dict[str, Any]


class PlatformBillingUsageSummary(BaseModel):
    key: str
    current: int
    limit: int | None = None
    over_limit: bool | None = None
    stale_after: datetime | None = None


class PlatformBillingDecisionAvailability(BaseModel):
    available: bool
    reason: str | None = None


class PlatformBillingSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    organization_id: str
    access: PlatformBillingAccessSummary
    plan: PlatformBillingPlanSummary
    billing_period: PlatformBillingPeriodSummary
    entitlements: list[PlatformBillingEntitlementSummary]
    usage: list[PlatformBillingUsageSummary]
    decision_availability: PlatformBillingDecisionAvailability
    server_time: datetime
