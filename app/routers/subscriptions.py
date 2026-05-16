# FIXED: [FIX 5] Added pagination (page, page_size) to subscription list routes
#        (member subscription history and plans listing).
from typing import List, Optional
from uuid import UUID
from math import ceil

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.core.deps import get_current_active_staff, require_gym_access
from app.core.deps import Staff
from app.schemas.common import Response, PaginatedResponse, MessageResponse
from app.schemas.subscription import (
    PlanResponse, PlanCreate, PlanUpdate,
    SubscriptionResponse, SubscriptionCreate, CancelRequest
)
from app.services.subscription_service import SubscriptionService
from app.services.plan_service import PlanService  # assuming exists or will be wired
from app.core.exceptions import NotFoundError, ValidationError
from app.models.subscription import MemberSubscription, SubscriptionPlan

router = APIRouter(prefix="/gyms/{gym_id}", tags=["Subscriptions"])


# ========== Plan Management ==========

@router.get("/plans", response_model=PaginatedResponse[PlanResponse])
async def list_plans(
    gym_id: UUID,
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(50, ge=1, le=200, description="Items per page"),
    current_staff: Staff = Depends(require_gym_access),
    db: AsyncSession = Depends(get_db)
):
    """
    List all active subscription plans for a gym with pagination.
    """
    # Count total
    count_query = (
        select(func.count())
        .select_from(SubscriptionPlan)
        .where(SubscriptionPlan.gym_id == gym_id, SubscriptionPlan.is_active == True)
    )
    total = (await db.execute(count_query)).scalar() or 0

    # Fetch page
    query = (
        select(SubscriptionPlan)
        .where(SubscriptionPlan.gym_id == gym_id, SubscriptionPlan.is_active == True)
        .order_by(SubscriptionPlan.price)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await db.execute(query)
    plans = result.scalars().all()

    return PaginatedResponse(
        data=[PlanResponse.model_validate(p) for p in plans],
        page=page,
        size=page_size,
        total=total,
        pages=ceil(total / page_size) if page_size else 0,
    )


@router.post("/plans", response_model=Response[PlanResponse])
async def create_plan(
    gym_id: UUID,
    data: PlanCreate,
    current_staff: Staff = Depends(require_gym_access),
    db: AsyncSession = Depends(get_db)
):
    """
    Create a new subscription plan (existing endpoint - keep).
    """
    service = PlanService(db)
    try:
        plan = await service.create_plan(gym_id, data, current_staff.id)
        await db.commit()
        return Response(data=PlanResponse.model_validate(plan))
    except ValidationError as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": str(e), "error_code": e.error_code}
        )


@router.put("/plans/{plan_id}", response_model=Response[PlanResponse])
async def update_plan(
    gym_id: UUID,
    plan_id: UUID,
    data: PlanUpdate,
    current_staff: Staff = Depends(require_gym_access),
    db: AsyncSession = Depends(get_db)
):
    """
    Update an existing subscription plan.
    """
    service = PlanService(db)
    try:
        plan = await service.update_plan(gym_id, plan_id, data, current_staff.id)
        await db.commit()
        return Response(data=PlanResponse.model_validate(plan))
    except NotFoundError as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": str(e), "error_code": "NOT_FOUND"}
        )
    except ValidationError as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": str(e), "error_code": e.error_code}
        )


@router.patch("/plans/{plan_id}/toggle", response_model=Response[PlanResponse])
async def toggle_plan_active(
    gym_id: UUID,
    plan_id: UUID,
    current_staff: Staff = Depends(require_gym_access),
    db: AsyncSession = Depends(get_db)
):
    """
    Toggle the is_active flag of a plan.
    """
    service = PlanService(db)
    try:
        plan = await service.toggle_plan_active(gym_id, plan_id, current_staff.id)
        await db.commit()
        return Response(data=PlanResponse.model_validate(plan))
    except NotFoundError as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": str(e), "error_code": "NOT_FOUND"}
        )


# ========== Member Subscriptions ==========

@router.post("/members/{member_id}/subscriptions", response_model=Response[SubscriptionResponse])
async def assign_plan_to_member(
    gym_id: UUID,
    member_id: UUID,
    data: SubscriptionCreate,
    current_staff: Staff = Depends(require_gym_access),
    db: AsyncSession = Depends(get_db)
):
    """
    Assign a subscription plan to a member (existing endpoint - keep).
    """
    service = SubscriptionService(db)
    try:
        subscription = await service.assign_subscription(
            gym_id=gym_id,
            member_id=member_id,
            plan_id=data.plan_id,
            start_date=data.start_date,
            end_date=data.end_date,
            price_paid=data.price_paid,
            created_by=current_staff.id
        )
        await db.commit()
        return Response(data=SubscriptionResponse.model_validate(subscription))
    except NotFoundError as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": str(e), "error_code": "NOT_FOUND"}
        )
    except ValidationError as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": str(e), "error_code": e.error_code}
        )


@router.get("/members/{member_id}/subscriptions", response_model=PaginatedResponse[SubscriptionResponse])
async def get_member_subscription_history(
    gym_id: UUID,
    member_id: UUID,
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(50, ge=1, le=200, description="Items per page"),
    current_staff: Staff = Depends(require_gym_access),
    db: AsyncSession = Depends(get_db)
):
    """
    Get all subscriptions (history) for a member with pagination.
    """
    base_filter = [
        MemberSubscription.member_id == member_id,
        MemberSubscription.gym_id == gym_id,
    ]

    # Total count
    count_query = (
        select(func.count())
        .select_from(MemberSubscription)
        .where(*base_filter)
    )
    total = (await db.execute(count_query)).scalar() or 0

    # Fetch page
    query = (
        select(MemberSubscription)
        .where(*base_filter)
        .options(selectinload(MemberSubscription.plan))
        .order_by(MemberSubscription.start_date.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await db.execute(query)
    subscriptions = result.scalars().all()

    return PaginatedResponse(
        data=[SubscriptionResponse.model_validate(s) for s in subscriptions],
        page=page,
        size=page_size,
        total=total,
        pages=ceil(total / page_size) if page_size else 0,
    )


@router.get("/members/{member_id}/subscriptions/active", response_model=Response[SubscriptionResponse])
async def get_active_subscription(
    gym_id: UUID,
    member_id: UUID,
    current_staff: Staff = Depends(require_gym_access),
    db: AsyncSession = Depends(get_db)
):
    """
    Get current active subscription for a member, or 404 if none.
    """
    service = SubscriptionService(db)
    try:
        subscription = await service.get_active_subscription(member_id, gym_id)
        if not subscription:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"message": "No active subscription found", "error_code": "NOT_FOUND"}
            )
        return Response(data=SubscriptionResponse.model_validate(subscription))
    except NotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": str(e), "error_code": "NOT_FOUND"}
        )


# ========== Subscription Actions ==========

@router.post("/subscriptions/{sub_id}/freeze", response_model=Response[SubscriptionResponse])
async def freeze_subscription(
    sub_id: UUID,
    gym_id: UUID,
    current_staff: Staff = Depends(require_gym_access),
    db: AsyncSession = Depends(get_db)
):
    """
    Freeze an active subscription (existing endpoint - keep).
    """
    service = SubscriptionService(db)
    try:
        subscription = await service.freeze_subscription(gym_id, sub_id, current_staff.id)
        await db.commit()
        return Response(data=SubscriptionResponse.model_validate(subscription))
    except NotFoundError as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": str(e), "error_code": "NOT_FOUND"}
        )
    except ValidationError as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": str(e), "error_code": e.error_code}
        )


@router.post("/subscriptions/{sub_id}/unfreeze", response_model=Response[SubscriptionResponse])
async def unfreeze_subscription(
    sub_id: UUID,
    gym_id: UUID,
    current_staff: Staff = Depends(require_gym_access),
    db: AsyncSession = Depends(get_db)
):
    """
    Unfreeze a frozen subscription.
    """
    service = SubscriptionService(db)
    try:
        subscription = await service.unfreeze_subscription(gym_id, sub_id, current_staff.id)
        await db.commit()
        return Response(data=SubscriptionResponse.model_validate(subscription))
    except NotFoundError as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": str(e), "error_code": "NOT_FOUND"}
        )
    except ValidationError as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": str(e), "error_code": e.error_code}
        )


@router.post("/subscriptions/{sub_id}/cancel", response_model=Response[SubscriptionResponse])
async def cancel_subscription(
    sub_id: UUID,
    gym_id: UUID,
    data: CancelRequest,
    current_staff: Staff = Depends(require_gym_access),
    db: AsyncSession = Depends(get_db)
):
    """
    Cancel an active or frozen subscription with a reason.
    """
    service = SubscriptionService(db)
    try:
        subscription = await service.cancel_subscription(gym_id, sub_id, data.reason, current_staff.id)
        await db.commit()
        return Response(data=SubscriptionResponse.model_validate(subscription))
    except NotFoundError as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": str(e), "error_code": "NOT_FOUND"}
        )
    except ValidationError as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": str(e), "error_code": e.error_code}
        )