import uuid
import logging
from fastapi import HTTPException, status
from sqlalchemy import select
from app.models.member import Member, MemberMeasurement
from app.models.gym import Gym
from app.models.organization import Organization
from app.models.enums import TIER_LIMITS, MemberStatus
from app.repositories.member_repo import MemberRepository, MeasurementRepository
from app.repositories.subscription_repo import SubscriptionRepository
from app.schemas.member import MemberCreate, MeasurementCreate
from app.utils.lock import RedisLock

logger = logging.getLogger(__name__)

class MemberService:
    def __init__(
        self,
        member_repo: MemberRepository,
        sub_repo: SubscriptionRepository,
        measurement_repo: MeasurementRepository,
        session
    ):
        self.member_repo = member_repo
        self.sub_repo = sub_repo
        self.measurement_repo = measurement_repo
        self.session = session

    async def create_member(
        self, 
        org_id: uuid.UUID, 
        gym_id: uuid.UUID, 
        data: MemberCreate, 
        staff_id: uuid.UUID
    ) -> Member:
        """
        Create a new member with license check and sequential UID generation.
        Atomic operation.
        """
        async with RedisLock(f"member_uid_gen:{str(gym_id)}"):
            async with self.session.begin_nested():
                # 1. License Enforcement
                q = select(Organization).where(Organization.id == org_id)
                result = await self.session.execute(q)
                org = result.scalar_one()

                active_count = await self.sub_repo.count_active_in_org(org_id)
                limit = TIER_LIMITS[org.tier]["max_members"]
                if active_count >= limit:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN, 
                        detail={
                            "error": "MEMBER_LIMIT_EXCEEDED",
                            "message": f"Your {org.tier} plan allows {limit} active members. Upgrade to add more."
                        }
                    )

                # 2. Sequential UID generation
                last_uid = await self.member_repo.get_last_member_uid(gym_id)
                if last_uid:
                    # e.g., "GYM001-M0005" → num = 6
                    try:
                        num = int(last_uid.split("-M")[-1]) + 1
                    except (IndexError, ValueError):
                        num = 1
                else:
                    num = 1
                
                # Fetch gym for its gymu_id prefix
                gym = await self.session.get(Gym, gym_id)
                if not gym or gym.org_id != org_id:
                    raise HTTPException(status_code=404, detail="Gym not found or access denied")
                    
                member_uid = f"{gym.gymu_id}-M{num:04d}"
                qr_token = str(uuid.uuid4())[:12]

                # 3. Create member
                member = Member(
                    org_id=org_id,
                    gym_id=gym_id,
                    member_uid=member_uid,
                    qr_token=qr_token,
                    name=data.name,
                    phone=data.phone,
                    email=data.email,
                    status=MemberStatus.active,
                    created_by=staff_id
                )
                return await self.member_repo.create(member)

    async def log_measurement(
        self, 
        gym_id: uuid.UUID, 
        member_id: uuid.UUID, 
        data: MeasurementCreate, 
        staff_id: uuid.UUID
    ) -> MemberMeasurement:
        """Log a member measurement record."""
        # Verify member exists and belongs to gym
        member = await self.member_repo.get_by_id(member_id, gym_id)
        if not member:
            raise HTTPException(status_code=404, detail="Member not found")

        measurement = MemberMeasurement(
            gym_id=gym_id,
            member_id=member_id,
            measured_on=data.measured_on,
            weight_kg=data.weight_kg,
            height_cm=data.height_cm,
            body_fat_pct=data.body_fat_pct,
            notes=data.notes,
            recorded_by=staff_id
        )
        return await self.measurement_repo.create(measurement)