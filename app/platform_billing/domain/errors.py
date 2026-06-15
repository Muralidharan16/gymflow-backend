"""
app/platform_billing/domain/errors.py
======================================
Platform Billing domain-level error codes.

These produce structured RFC 9457 problem details.
Module name prefixed with PLATFORM_BILLING_ to avoid collision
with facility-commerce error codes.
"""

from enum import Enum
from typing import Mapping


class PlatformBillingErrorCode(str, Enum):
    PLATFORM_ACCESS_RESTRICTED = "PLATFORM_ACCESS_RESTRICTED"
    PLATFORM_FEATURE_NOT_INCLUDED = "PLATFORM_FEATURE_NOT_INCLUDED"
    ENTITLEMENT_LIMIT_REACHED = "ENTITLEMENT_LIMIT_REACHED"
    PRECONDITION_REQUIRED = "PRECONDITION_REQUIRED"
    PRECONDITION_FAILED = "PRECONDITION_FAILED"
    RESOURCE_VERSION_CONFLICT = "RESOURCE_VERSION_CONFLICT"
    IDEMPOTENCY_KEY_REQUIRED = "IDEMPOTENCY_KEY_REQUIRED"
    IDEMPOTENCY_REQUEST_CONFLICT = "IDEMPOTENCY_REQUEST_CONFLICT"
    BILLING_ACCOUNT_INCOMPLETE = "BILLING_ACCOUNT_INCOMPLETE"
    PLAN_NOT_AVAILABLE = "PLAN_NOT_AVAILABLE"
    PLAN_CHANGE_PREVIEW_EXPIRED = "PLAN_CHANGE_PREVIEW_EXPIRED"
    PAYMENT_CONFIRMATION_PENDING = "PAYMENT_CONFIRMATION_PENDING"
    PROVIDER_TEMPORARILY_UNAVAILABLE = "PROVIDER_TEMPORARILY_UNAVAILABLE"
    ACCESS_DECISION_UNAVAILABLE = "ACCESS_DECISION_UNAVAILABLE"
    RECENT_AUTHENTICATION_REQUIRED = "RECENT_AUTHENTICATION_REQUIRED"
    MFA_REQUIRED = "MFA_REQUIRED"
    RETAINED_FINANCIAL_RECORDS = "RETAINED_FINANCIAL_RECORDS"


ERROR_HTTP_STATUS: Mapping[PlatformBillingErrorCode, int] = {
    PlatformBillingErrorCode.PLATFORM_ACCESS_RESTRICTED: 403,
    PlatformBillingErrorCode.PLATFORM_FEATURE_NOT_INCLUDED: 403,
    PlatformBillingErrorCode.ENTITLEMENT_LIMIT_REACHED: 409,
    PlatformBillingErrorCode.PRECONDITION_REQUIRED: 428,
    PlatformBillingErrorCode.PRECONDITION_FAILED: 412,
    PlatformBillingErrorCode.RESOURCE_VERSION_CONFLICT: 409,
    PlatformBillingErrorCode.IDEMPOTENCY_KEY_REQUIRED: 400,
    PlatformBillingErrorCode.IDEMPOTENCY_REQUEST_CONFLICT: 409,
    PlatformBillingErrorCode.BILLING_ACCOUNT_INCOMPLETE: 422,
    PlatformBillingErrorCode.PLAN_NOT_AVAILABLE: 422,
    PlatformBillingErrorCode.PLAN_CHANGE_PREVIEW_EXPIRED: 409,
    PlatformBillingErrorCode.PAYMENT_CONFIRMATION_PENDING: 202,
    PlatformBillingErrorCode.PROVIDER_TEMPORARILY_UNAVAILABLE: 503,
    PlatformBillingErrorCode.ACCESS_DECISION_UNAVAILABLE: 503,
    PlatformBillingErrorCode.RECENT_AUTHENTICATION_REQUIRED: 401,
    PlatformBillingErrorCode.MFA_REQUIRED: 403,
    PlatformBillingErrorCode.RETAINED_FINANCIAL_RECORDS: 409,
}


class PlatformBillingError(Exception):

    def __init__(
        self,
        code: PlatformBillingErrorCode,
        detail: str = "",
        correlation_id: str | None = None,
    ):
        self.code = code
        self.detail = detail
        self.correlation_id = correlation_id
        self.http_status = ERROR_HTTP_STATUS[code]
        super().__init__(detail or code.value)
