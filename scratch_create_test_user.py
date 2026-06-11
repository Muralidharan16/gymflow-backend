import asyncio
import sys
import os
import uuid
from datetime import datetime, timezone
from sqlalchemy import select, delete, text
from app.core.database import async_session_maker
from app.models.auth import Owner
from app.models.organization import Organization
from app.models.org_branch import OrgBranch
from app.models.branch_operating_hours import BranchOperatingHours, BranchSpecialHours, BranchHoursProjection, BranchHoursAuditLog
from app.core.security import hash_password

def require_destructive_reset_enabled():
    if os.getenv("ALLOW_DESTRUCTIVE_DB_RESET") != "true":
        raise SystemExit("Refusing destructive test-user reset. Set ALLOW_DESTRUCTIVE_DB_RESET=true to continue.")

async def setup_test_user():
    test_email = "phase5@example.com"
    test_password = "StrongPassword123!"

    async with async_session_maker() as session:
        # 1. Cleanup existing user
        stmt = select(Owner.org_id).where(Owner.email == test_email)
        res = await session.execute(stmt)
        org_id = res.scalar_one_or_none()

        if org_id:
            print(f"Cleaning up existing user and org {org_id}...")
            # We must use replication role trick to bypass foreign key checks or just delete in order.
            await session.execute(text("SET session_replication_role = 'replica'"))
            
            # Delete hours
            await session.execute(delete(BranchHoursAuditLog).where(BranchHoursAuditLog.branch_id.in_(
                select(OrgBranch.id).where(OrgBranch.org_id == org_id)
            )))
            await session.execute(delete(BranchHoursProjection).where(BranchHoursProjection.branch_id.in_(
                select(OrgBranch.id).where(OrgBranch.org_id == org_id)
            )))
            await session.execute(delete(BranchOperatingHours).where(BranchOperatingHours.branch_id.in_(
                select(OrgBranch.id).where(OrgBranch.org_id == org_id)
            )))
            await session.execute(delete(BranchSpecialHours).where(BranchSpecialHours.branch_id.in_(
                select(OrgBranch.id).where(OrgBranch.org_id == org_id)
            )))
            
            await session.execute(delete(OrgBranch).where(OrgBranch.org_id == org_id))
            await session.execute(delete(Owner).where(Owner.org_id == org_id))
            await session.execute(delete(Organization).where(Organization.id == org_id))
            
            await session.execute(text("SET session_replication_role = 'origin'"))
            await session.commit()

        print("Creating new organization...")
        new_org_id = uuid.uuid4()
        org = Organization(
            id=new_org_id,
            name="Phase 5 Gym",
            slug="phase-5-gym",
            business_type="gym"
        )
        session.add(org)
        await session.flush()

        print("Creating new owner...")
        hashed_pw = hash_password(test_password)
        owner = Owner(
            id=uuid.uuid4(),
            org_id=new_org_id,
            owner_name="Tester",
            email=test_email,
            hashed_password=hashed_pw,
            email_verified=True,
            email_verified_at=datetime.now(timezone.utc),
            onboarding_completed=True,
            onboarding_completed_at=datetime.now(timezone.utc)
        )
        session.add(owner)
        await session.flush()

        print("Creating main branch...")
        branch = OrgBranch(
            id=uuid.uuid4(),
            org_id=new_org_id,
            branch_name="Main Branch",
            branch_code="MAIN",
            internal_slug="phase-5-gym-main",
            country_code="IN"
        )
        session.add(branch)
        
        await session.commit()
        print("Test user created successfully!")
        print(f"Email: {test_email}")
        print(f"Password: {test_password}")

if __name__ == "__main__":
    require_destructive_reset_enabled()
    asyncio.run(setup_test_user())
