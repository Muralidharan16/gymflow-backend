from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.platform_billing.api.checkout import normalize_idempotency_key, require_bearer_token
from app.platform_billing.api.dependencies import PlatformCapabilityContext, require_platform_capability
from app.platform_billing.api.schemas import CreateFakeCheckoutSimulationRequest, FakeCheckoutSimulationResponse
from app.platform_billing.domain.capability import OperationClass
from app.platform_billing.services.checkout_simulation import (
    CheckoutSimulationConflictError,
    CheckoutSimulationInvalidStateError,
    CheckoutSimulationNotFoundError,
    CheckoutSimulationServices,
    PlatformCheckoutSimulationService,
    default_simulation_services,
)

router = APIRouter(prefix="/api/v1/platform-billing", tags=["Platform Billing Fake Checkout Simulation"])


def _verify_fake_checkout_simulation_enabled() -> None:
    if not settings.PLATFORM_BILLING_FAKE_CHECKOUT_ENABLED:
        raise _disabled()
    if not settings.PLATFORM_BILLING_FAKE_CHECKOUT_SIMULATION_ENABLED:
        raise _disabled()
    if settings.ENVIRONMENT not in ("development", "test"):
        raise _disabled()
    if settings.PLATFORM_BILLING_PROVIDER_MODE != "fake":
        raise _disabled()


def _verify_fake_checkout_simulation_readable() -> None:
    if settings.ENVIRONMENT not in ("development", "test"):
        raise _disabled()


def _disabled() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "PLATFORM_BILLING_FAKE_CHECKOUT_SIMULATION_DISABLED", "message": "Fake checkout simulation is disabled."},
    )


@router.post("/fake-checkout-simulations", response_model=FakeCheckoutSimulationResponse)
async def create_fake_checkout_simulation(
    body: CreateFakeCheckoutSimulationRequest,
    req: Request,
    idempotency_key: str = Depends(normalize_idempotency_key),
    context: PlatformCapabilityContext = Depends(
        require_platform_capability("platform_billing.change_plan", OperationClass.financial.value)
    ),
    db: AsyncSession = Depends(get_db),
    simulation_services: CheckoutSimulationServices = Depends(default_simulation_services),
) -> FakeCheckoutSimulationResponse:
    require_bearer_token(req)
    _verify_fake_checkout_simulation_enabled()
    service = PlatformCheckoutSimulationService(db, simulation_services=simulation_services)
    try:
        return await service.create_simulation(
            checkout_operation_id=body.checkout_operation_id,
            requested_outcome=body.requested_outcome,
            organization_id=context.staff.org_id,
            idempotency_key=idempotency_key,
        )
    except CheckoutSimulationNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    except CheckoutSimulationConflictError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "IDEMPOTENCY_REQUEST_CONFLICT", "message": "Simulation idempotency conflict."},
        )
    except CheckoutSimulationInvalidStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": str(exc).upper(), "message": "Fake checkout simulation invalid state."},
        )


@router.get("/fake-checkout-simulations/{simulation_operation_id}", response_model=FakeCheckoutSimulationResponse)
async def get_fake_checkout_simulation(
    simulation_operation_id: uuid.UUID,
    context: PlatformCapabilityContext = Depends(
        require_platform_capability("platform_billing.view", OperationClass.safe_read.value)
    ),
    db: AsyncSession = Depends(get_db),
) -> FakeCheckoutSimulationResponse:
    _verify_fake_checkout_simulation_readable()
    service = PlatformCheckoutSimulationService(db)
    result = await service.get_simulation(
        simulation_operation_id=simulation_operation_id,
        organization_id=context.staff.org_id,
    )
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return result
