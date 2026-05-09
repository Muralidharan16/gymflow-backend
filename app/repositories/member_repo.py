import uuid
from typing import Optional, List
from sqlalchemy import select, or_, func
from app.models.member import Member, MemberMeasurement
from app.repositories.base import BaseRepository

class MemberRepository(BaseRepository[Member]):
    def __init__(self, session):
        super().__init__(Member, session)

    async def get_by_uid(self, member_uid: str, gym_id: uuid.UUID) -> Optional[Member]:
        """Tenant-safe fetch by member_uid."""
        q = select(self.model).where(
            self.model.member_uid == member_uid,
            self.model.gym_id == gym_id
        )
        result = await self.session.execute(q)
        return await result.scalar_one_or_none()

    async def get_by_qr(self, qr_token: str, gym_id: uuid.UUID) -> Optional[Member]:
        """Tenant-safe fetch by QR token."""
        q = select(self.model).where(
            self.model.qr_token == qr_token,
            self.model.gym_id == gym_id
        )
        result = await self.session.execute(q)
        return await result.scalar_one_or_none()

    async def get_by_fingerprint(self, fingerprint_id: str, gym_id: uuid.UUID) -> Optional[Member]:
        """Tenant-safe fetch by fingerprint ID."""
        q = select(self.model).where(
            self.model.fingerprint_id == fingerprint_id,
            self.model.gym_id == gym_id
        )
        result = await self.session.execute(q)
        return await result.scalar_one_or_none()

    async def get_by_any_uid(self, uid: str, gym_id: uuid.UUID) -> Optional[Member]:
        """Tenant-safe fetch by any unique identifier (UID, QR, or Fingerprint)."""
        q = select(self.model).where(
            self.model.gym_id == gym_id,
            or_(
                self.model.member_uid == uid,
                self.model.qr_token == uid,
                self.model.fingerprint_id == uid
            )
        )
        result = await self.session.execute(q)
        return await result.scalar_one_or_none()

    async def count_active_in_org(self, org_id: uuid.UUID) -> int:
        """Org-safe count of active members."""
        q = select(func.count()).where(
            self.model.org_id == org_id, 
            self.model.is_active == True
        )
        result = await self.session.execute(q)
        return await result.scalar_one()

    async def get_last_member_uid(self, gym_id: uuid.UUID) -> str | None:
        """Fetch the most recent member UID for a gym to generate the next one."""
        q = (
            select(self.model.member_uid)
            .where(self.model.gym_id == gym_id)
            .order_by(self.model.created_at.desc())
            .limit(1)
        )
        result = await self.session.execute(q)
        return await result.scalar_one_or_none()

class MeasurementRepository(BaseRepository[MemberMeasurement]):
    def __init__(self, session):
        super().__init__(MemberMeasurement, session)

    async def list_by_member(
        self, 
        member_id: uuid.UUID, 
        gym_id: uuid.UUID, 
        limit: int = 50
    ) -> List[MemberMeasurement]:
        """Tenant-safe list of measurements for a specific member."""
        q = (
            select(self.model)
            .where(
                self.model.member_id == member_id,
                self.model.gym_id == gym_id
            )
            .order_by(self.model.measured_on.desc())
            .limit(limit)
        )
        result = await self.session.execute(q)
        return list(result.scalars().all())