from typing import List, Optional
from uuid import UUID

from sqlalchemy import select, update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.gym import Gym, BranchTaxSettings
from app.repositories.base_repo import BaseRepository


class GymRepository(BaseRepository[Gym]):
    """Repository for Gym (branch) operations."""

    def __init__(self, session: AsyncSession):
        super().__init__(Gym, session)

    async def get_by_org_id(self, org_id: UUID, is_active: bool = True) -> List[Gym]:
        """
        Get all gyms (branches) for an organization.
        
        Args:
            org_id: Organization UUID
            is_active: Filter by active status
            
        Returns:
            List of Gym objects
        """
        query = select(Gym).where(
            Gym.org_id == org_id,
            Gym.is_active == is_active
        ).order_by(Gym.name)
        result = await self.session.execute(query)
        return result.scalars().all()

    async def get_by_id_and_org(self, gym_id: UUID, org_id: UUID) -> Optional[Gym]:
        """
        Get a specific gym by ID and organization ID (for scoping).
        
        Args:
            gym_id: Gym UUID
            org_id: Organization UUID
            
        Returns:
            Gym if found and belongs to org, else None
        """
        query = select(Gym).where(
            Gym.id == gym_id,
            Gym.org_id == org_id,
            Gym.is_active == True
        )
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def get_by_id(self, gym_id: UUID) -> Optional[Gym]:
        """Get gym by ID (without org scope)."""
        return await self.session.get(Gym, gym_id)

    async def create(self, gym: Gym) -> Gym:
        """Create a new gym."""
        self.session.add(gym)
        await self.session.flush()
        return gym

    async def update(self, gym: Gym) -> Gym:
        """
        Update an existing gym.
        
        Args:
            gym: Gym object with updated attributes
            
        Returns:
            Updated Gym
        """
        await self.session.merge(gym)
        await self.session.flush()
        return gym

    async def soft_delete(self, gym_id: UUID, org_id: UUID) -> bool:
        """
        Soft delete a gym (set is_active=False).
        
        Args:
            gym_id: Gym UUID
            org_id: Organization UUID (for validation)
            
        Returns:
            True if deleted, False if not found
        """
        query = sql_update(Gym).where(
            Gym.id == gym_id,
            Gym.org_id == org_id,
            Gym.is_active == True
        ).values(is_active=False)
        result = await self.session.execute(query)
        await self.session.flush()
        return result.rowcount > 0

    # Tax configuration methods

    async def get_tax_config(self, gym_id: UUID) -> Optional[BranchTaxSettings]:
        """Get active tax configuration for a gym."""
        query = select(BranchTaxSettings).where(
            BranchTaxSettings.gym_id == gym_id,
            BranchTaxSettings.is_active == True
        )
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def get_tax_config_by_id(self, config_id: UUID) -> Optional[BranchTaxSettings]:
        """Get tax config by ID."""
        return await self.session.get(BranchTaxSettings, config_id)

    async def create_tax_config(self, config: BranchTaxSettings) -> BranchTaxSettings:
        """Create a new tax configuration."""
        self.session.add(config)
        await self.session.flush()
        return config

    async def update_tax_config(self, config: BranchTaxSettings) -> BranchTaxSettings:
        """Update an existing tax configuration."""
        await self.session.merge(config)
        await self.session.flush()
        return config

    async def deactivate_tax_config(self, gym_id: UUID) -> bool:
        """
        Deactivate the current active tax config for a gym.
        
        Args:
            gym_id: Gym UUID
            
        Returns:
            True if deactivated, False if no active config found
        """
        # First get the active config
        active = await self.get_tax_config(gym_id)
        if not active:
            return False
        
        active.is_active = False
        await self.session.merge(active)
        await self.session.flush()
        return True

    async def get_all_tax_configs(self, gym_id: UUID) -> List[BranchTaxSettings]:
        """Get all tax configurations for a gym (history)."""
        query = select(BranchTaxSettings).where(
            BranchTaxSettings.gym_id == gym_id
        ).order_by(BranchTaxSettings.created_at.desc())
        result = await self.session.execute(query)
        return result.scalars().all()