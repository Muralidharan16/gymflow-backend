from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import Staff, get_current_active_staff
from app.platform_billing.domain.capability_decision import CapabilityDecision
from app.platform_billing.services.capability_authorization_service import (
    CapabilityAuthorizationService,
)


@dataclass(frozen=True)
class PlatformCapabilityContext:
    staff: Staff
    capability_key: str
    operation_class: str
    decision: CapabilityDecision | None = None
    enforcement_enabled: bool = False
    shadow_enabled: bool = False


def require_platform_capability(
    capability_key: str,
    operation_class: str,
) -> Callable[..., object]:
    async def dependency(
        request: Request,
        staff: Staff = Depends(get_current_active_staff),
        db: AsyncSession = Depends(get_db),
    ) -> PlatformCapabilityContext:
        raw_enforcement_enabled = settings.PLATFORM_BILLING_ENFORCEMENT is True
        raw_shadow_enabled = settings.PLATFORM_BILLING_SHADOW_RESOLVER is True
        shadow_enabled = raw_shadow_enabled or raw_enforcement_enabled
        enforcement_enabled = raw_enforcement_enabled and raw_shadow_enabled

        if not enforcement_enabled and not shadow_enabled:
            return PlatformCapabilityContext(
                staff=staff,
                capability_key=capability_key,
                operation_class=operation_class,
                enforcement_enabled=False,
                shadow_enabled=False,
            )

        service = CapabilityAuthorizationService(db)
        result = await service.authorize(
            organization_id=staff.org_id,
            capability_key=capability_key,
            operation_class=operation_class,
            correlation_id=str(getattr(request.state, "correlation_id", "")) or None,
        )

        if enforcement_enabled and not result.decision.allowed:
            raise _decision_http_exception(result.decision, request)

        return PlatformCapabilityContext(
            staff=staff,
            capability_key=capability_key,
            operation_class=operation_class,
            decision=result.decision,
            enforcement_enabled=enforcement_enabled,
            shadow_enabled=shadow_enabled,
        )

    return dependency


def _decision_http_exception(
    decision: CapabilityDecision,
    request: Request,
) -> HTTPException:
    status_code = _status_for_decision(decision.decision_code)
    return HTTPException(
        status_code=status_code,
        detail={
            "type": "https://errors.doers.app/platform-billing/access-restricted",
            "title": _title_for_decision(decision.decision_code),
            "status": status_code,
            "code": decision.decision_code,
            "detail": _detail_for_decision(decision.decision_code),
            "instance": str(request.url.path),
            "access_mode": decision.access_mode,
            "reason_code": decision.safe_reason_code,
            "recovery_actions": ["VIEW_PLAN_BILLING", "CONTACT_SUPPORT"],
        },
    )


def _status_for_decision(code: str) -> int:
    if code == "PLATFORM_USAGE_LIMIT_REACHED":
        return status.HTTP_409_CONFLICT
    if code in {"ACCESS_DECISION_UNAVAILABLE", "PLATFORM_PROJECTION_INVALID"}:
        return status.HTTP_503_SERVICE_UNAVAILABLE
    return status.HTTP_403_FORBIDDEN


def _title_for_decision(code: str) -> str:
    if code == "PLATFORM_USAGE_LIMIT_REACHED":
        return "This limit has been reached"
    if code in {"ACCESS_DECISION_UNAVAILABLE", "PLATFORM_PROJECTION_INVALID"}:
        return "This action is temporarily unavailable"
    return "This action is currently unavailable"


def _detail_for_decision(code: str) -> str:
    if code == "PLATFORM_USAGE_LIMIT_REACHED":
        return "Your current plan limit has been reached for this action."
    if code in {"ACCESS_DECISION_UNAVAILABLE", "PLATFORM_PROJECTION_INVALID"}:
        return "We could not safely confirm account access right now. Please try again shortly."
    return "Your account status does not currently allow this action."
