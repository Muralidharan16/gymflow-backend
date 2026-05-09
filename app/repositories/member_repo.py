import uuid
from typing import Optional
from sqlalchemy import select, or_, func
from app.models.member import Member, MemberMeasurement
from app.repositories.base import BaseRepository

class MemberRepository(BaseRepository[Member]):
    def __init__(self, session):
        super().__init__(Member, session)

    async def get_by_uid(self, member_uid: str) -> Optional[Member]:
        q = select(self.model).where(self.model.member_uid == member_uid)
        result = await self.session.execute(q)
        return result.scalar_one_or_none()

    async def get_by_qr(self, qr_token: str) -> Optional[Member]:
        q = select(self.model).where(self.model.qr_token == qr_token)
        result = await self.session.execute(q)
        return result.scalar_one_or_none()

    async def get_by_fingerprint(self, fingerprint_id: str) -> Optional[Member]:
        q = select(self.model).where(self.model.fingerprint_id == fingerprint_id)
        result = await self.session.execute(q)
        return result.scalar_one_or_none()

    async def get_by_any_uid(self, uid: str) -> Optional[Member]:
        q = select(self.model).where(
            or_(
                self.model.member_uid == uid,
                self.model.qr_token == uid,
                self.model.fingerprint_id == uid
            )
        )
        result = await self.session.execute(q)
        return result.scalar_one_or_none()

    async def count_active_in_org(self, org_id: uuid.UUID) -> int:
        q = select(func.count()).where(self.model.org_id == org_id, self.model.is_active == True)
        result = await self.session.execute(q)
        return result.scalar_one()

class MeasurementRepository(BaseRepository[MemberMeasurement]):
    def __init__(self, session):
        super().__init__(MemberMeasurement, session)
