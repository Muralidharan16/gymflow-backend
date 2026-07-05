import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from typing import AsyncGenerator
import sys
import os
import uuid
from unittest.mock import patch
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app
from app.models.auth import Owner
from app.models.organization import Organization
from app.models.org_branch import OrgBranch
from app.schemas.branch_contacts import BranchContactORM, ContactKind
from app.core.database import AsyncSessionLocal
from app.core.redis import init_redis, get_redis_utils, close_redis
from conftest import cleanup_test_database_tables

@pytest_asyncio.fixture(autouse=True)
async def cleanup_database_and_redis():
    await close_redis()
    await init_redis()
    redis_utils = get_redis_utils()
    await redis_utils.client.flushdb()

    await cleanup_test_database_tables([
        "branch_contacts_audit",
        "branch_contacts",
        "org_branch_state",
        "org_branches",
        "owners",
        "organizations",
    ])

    yield

    await cleanup_test_database_tables([
        "branch_contacts_audit",
        "branch_contacts",
        "org_branch_state",
        "org_branches",
        "owners",
        "organizations",
    ])

@pytest_asyncio.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test"
    ) as ac:
        yield ac

@pytest.mark.asyncio
async def test_onboarding_creates_contacts_successfully(client):
    # 1. Sign up
    signup_payload = {
        "org_name": "Onboard Test Gym",
        "owner_name": "Test Owner",
        "email": "onboard_test@example.com",
        "password": "StrongPassword123!",
        "facility_type": "gym"
    }
    
    with patch("app.services.auth_service.send_verification_email", return_value=True) as mock_send:
        resp = await client.post("/auth/signup", json=signup_payload)
        assert resp.status_code == 200
        raw_token = mock_send.call_args[1]["raw_token"]
        
        # Verify (this creates Owner, Org, Gym)
        verify_resp = await client.get(f"/auth/verify?token={raw_token}", follow_redirects=False)
        assert verify_resp.status_code == 307

    # 2. Login to get session cookies
    login_payload = {
        "email": "onboard_test@example.com",
        "password": "StrongPassword123!"
    }
    login_resp = await client.post("/auth/login", json=login_payload)
    assert login_resp.status_code == 200
    
    # 3. Complete onboarding
    onboard_payload = {
        "phone": "9876543210",
        "address_line1": "123 Main Street",
        "address_line2": "Suite 4B",
        "city": "Chennai",
        "state": "Tamil Nadu",
        "pincode": "600001",
        "country": "India"
    }
    onboard_resp = await client.post("/onboarding/complete", json=onboard_payload)
    assert onboard_resp.status_code == 200
    
    # 4. Verify in DB that contacts are created
    async with AsyncSessionLocal() as session:
        stmt_owner = select(Owner).where(Owner.email == "onboard_test@example.com")
        res_owner = await session.execute(stmt_owner)
        owner = res_owner.scalar_one()
        
        stmt_contacts = select(BranchContactORM).where(BranchContactORM.org_id == owner.org_id)
        res_contacts = await session.execute(stmt_contacts)
        contacts = res_contacts.scalars().all()
        
        assert len(contacts) == 2
        
        phone_contact = next(c for c in contacts if c.contact_kind == ContactKind.PHONE)
        email_contact = next(c for c in contacts if c.contact_kind == ContactKind.EMAIL)
        
        assert phone_contact.phone_e164 == "+919876543210"
        assert phone_contact.is_primary is True
        assert phone_contact.visibility_scope == "public"
        
        assert email_contact.email_raw == "onboard_test@example.com"
        assert email_contact.is_primary is True
        assert email_contact.visibility_scope == "public"

    # 5. Call list branches endpoint and verify response structure & details
    branches_resp = await client.get("/branches")
    assert branches_resp.status_code == 200
    branches_data = branches_resp.json()["data"]
    assert len(branches_data) > 0
    
    principal_branch = branches_data[0]
    assert principal_branch["contact_email"] == "onboard_test@example.com"
    assert principal_branch["contact_phone"] in ["(987) 654-3210", "+919876543210", "098765 43210"]
