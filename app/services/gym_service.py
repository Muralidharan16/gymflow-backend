import logging
import uuid
from typing import List, Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, ValidationError
from app.models.gym import Gym, BranchTaxSettings
from app.repositories.gym_repo import GymRepository
from app.schemas.gym import GymCreate, GymUpdate, TaxConfigCreate
from app.models.organization import Organization
from app.models.enums import TIER_LIMITS
from sqlalchemy import select, func

logger = logging.getLogger(__name__)


class GymService:
    """Service for managing gym branches and tax configurations."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.gym_repo = GymRepository(session)

    # === Branch Management ===

    async def list_branches(self, org_id: UUID) -> List[Gym]:
        """List all active branches for an organization."""
        return await self.gym_repo.get_by_org_id(org_id, is_active=True)

    async def get_branch(self, gym_id: UUID, org_id: UUID) -> Gym:
        """
        Get a specific branch by ID, scoped to organization.
        
        Args:
            gym_id: Branch UUID
            org_id: Organization UUID
            
        Returns:
            Gym object
            
        Raises:
            NotFoundError: If branch not found or doesn't belong to org
        """
        gym = await self.gym_repo.get_by_id_and_org(gym_id, org_id)
        if not gym:
            raise NotFoundError(f"Branch {gym_id} not found in organization {org_id}", error_code="NOT_FOUND")
        return gym

    async def create_branch(self, org_id: UUID, data: GymCreate, created_by: UUID) -> Gym:
        """
        Create a new gym branch under an organization.
        
        Args:
            org_id: Organization UUID
            data: Gym creation data
            created_by: Staff UUID who created this branch
            
        Returns:
            Created Gym
            
        Raises:
            ValidationError: If branch limit exceeded
        """
        # Get organization tier
        org_result = await self.session.execute(select(Organization).where(Organization.id == org_id))
        org = org_result.scalar_one_or_none()
        if not org:
            raise NotFoundError(f"Organization {org_id} not found", error_code="ORG_NOT_FOUND")

        # Count active branches
        count_result = await self.session.execute(
            select(func.count()).select_from(Gym).where(Gym.org_id == org_id, Gym.is_active == True)
        )
        current_branches = count_result.scalar() or 0

        limit = TIER_LIMITS[org.tier]["max_branches"]
        if current_branches >= limit:
            raise ValidationError(
                f"Your {org.tier.value} plan allows {limit} branches. Upgrade to add more.",
                error_code="BRANCH_LIMIT_EXCEEDED"
            )
        
        # ENFORCEMENT: 2nd branch or more requires a registration number
        if current_branches >= 1:
            from app.models.organization import OrganizationRegistration
            reg_check_q = select(OrganizationRegistration).where(OrganizationRegistration.org_id == org_id)
            reg_check_res = await self.session.execute(reg_check_q)
            if not reg_check_res.scalar_one_or_none():
                raise ValidationError(
                    "A Business Registration Number (PAN, VAT, or local Business ID) is required to add more than one branch. Please update your profile.",
                    error_code="REGISTRATION_REQUIRED"
                )
        
        gym = Gym(
            org_id=org_id,
            name=data.name,
            gymu_id=data.internal_code or f"BR-{str(uuid.uuid4())[:6].upper()}",
            address=data.address,
            city=data.city,
            phone=data.phone,
            is_active=True
        )
        return await self.gym_repo.create(gym)

    async def update_branch(
        self,
        gym_id: UUID,
        org_id: UUID,
        data: GymUpdate,
        updated_by: UUID
    ) -> Gym:
        """
        Update an existing branch.
        
        Args:
            gym_id: Branch UUID
            org_id: Organization UUID (for scoping)
            data: Update data (partial)
            updated_by: Staff UUID performing update
            
        Returns:
            Updated Gym
            
        Raises:
            NotFoundError: If branch not found
        """
        gym = await self.get_branch(gym_id, org_id)
        
        # Update only provided fields
        if data.name is not None:
            gym.name = data.name
        if data.address is not None:
            gym.address = data.address
        if data.city is not None:
            gym.city = data.city
        if data.phone is not None:
            gym.phone = data.phone
        
        return await self.gym_repo.update(gym)

    async def delete_branch(self, gym_id: UUID, org_id: UUID) -> None:
        """
        Soft delete a branch (set is_active=False).
        
        Args:
            gym_id: Branch UUID
            org_id: Organization UUID (for scoping)
            
        Raises:
            NotFoundError: If branch not found or already inactive
        """
        deleted = await self.gym_repo.soft_delete(gym_id, org_id)
        if not deleted:
            raise NotFoundError(f"Branch {gym_id} not found or already deleted", error_code="NOT_FOUND")

    # === Tax Configuration ===

    async def get_tax_config(self, gym_id: UUID) -> Optional[BranchTaxSettings]:
        """Get active tax configuration for a gym."""
        return await self.gym_repo.get_tax_config(gym_id)

    async def create_or_update_tax_config(
        self,
        gym_id: UUID,
        data: TaxConfigCreate,
        created_by: UUID
    ) -> BranchTaxSettings:
        """
        Create or update tax configuration for a gym.
        Deactivates previous active config and creates a new one.
        
        Args:
            gym_id: Branch UUID
            data: Tax configuration data
            created_by: Staff UUID
            
        Returns:
            Created/updated BranchTaxSettings
        """
        # Deactivate current active config
        await self.gym_repo.deactivate_tax_config(gym_id)
        
        # Encrypt and Mask
        from app.utils.encryption import encrypt_data, mask_id_number
        encrypted_id = encrypt_data(data.tax_id)
        masked_id = mask_id_number(data.tax_id)

        # Create new config
        config = BranchTaxSettings(
            gym_id=gym_id,
            tax_type=data.tax_type,
            tax_id_encrypted=encrypted_id,
            tax_id_masked=masked_id,
            legal_name=data.legal_name,
            gst_rate=data.gst_rate,
            sac_code=data.sac_code,
            filing_frequency=data.filing_frequency,
            is_active=True
        )
        return await self.gym_repo.create_tax_config(config)

    async def delete_tax_config(self, gym_id: UUID) -> None:
        """
        Deactivate the active tax configuration for a gym.
        
        Args:
            gym_id: Branch UUID
            
        Raises:
            NotFoundError: If no active config exists
        """
        deactivated = await self.gym_repo.deactivate_tax_config(gym_id)
        if not deactivated:
            raise NotFoundError(f"No active tax configuration found for gym {gym_id}", error_code="NOT_FOUND")

    async def get_tax_config_history(self, gym_id: UUID) -> List[BranchTaxSettings]:
        """Get all tax configurations (history) for a gym."""
        return await self.gym_repo.get_all_tax_configs(gym_id)