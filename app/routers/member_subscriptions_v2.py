import uuid
from math import ceil

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import Staff, require_org_admin
from app.models.member_subscription_v2 import ModernSubscriptionStatus
from app.schemas.member_subscription_v2 import SubscriptionCreate, SubscriptionListResponse, SubscriptionResponse
from app.services.member_subscription_v2_service import MemberSubscriptionV2Service

router = APIRouter(prefix="/organizations/{org_id}/subscriptions", tags=["Modern Subscriptions"])


def _enforce_path_org(org_id: uuid.UUID, staff: Staff) -> None:
    if staff.org_id != org_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied to this organization")


@router.post("", response_model=SubscriptionResponse, status_code=status.HTTP_201_CREATED)
async def create_subscription(
    org_id: uuid.UUID,
    data: SubscriptionCreate,
    db: AsyncSession = Depends(get_db),
    staff: Staff = Depends(require_org_admin),
):
    _enforce_path_org(org_id, staff)
    service = MemberSubscriptionV2Service(db)
    try:
        subscription = await service.create_subscription(org_id, data, staff.id)
        await db.commit()
        return subscription
    except Exception:
        await db.rollback()
        raise


@router.get("", response_model=SubscriptionListResponse)
async def list_subscriptions(
    org_id: uuid.UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    status_filter: ModernSubscriptionStatus | None = Query(None, alias="status"),
    branch_id: uuid.UUID | None = None,
    member_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    staff: Staff = Depends(require_org_admin),
):
    _enforce_path_org(org_id, staff)
    service = MemberSubscriptionV2Service(db)
    subscriptions, total = await service.list_subscriptions(
        org_id=org_id,
        page=page,
        size=page_size,
        status_filter=status_filter,
        branch_id=branch_id,
        member_id=member_id,
    )
    return SubscriptionListResponse(
        data=[SubscriptionResponse.model_validate(sub) for sub in subscriptions],
        total=total,
        page=page,
        size=page_size,
        pages=ceil(total / page_size) if page_size else 0,
    )


@router.get("/{subscription_id}", response_model=SubscriptionResponse)
async def get_subscription(
    org_id: uuid.UUID,
    subscription_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    staff: Staff = Depends(require_org_admin),
):
    _enforce_path_org(org_id, staff)
    service = MemberSubscriptionV2Service(db)
    subscription = await service.get_subscription(org_id, subscription_id)
    if not subscription:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subscription not found")
    return subscription
