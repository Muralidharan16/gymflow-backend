from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.platform_billing.api.dependencies import (
    PlatformCapabilityContext,
    require_platform_capability,
)
from app.platform_billing.api.schemas import PlatformBillingSummaryResponse
from app.platform_billing.domain.capability import OperationClass
from app.platform_billing.observability.metrics import METRIC_NAMES, get_metrics
from app.platform_billing.services.billing_summary_service import (
    PlatformBillingSummaryService,
)

router = APIRouter(prefix="/api/v1/platform-billing", tags=["Platform Billing"])


@router.get("/summary", response_model=PlatformBillingSummaryResponse)
async def get_platform_billing_summary(
    context: PlatformCapabilityContext = Depends(
        require_platform_capability(
            "platform_billing.view",
            OperationClass.safe_read.value,
        )
    ),
    db: AsyncSession = Depends(get_db),
) -> PlatformBillingSummaryResponse:
    if settings.PLATFORM_BILLING_READ_API is not True:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "PLATFORM_BILLING_READ_API_DISABLED",
                "message": "Platform Billing read APIs are not enabled.",
            },
        )

    get_metrics().increment(
        METRIC_NAMES["read_api_total"],
        {
            "endpoint": "summary",
            "enabled": "true",
        },
    )
    service = PlatformBillingSummaryService(db)
    return await service.get_summary(context.staff.org_id)
