import uuid
from typing import Optional
from sqlalchemy import select, func
from app.models.gym import Gym, BranchTaxSettings
from app.repositories.base import BaseRepository

class GymRepository(BaseRepository[Gym]):
    def __init__(self, session):
        super().__init__(Gym, session)

    async def count_active_branches(self, org_id: uuid.UUID) -> int:
        q = select(func.count()).where(self.model.org_id == org_id, self.model.is_active == True)
        result = await self.session.execute(q)
        return result.scalar_one()

    async def get_by_gymu_id(self, gymu_id: str) -> Optional[Gym]:
        q = select(self.model).where(self.model.gymu_id == gymu_id)
        result = await self.session.execute(q)
        return result.scalar_one_or_none()

class TaxRepository(BaseRepository[BranchTaxSettings]):
    def __init__(self, session):
        super().__init__(BranchTaxSettings, session)

    async def get_by_gym_id(self, gym_id: uuid.UUID) -> Optional[BranchTaxSettings]:
        q = select(self.model).where(self.model.gym_id == gym_id)
        result = await self.session.execute(q)
        return result.scalar_one_or_none()
