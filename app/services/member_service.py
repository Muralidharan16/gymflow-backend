from datetime import datetime, date, timezone
from typing import List, Optional, Tuple
from uuid import UUID
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, ValidationError, MemberLimitExceeded
from app.models.member import Member, MemberStatus, MemberMeasurement
from app.repositories.member_repo import MemberRepository
from app.schemas.member import MemberCreate, MemberUpdate, MeasurementCreate
from app.utils.phone import normalize_phone
from app.utils.qr import generate_qr_token
from app.core.logging import logger


class MemberService:
    """Service for member management operations."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.member_repo = MemberRepository(session)

    async def list_members(
        self,
        gym_id: UUID,
        status: Optional[MemberStatus] = None,
        search_term: Optional[str] = None,
        page: int = 1,
        size: int = 10
    ) -> Tuple[List[Member], int]:
        """
        List members with pagination and filtering.
        
        Returns:
            Tuple of (list of members, total count)
        """
        return await self.member_repo.search(gym_id, status, search_term, page, size)

    async def get_member(self, member_id: UUID, gym_id: UUID) -> Member:
        """
        Get a member by ID, scoped to gym.
        
        Raises:
            NotFoundError: If member not found
        """
        member = await self.member_repo.get_by_id_active(member_id, gym_id)
        if not member:
            raise NotFoundError(f"Member {member_id} not found in gym {gym_id}")
        return member

    async def get_member_by_uid(self, member_uid: str) -> Member:
        """
        Get a member by UID (for QR check-in, no gym scope needed).
        
        Raises:
            NotFoundError: If member not found
        """
        member = await self.member_repo.get_by_uid_active(member_uid)
        if not member:
            raise NotFoundError(f"Member with UID {member_uid} not found")
        return member

    async def create_member(
        self,
        gym_id: UUID,
        data: MemberCreate,
        created_by: UUID
    ) -> Member:
        """
        Create a new member.
        
        Args:
            gym_id: Gym UUID
            data: Member creation data
            created_by: Staff UUID
            
        Returns:
            Created Member
            
        Raises:
            ValidationError: If phone is invalid or member already exists
            MemberLimitExceeded: If gym member limit reached
        """
        # Normalize phone number
        phone = normalize_phone(data.phone)
        if not phone:
            raise ValidationError("Invalid phone number", error_code="VALIDATION_ERROR")
        
        # Check for existing member with same phone in this gym
        existing = await self.member_repo.get_by_phone(phone, gym_id)
        if existing:
            raise ValidationError(
                f"Member with phone {phone} already exists in this gym",
                error_code="VALIDATION_ERROR"
            )
        
        # Check member limit (if any)
        # This can be implemented based on gym settings
        # For now, assume no limit or implement via settings
        
        # Generate unique QR token
        qr_token = await generate_qr_token()
        
        member = Member(
            gym_id=gym_id,
            member_uid=qr_token,  # QR token serves as member_uid
            name=data.name,
            phone=phone,
            email=data.email,
            date_of_birth=data.date_of_birth,
            gender=data.gender,
            blood_group=data.blood_group,
            address=data.address,
            notes=data.notes,
            status=MemberStatus.ACTIVE,
            is_active=True,
            qr_token=qr_token,
            created_by=created_by,
            updated_by=created_by
        )
        
        created = await self.member_repo.create(member)
        await self.session.commit()
        logger.info(f"Created member {created.id} in gym {gym_id}")
        return created

    async def update_member(
        self,
        gym_id: UUID,
        member_id: UUID,
        data: MemberUpdate,
        updated_by: UUID
    ) -> Member:
        """
        Update an existing member.
        
        Args:
            gym_id: Gym UUID
            member_id: Member UUID
            data: Update data (partial)
            updated_by: Staff UUID
            
        Returns:
            Updated Member
            
        Raises:
            NotFoundError: If member not found
            ValidationError: If phone is invalid or conflicts
        """
        member = await self.get_member(member_id, gym_id)
        
        # Update fields
        if data.name is not None:
            member.name = data.name
        
        if data.phone is not None:
            new_phone = normalize_phone(data.phone)
            if new_phone:
                # Check if another member already has this phone
                existing = await self.member_repo.get_by_phone(new_phone, gym_id)
                if existing and existing.id != member_id:
                    raise ValidationError(
                        f"Another member already has phone {new_phone}",
                        error_code="VALIDATION_ERROR"
                    )
                member.phone = new_phone
        
        if data.email is not None:
            member.email = data.email
        
        if data.address is not None:
            member.address = data.address
        
        if data.gender is not None:
            member.gender = data.gender
        
        if data.notes is not None:
            member.notes = data.notes
        
        if data.status is not None:
            member.status = data.status
        
        member.updated_by = updated_by
        
        updated = await self.member_repo.update(member)
        await self.session.commit()
        logger.info(f"Updated member {member_id}")
        return updated

    async def soft_delete(self, gym_id: UUID, member_id: UUID) -> None:
        """
        Soft delete a member (set is_active=False).
        
        Args:
            gym_id: Gym UUID
            member_id: Member UUID
            
        Raises:
            NotFoundError: If member not found
        """
        deleted = await self.member_repo.soft_delete(member_id, gym_id)
        if not deleted:
            raise NotFoundError(f"Member {member_id} not found in gym {gym_id}")
        await self.session.commit()
        logger.info(f"Soft deleted member {member_id}")

    async def get_member_qr_png(self, member_uid: str) -> bytes:
        """
        Get QR code PNG bytes for a member.
        
        Args:
            member_uid: Member's unique UID (QR token)
            
        Returns:
            PNG image bytes
            
        Raises:
            NotFoundError: If member not found
        """
        member = await self.get_member_by_uid(member_uid)
        # Generate QR code PNG from member's qr_token
        from app.utils.qr import generate_qr_png
        return await generate_qr_png(member.qr_token)

    # === Measurements ===

    async def log_measurement(
        self,
        member_id: UUID,
        gym_id: UUID,
        data: MeasurementCreate,
        created_by: UUID
    ) -> MemberMeasurement:
        """
        Log a new measurement for a member.
        
        Args:
            member_id: Member UUID
            gym_id: Gym UUID (for scoping)
            data: Measurement data
            created_by: Staff UUID
            
        Returns:
            Created MemberMeasurement
        """
        # Ensure member exists
        await self.get_member(member_id, gym_id)
        
        measurement = MemberMeasurement(
            member_id=member_id,
            gym_id=gym_id,
            measured_on=data.measured_on or date.today(),
            weight_kg=data.weight_kg,
            height_cm=data.height_cm,
            body_fat_percentage=data.body_fat_percentage,
            muscle_mass_kg=data.muscle_mass_kg,
            bmi=data.bmi,
            chest_cm=data.chest_cm,
            waist_cm=data.waist_cm,
            hips_cm=data.hips_cm,
            arms_cm=data.arms_cm,
            thighs_cm=data.thighs_cm,
            notes=data.notes,
            created_by=created_by
        )
        created = await self.member_repo.create_measurement(measurement)
        await self.session.commit()
        return created

    async def get_measurements(
        self,
        member_id: UUID,
        gym_id: UUID
    ) -> List[MemberMeasurement]:
        """
        Get measurement history for a member.
        """
        # Ensure member exists
        await self.get_member(member_id, gym_id)
        return await self.member_repo.get_measurements(member_id, gym_id)