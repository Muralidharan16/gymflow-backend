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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app
from app.models.auth import Owner
from app.models.organization import Organization
from app.models.org_branch import OrgBranch
from app.schemas.branch_contacts import BranchContactORM, BranchContactAuditORM
from app.core.database import AsyncSessionLocal
from app.core.redis import init_redis, get_redis_utils, close_redis

@pytest_asyncio.fixture(autouse=True)
async def cleanup_database_and_redis():
    await close_redis()
    await init_redis()
    redis_utils = get_redis_utils()
    await redis_utils.client.flushdb()

    async with AsyncSessionLocal() as session:
        from sqlalchemy import text
        await session.execute(text("RESET ROLE"))
        await session.execute(text("SET session_replication_role = 'replica'"))
        
        test_emails = ["branch_contacts_api@example.com"]
        
        stmt = select(Owner.org_id).where(Owner.email.in_(test_emails))
        res = await session.execute(stmt)
        org_ids = res.scalars().all()
        
        if org_ids:
            await session.execute(delete(BranchContactAuditORM).where(BranchContactAuditORM.org_id.in_(org_ids)))
            await session.execute(delete(BranchContactORM).where(BranchContactORM.org_id.in_(org_ids)))
            await session.execute(delete(OrgBranch).where(OrgBranch.org_id.in_(org_ids)))
            await session.execute(delete(Owner).where(Owner.org_id.in_(org_ids)))
            await session.execute(delete(Organization).where(Organization.id.in_(org_ids)))
            
        await session.execute(text("SET session_replication_role = 'origin'"))
        await session.commit()

    yield

    async with AsyncSessionLocal() as session:
        from sqlalchemy import text
        await session.execute(text("RESET ROLE"))
        await session.execute(text("SET session_replication_role = 'replica'"))
        
        test_emails = ["branch_contacts_api@example.com"]
        stmt = select(Owner.org_id).where(Owner.email.in_(test_emails))
        res = await session.execute(stmt)
        org_ids = res.scalars().all()
        if org_ids:
            await session.execute(delete(BranchContactAuditORM).where(BranchContactAuditORM.org_id.in_(org_ids)))
            await session.execute(delete(BranchContactORM).where(BranchContactORM.org_id.in_(org_ids)))
            await session.execute(delete(OrgBranch).where(OrgBranch.org_id.in_(org_ids)))
            await session.execute(delete(Owner).where(Owner.org_id.in_(org_ids)))
            await session.execute(delete(Organization).where(Organization.id.in_(org_ids)))
        await session.execute(text("SET session_replication_role = 'origin'"))
        await session.commit()

@pytest_asyncio.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test"
    ) as ac:
        yield ac

@pytest_asyncio.fixture
async def auth_session(client):
    # Register
    signup_payload = {
        "org_name": "API Test Gym",
        "owner_name": "API Tester",
        "email": "branch_contacts_api@example.com",
        "password": "StrongPassword123!",
        "facility_type": "gym"
    }
    
    with patch("app.services.auth_service.send_verification_email", return_value=True) as mock_send:
        resp = await client.post("/auth/signup", json=signup_payload)
        raw_token = mock_send.call_args[1]["raw_token"]
        await client.get(f"/auth/verify?token={raw_token}", follow_redirects=False)

    # Login
    login_payload = {
        "email": "branch_contacts_api@example.com",
        "password": "StrongPassword123!"
    }
    await client.post("/auth/login", json=login_payload)
    
    # Complete Onboarding to create a branch
    onboard_payload = {
        "phone": "9876543210",
        "address_line1": "123 Main Street",
        "city": "Chennai",
        "state": "Tamil Nadu",
        "pincode": "600001",
        "country": "India"
    }
    await client.post("/onboarding/complete", json=onboard_payload)
    
    # Get branch_id
    branches_resp = await client.get("/branches")
    branch_id = branches_resp.json()["data"][0]["id"]
    
    return {"client": client, "branch_id": branch_id}

@pytest.mark.asyncio
async def test_api_create_phone_contact(auth_session):
    client = auth_session["client"]
    branch_id = auth_session["branch_id"]
    
    payload = {
        "contact_kind": "phone",
        "phone_number": "9876543210",
        "contact_label": "Test Phone",
        "visibility_scope": "public",
        "channel_capabilities": {"whatsapp": True}
    }
    
    resp = await client.post(f"/branches/{branch_id}/contacts", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["contact_kind"] == "phone"
    assert data["phone_e164"] == "+919876543210"
    assert data["country_code"] == "IN"
    assert data["email_raw"] is None

@pytest.mark.asyncio
async def test_api_create_email_contact(auth_session):
    client = auth_session["client"]
    branch_id = auth_session["branch_id"]
    
    payload = {
        "contact_kind": "email",
        "email_address": "Test@example.com",
        "contact_label": "Test Email",
        "visibility_scope": "internal",
        "channel_capabilities": {}
    }
    
    resp = await client.post(f"/branches/{branch_id}/contacts", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["contact_kind"] == "email"
    assert data["email_normalized"] == "test@example.com"
    assert data["phone_e164"] is None

@pytest.mark.asyncio
async def test_api_list_contacts(auth_session):
    client = auth_session["client"]
    branch_id = auth_session["branch_id"]
    
    resp = await client.get(f"/branches/{branch_id}/contacts")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 2 # Principal branch creates 2 initially
    
    # Check they all belong to this branch
    for c in data:
        assert c["branch_id"] == branch_id

@pytest.mark.asyncio
async def test_api_get_contact(auth_session):
    client = auth_session["client"]
    branch_id = auth_session["branch_id"]
    
    # List to get an ID
    resp = await client.get(f"/branches/{branch_id}/contacts")
    contact_id = resp.json()[0]["id"]
    
    resp2 = await client.get(f"/branches/{branch_id}/contacts/{contact_id}")
    assert resp2.status_code == 200
    assert resp2.json()["id"] == contact_id

@pytest.mark.asyncio
async def test_api_soft_delete_contact(auth_session):
    client = auth_session["client"]
    branch_id = auth_session["branch_id"]
    
    # Create one to delete
    payload = {
        "contact_kind": "phone",
        "phone_number": "9999999999",
        "contact_label": "To Delete"
    }
    create_resp = await client.post(f"/branches/{branch_id}/contacts", json=payload)
    print(create_resp.json())
    assert create_resp.status_code == 201
    contact_id = create_resp.json()["id"]
    
    # Delete it
    del_resp = await client.delete(f"/branches/{branch_id}/contacts/{contact_id}")
    assert del_resp.status_code == 204
    
    # Verify it doesn't show in list
    list_resp = await client.get(f"/branches/{branch_id}/contacts")
    ids = [c["id"] for c in list_resp.json()]
    assert contact_id not in ids
    
    # Verify it's 404 on GET
    get_resp = await client.get(f"/branches/{branch_id}/contacts/{contact_id}")
    assert get_resp.status_code == 404

@pytest.mark.asyncio
async def test_api_promote_contact(auth_session):
    client = auth_session["client"]
    branch_id = auth_session["branch_id"]
    
    # Create a secondary email contact
    payload = {
        "contact_kind": "email",
        "email_address": "secondary@example.com",
        "contact_label": "Secondary Email"
    }
    create_resp = await client.post(f"/branches/{branch_id}/contacts", json=payload)
    print(create_resp.json())
    assert create_resp.status_code == 201
    contact_id = create_resp.json()["id"]
    assert create_resp.json()["is_primary"] is False
    
    # Promote it
    prom_resp = await client.post(f"/branches/{branch_id}/contacts/{contact_id}/promote", json={"contact_kind": "email"})
    assert prom_resp.status_code == 200
    assert prom_resp.json()["is_primary"] is True
    
    # Verify list only has 1 primary email
    list_resp = await client.get(f"/branches/{branch_id}/contacts")
    emails = [c for c in list_resp.json() if c["contact_kind"] == "email"]
    primary_emails = [c for c in emails if c["is_primary"]]
    assert len(primary_emails) == 1
    assert primary_emails[0]["id"] == contact_id

@pytest.mark.asyncio
async def test_api_audit_trail(auth_session):
    client = auth_session["client"]
    branch_id = auth_session["branch_id"]
    
    payload = {
        "contact_kind": "phone",
        "phone_number": "8888888888",
        "contact_label": "Audit Test"
    }
    create_resp = await client.post(f"/branches/{branch_id}/contacts", json=payload)
    print(create_resp.json())
    assert create_resp.status_code == 201
    contact_id = create_resp.json()["id"]
    
    audit_resp = await client.get(f"/branches/{branch_id}/contacts/{contact_id}/audit")
    assert audit_resp.status_code == 200
    audits = audit_resp.json()
    assert len(audits) >= 1
    assert audits[0]["action"] == "INSERT"
