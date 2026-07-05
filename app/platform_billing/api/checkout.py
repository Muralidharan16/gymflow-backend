from __future__ import annotations

import uuid
import re
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.platform_billing.api.dependencies import PlatformCapabilityContext, require_platform_capability
from app.platform_billing.api.schemas import (
    CreateCheckoutSessionRequest,
    CreateCheckoutSessionResponse,
    GetCheckoutOperationResponse,
)
from app.platform_billing.domain.capability import OperationClass
from app.platform_billing.services.checkout import (
    CheckoutConflictError,
    CheckoutPlanNotFoundError,
    CheckoutPrerequisiteError,
    PlatformCheckoutService,
)

router = APIRouter(prefix="/api/v1/platform-billing", tags=["Platform Billing Checkout"])
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def _verify_fake_checkout_enabled() -> None:
    if not settings.PLATFORM_BILLING_FAKE_CHECKOUT_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "PLATFORM_BILLING_FAKE_CHECKOUT_DISABLED", "message": "Fake checkout is disabled."},
        )
    if settings.ENVIRONMENT not in ("development", "test"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "PLATFORM_BILLING_FAKE_CHECKOUT_DISABLED", "message": "Fake checkout is disabled in this environment."},
        )
    if settings.PLATFORM_BILLING_PROVIDER_MODE != "fake":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "PLATFORM_BILLING_FAKE_CHECKOUT_DISABLED", "message": "Fake checkout requires fake provider mode."},
        )

def require_bearer_token(request: Request) -> None:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token required for this operation."
        )


def normalize_idempotency_key(raw_key: str = Header(..., alias="Idempotency-Key")) -> str:
    normalized_key = raw_key.strip()
    if not (16 <= len(normalized_key) <= 160):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Idempotency-Key length must be between 16 and 160 characters"
        )
    if not IDEMPOTENCY_KEY_PATTERN.fullmatch(normalized_key):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Idempotency-Key contains invalid characters"
        )
    return normalized_key

@router.post("/checkout-sessions", response_model=CreateCheckoutSessionResponse)
async def create_checkout_session(
    request: CreateCheckoutSessionRequest,
    req: Request,
    idempotency_key: str = Depends(normalize_idempotency_key),
    context: PlatformCapabilityContext = Depends(
        require_platform_capability("platform_billing.change_plan", OperationClass.financial.value)
    ),
    db: AsyncSession = Depends(get_db),
) -> CreateCheckoutSessionResponse:
    require_bearer_token(req)
    _verify_fake_checkout_enabled()

    service = PlatformCheckoutService(db)
    try:
        return await service.create_checkout_session(
            request=request,
            organization_id=context.staff.org_id,
            idempotency_key=idempotency_key,
        )
    except CheckoutConflictError as e:
        if str(e) == "versioned_flow_required":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "VERSIONED_FLOW_REQUIRED", "message": "A current subscription exists."},
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "IDEMPOTENCY_REQUEST_CONFLICT", "message": "Idempotency conflict."},
        )
    except CheckoutPlanNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "PLAN_NOT_FOUND", "message": "Plan or price not found."},
        )
    except CheckoutPrerequisiteError as e:
        code = str(e).upper()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": code, "message": "Checkout prerequisite error."},
        )


@router.get("/checkout-operations/{operation_id}", response_model=GetCheckoutOperationResponse)
async def get_checkout_operation(
    operation_id: uuid.UUID,
    context: PlatformCapabilityContext = Depends(
        require_platform_capability("platform_billing.view", OperationClass.safe_read.value)
    ),
    db: AsyncSession = Depends(get_db),
) -> GetCheckoutOperationResponse:
    # GET shouldn't necessarily require creation flag, but user says:
    # "explicit non-production/fake-operation safety"
    if settings.ENVIRONMENT not in ("development", "test"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "PLATFORM_BILLING_CHECKOUT_DISABLED", "message": "Checkout reads are restricted to non-production."},
        )

    service = PlatformCheckoutService(db)
    result = await service.get_checkout_operation(
        operation_id=operation_id,
        organization_id=context.staff.org_id,
    )
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return result
