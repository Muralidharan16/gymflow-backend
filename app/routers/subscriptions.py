import uuid
from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List
from app.core.database import get_db
from app.schemas.subscription import PlanCreate, PlanResponse, SubscriptionCreate, SubscriptionResponse, FreezeRequest
from app.schemas.common import Response
from app.services.subscription_service import SubscriptionService
from app.repositories.subscription_repo import PlanRepository, SubscriptionRepository, FreezeLogRepository
from app.repositories.member_repo import MemberRepository

router = APIRouter(tags=["Subscriptions"])

@router.post("/gyms/{gym_id}/plans", response_model=Response[PlanResponse])
async def create_plan(gym_id: uuid.UUID, data: PlanCreate, db: AsyncSession = Depends(get_db)):
    repo = PlanRepository(db)
    from app.models.subscription import SubscriptionPlan
    plan = SubscriptionPlan(gym_id=gym_id, **data.model_dump())
    new_plan = await repo.create(plan)
    return Response(data=new_plan)

@router.post("/gyms/{gym_id}/members/{member_id}/subscriptions", response_model=Response[SubscriptionResponse])
async def assign_plan(gym_id: uuid.UUID, member_id: uuid.UUID, data: SubscriptionCreate, request: Request, db: AsyncSession = Depends(get_db)):
    service = SubscriptionService(SubscriptionRepository(db), PlanRepository(db), FreezeLogRepository(db), MemberRepository(db))
    sub = await service.assign_plan(gym_id, member_id, data.plan_id, data.start_date, request.state.staff_id)
    return Response(data=sub, message="Plan assigned")

@router.post("/subscriptions/{sub_id}/freeze")
async def freeze_subscription(sub_id: uuid.UUID, data: FreezeRequest, request: Request, db: AsyncSession = Depends(get_db)):
    service = SubscriptionService(SubscriptionRepository(db), PlanRepository(db), FreezeLogRepository(db), MemberRepository(db))
    # Note: request.state.gym_id might be needed if not present in path
    gym_id = request.state.gym_id or sub_id # Fallback/Mock
    await service.freeze_subscription(gym_id, sub_id, data.days, data.reason, request.state.staff_id)
    return Response(message="Subscription frozen")
