import uuid
from fastapi import HTTPException
from sqlalchemy import select
from app.models.gym import Gym, BranchTaxSettings
from app.models.organization import Organization
from app.models.enums import TIER_LIMITS
from app.repositories.gym_repo import GymRepository, TaxRepository
from app.schemas.gym import GymCreate, GymUpdate, TaxConfigCreate


class GymService:
    def __init__(self, gym_repo: GymRepository, tax_repo: TaxRepository, session):
        self.gym_repo = gym_repo
        self.tax_repo = tax_repo
        self.session = session

    async def create_branch(self, org_id: uuid.UUID, data: GymCreate) -> Gym:
        # --- Rule 2: Branch License Enforcement ---
        q = select(Organization).where(Organization.id == org_id)
        result = await self.session.execute(q)
        org = result.scalar_one()

        current_branches = await self.gym_repo.count_active_branches(org_id)
        limit = TIER_LIMITS[org.tier]["max_branches"]
        if current_branches >= limit:
            raise HTTPException(status_code=403, detail={
                "error": "BRANCH_LIMIT_EXCEEDED",
                "message": f"Your {org.tier} plan allows {limit} branch(es). Upgrade to add more."
            })

        # --- Generate sequential gymu_id (GYM001, GYM002...) ---
        last = await self.gym_repo.get_last_gymu_id(org_id)
        if last:
            num = int(last[3:]) + 1
        else:
            num = 1
        gymu_id = f"GYM{num:03d}"

        gym = Gym(
            org_id=org_id,
            name=data.name,
            gymu_id=gymu_id,
            address=data.address,
            city=data.city,
            phone=data.phone
        )
        return await self.gym_repo.create(gym)

    async def update_tax_config(self, gym_id: uuid.UUID, data: TaxConfigCreate) -> BranchTaxSettings:
        # --- Rule 1: PAN-GST Validation ---
        # Fetch org's pan via gym relation
        q = (
            select(Organization.pan_number)
            .join(Gym, Gym.org_id == Organization.id)
            .where(Gym.id == gym_id)
        )
        res = await self.session.execute(q)
        pan = res.scalar_one()

        if len(data.gst_number) != 15:
            raise HTTPException(400, "Invalid GSTIN format. Must be 15 characters.")

        extracted_pan = data.gst_number[2:12].upper()
        if extracted_pan != pan.upper():
            raise HTTPException(400, detail={
                "error": "PAN_GST_MISMATCH",
                "message": "GSTIN does not match organization PAN. Branch addition blocked."
            })

        tax_config = await self.tax_repo.get_by_gym_id(gym_id)
        if tax_config:
            tax_config.gst_number = data.gst_number
            tax_config.legal_name = data.legal_name
            tax_config.gst_rate = data.gst_rate
            tax_config.sac_code = data.sac_code
            tax_config.is_active = True
            return await self.tax_repo.update(tax_config)
        else:
            tax_config = BranchTaxSettings(
                gym_id=gym_id,
                gst_number=data.gst_number,
                legal_name=data.legal_name,
                gst_rate=data.gst_rate,
                sac_code=data.sac_code
            )
            return await self.tax_repo.create(tax_config)