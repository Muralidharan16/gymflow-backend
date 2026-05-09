from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_active_staff, require_gym_access
from app.models.staff import Staff
from app.schemas.common import Response, PaginatedResponse, MessageResponse
from app.schemas.subscription import (
    SubscriptionPlanResponse, SubscriptionPlanCreate, SubscriptionPlanUpdate,
    MemberSubscriptionResponse, SubscriptionAssignRequest, CancelRequest
)
from app.services.subscription_service import SubscriptionService
from app.services.plan_service import PlanService  # assuming exists or will be wired
from app.core.exceptions import NotFoundError, ValidationError

router = APIRouter(prefix="/gyms/{gym_id}", tags=["Subscriptions"])


# ========== Plan Management ==========

@router.get("/plans", response_model=Response[List[SubscriptionPlanResponse]])
async def list_plans(
    gym_id: UUID,
    current_staff: Staff = Depends(require_gym_access),
    db: AsyncSession = Depends(get_db)
):
    """
    List all active subscription plans for a gym.
    """
    service = PlanService(db)
    plans = await service.list_plans(gym_id, active_only=True)
    return Response(data=[SubscriptionPlanResponse.model_validate(p) for p in plans])


@router.post("/plans", response_model=Response[SubscriptionPlanResponse])
async def create_plan(
    gym_id: UUID,
    data: SubscriptionPlanCreate,
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
        return Response(data=SubscriptionPlanResponse.model_validate(plan))
    except ValidationError as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": str(e), "error_code": e.error_code}
        )


@router.put("/plans/{plan_id}", response_model=Response[SubscriptionPlanResponse])
async def update_plan(
    gym_id: UUID,
    plan_id: UUID,
    data: SubscriptionPlanUpdate,
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
        return Response(data=SubscriptionPlanResponse.model_validate(plan))
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


@router.patch("/plans/{plan_id}/toggle", response_model=Response[SubscriptionPlanResponse])
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
        return Response(data=SubscriptionPlanResponse.model_validate(plan))
    except NotFoundError as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": str(e), "error_code": "NOT_FOUND"}
        )


# ========== Member Subscriptions ==========

@router.post("/members/{member_id}/subscriptions", response_model=Response[MemberSubscriptionResponse])
async def assign_plan_to_member(
    gym_id: UUID,
    member_id: UUID,
    data: SubscriptionAssignRequest,
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
        return Response(data=MemberSubscriptionResponse.model_validate(subscription))
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


@router.get("/members/{member_id}/subscriptions", response_model=Response[List[MemberSubscriptionResponse]])
async def get_member_subscription_history(
    gym_id: UUID,
    member_id: UUID,
    current_staff: Staff = Depends(require_gym_access),
    db: AsyncSession = Depends(get_db)
):
    """
    Get all subscriptions (history) for a member.
    """
    service = SubscriptionService(db)
    try:
        subscriptions = await service.get_member_subscription_history(member_id, gym_id)
        return Response(data=[MemberSubscriptionResponse.model_validate(s) for s in subscriptions])
    except NotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": str(e), "error_code": "NOT_FOUND"}
        )


@router.get("/members/{member_id}/subscriptions/active", response_model=Response[MemberSubscriptionResponse])
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
        return Response(data=MemberSubscriptionResponse.model_validate(subscription))
    except NotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": str(e), "error_code": "NOT_FOUND"}
        )


# ========== Subscription Actions ==========

@router.post("/subscriptions/{sub_id}/freeze", response_model=Response[MemberSubscriptionResponse])
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
        return Response(data=MemberSubscriptionResponse.model_validate(subscription))
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


@router.post("/subscriptions/{sub_id}/unfreeze", response_model=Response[MemberSubscriptionResponse])
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
        return Response(data=MemberSubscriptionResponse.model_validate(subscription))
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


@router.post("/subscriptions/{sub_id}/cancel", response_model=Response[MemberSubscriptionResponse])
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
        return Response(data=MemberSubscriptionResponse.model_validate(subscription))
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