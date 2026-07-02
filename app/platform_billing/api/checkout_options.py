from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.platform_billing.api.dependencies import PlatformCapabilityContext, require_platform_capability
from app.platform_billing.api.schemas import PlatformBillingCheckoutOptionsResponse
from app.platform_billing.domain.capability import OperationClass
from app.platform_billing.services.checkout_options import PlatformBillingCheckoutOptionsService


router = APIRouter(prefix="/api/v1/platform-billing", tags=["Platform Billing Checkout Options"])


@router.get("/checkout-options", response_model=PlatformBillingCheckoutOptionsResponse)
async def get_checkout_options(
    response: Response,
    context: PlatformCapabilityContext = Depends(
        require_platform_capability("platform_billing.view", OperationClass.safe_read.value)
    ),
    db: AsyncSession = Depends(get_db),
) -> PlatformBillingCheckoutOptionsResponse:
    response.headers["Cache-Control"] = "no-store"
    service = PlatformBillingCheckoutOptionsService(db)
    return await service.get_checkout_options(organization_id=context.staff.org_id)
