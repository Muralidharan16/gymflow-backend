import uuid
import logging
from typing import List, Optional
from decimal import Decimal
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException, status

from app.models.subscription import SubscriptionPlan
from app.repositories.subscription_repo import SubscriptionRepository
from app.schemas.subscription import PlanCreate, PlanUpdate
from app.core.exceptions import NotFoundError, ValidationError

logger = logging.getLogger(__name__)

class PlanService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.sub_repo = SubscriptionRepository(session)

    async def list_plans(self, gym_id: uuid.UUID, active_only: bool = True) -> List[SubscriptionPlan]:
        return await self.sub_repo.get_all_plans_for_gym(gym_id, active_only)

    async def create_plan(self, gym_id: uuid.UUID, data: PlanCreate, staff_id: uuid.UUID) -> SubscriptionPlan:
        plan = SubscriptionPlan(
            gym_id=gym_id,
            name=data.name,
            description=data.description,
            duration_days=data.duration_days,
            price=data.price,
            max_freeze_days=data.max_freeze_days,
            features=data.features or {},
            is_active=True,
            created_by=staff_id,
            updated_by=staff_id
        )
        return await self.sub_repo.create_plan(plan)

    async def update_plan(
        self, gym_id: uuid.UUID, plan_id: uuid.UUID, data: PlanUpdate, staff_id: uuid.UUID
    ) -> SubscriptionPlan:
        plan = await self.sub_repo.get_plan_by_id_and_gym(plan_id, gym_id)
        if not plan:
            raise NotFoundError("Plan not found")

        if data.name is not None:
            plan.name = data.name
        if data.description is not None:
            plan.description = data.description
        if data.duration_days is not None:
            plan.duration_days = data.duration_days
        if data.price is not None:
            plan.price = data.price
        if data.max_freeze_days is not None:
            plan.max_freeze_days = data.max_freeze_days
        if data.features is not None:
            plan.features = data.features
            
        plan.updated_by = staff_id
        return await self.sub_repo.update_plan(plan)

    async def toggle_plan_active(
        self, gym_id: uuid.UUID, plan_id: uuid.UUID, staff_id: uuid.UUID
    ) -> SubscriptionPlan:
        plan = await self.sub_repo.get_plan_by_id_and_gym(plan_id, gym_id)
        if not plan:
            raise NotFoundError("Plan not found")

        plan.is_active = not plan.is_active
        plan.updated_by = staff_id
        return await self.sub_repo.update_plan(plan)
