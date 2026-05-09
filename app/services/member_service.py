import uuid
from fastapi import HTTPException
from sqlalchemy import select
from app.models.member import Member, MemberMeasurement
from app.models.gym import Gym
from app.models.organization import Organization
from app.models.enums import TIER_LIMITS, MemberStatus
from app.repositories.member_repo import MemberRepository, MeasurementRepository
from app.repositories.subscription_repo import SubscriptionRepository
from app.schemas.member import MemberCreate, MemberUpdate, MeasurementCreate


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

    async def create_member(self, org_id: uuid.UUID, gym_id: uuid.UUID, data: MemberCreate, staff_id: uuid.UUID) -> Member:
        # --- Rule 3: Member License Enforcement ---
        q = select(Organization).where(Organization.id == org_id)
        result = await self.session.execute(q)
        org = result.scalar_one()

        active_count = await self.sub_repo.count_active_in_org(org_id)
        limit = TIER_LIMITS[org.tier]["max_members"]
        if active_count >= limit:
            raise HTTPException(status_code=403, detail={
                "error": "MEMBER_LIMIT_EXCEEDED",
                "message": f"Your {org.tier} plan allows {limit} active members. Upgrade to add more."
            })

        # --- Generate sequential member_uid (GYM001-M0001) ---
        last_uid = await self.member_repo.get_last_member_uid(gym_id)
        if last_uid:
            # e.g., "GYM001-M0005" → num = 6
            num = int(last_uid.split("-M")[1]) + 1
        else:
            num = 1
        # Need gym's gymu_id to form member_uid
        gym = await self.session.get(Gym, gym_id)  # direct, no multi-tenant filter needed
        member_uid = f"{gym.gymu_id}-M{num:04d}"

        qr_token = str(uuid.uuid4())[:12]

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

    async def log_measurement(self, gym_id: uuid.UUID, member_id: uuid.UUID, data: MeasurementCreate, staff_id: uuid.UUID) -> MemberMeasurement:
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