import uuid
from typing import Optional, List
from sqlalchemy import select, func
from app.models.gym import Gym, BranchTaxSettings
from app.repositories.base import BaseRepository

class GymRepository(BaseRepository[Gym]):
    def __init__(self, session):
        super().__init__(Gym, session)

    async def count_active_branches(self, org_id: uuid.UUID) -> int:
        """Org-safe count of active branches."""
        q = select(func.count()).where(
            self.model.org_id == org_id, 
            self.model.is_active == True
        )
        result = await self.session.execute(q)
        return await result.scalar_one()

    async def get_by_gymu_id(self, gymu_id: str, org_id: uuid.UUID) -> Optional[Gym]:
        """Org-safe fetch by short Gym ID."""
        q = select(self.model).where(
            self.model.gymu_id == gymu_id,
            self.model.org_id == org_id
        )
        result = await self.session.execute(q)
        return await result.scalar_one_or_none()

    async def get_last_gymu_id(self, org_id: uuid.UUID) -> str | None:
        """Org-safe fetch of last generated gym ID for sequencing."""
        q = (
            select(self.model.gymu_id)
            .where(self.model.org_id == org_id)
            .order_by(self.model.created_at.desc())
            .limit(1)
        )
        result = await self.session.execute(q)
        return await result.scalar_one_or_none()

    async def list_by_org(self, org_id: uuid.UUID) -> List[Gym]:
        """Org-safe list of all gyms."""
        q = select(self.model).where(self.model.org_id == org_id)
        result = await self.session.execute(q)
        return list(result.scalars().all())

class TaxRepository(BaseRepository[BranchTaxSettings]):
    def __init__(self, session):
        super().__init__(BranchTaxSettings, session)

    async def get_by_gym_id(self, gym_id: uuid.UUID) -> Optional[BranchTaxSettings]:
        """Fetch tax settings for a gym. Gym ID is the PK, but we must ensure it exists."""
        q = select(self.model).where(self.model.gym_id == gym_id)
        result = await self.session.execute(q)
        return await result.scalar_one_or_none()