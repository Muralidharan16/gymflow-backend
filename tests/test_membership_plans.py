import pytest
import pytest_asyncio
import uuid
from httpx import AsyncClient, ASGITransport
from datetime import datetime, timezone, timedelta
from sqlalchemy import text
from decimal import Decimal

from app.main import app
from app.core.database import AsyncSessionLocal
from app.core.security import create_access_token
from app.models.organization import Organization
from app.models.auth import Owner
from app.models.org_branch import OrgBranch

@pytest_asyncio.fixture(autouse=True)
async def cleanup_database():
    yield
    async with AsyncSessionLocal() as session:
        await session.execute(text("RESET ROLE"))
        await session.execute(text("SET session_replication_role = 'replica'"))
        await session.execute(text("DELETE FROM membership_plans;"))
        await session.execute(text("DELETE FROM org_branch_state;"))
        await session.execute(text("DELETE FROM org_branches;"))
        await session.execute(text("DELETE FROM owners;"))
        await session.execute(text("DELETE FROM organizations;"))
        await session.execute(text("SET session_replication_role = 'origin'"))
        await session.commit()

@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac

@pytest_asyncio.fixture
async def test_data():
    async with AsyncSessionLocal() as session:
        await session.execute(text("RESET ROLE"))

    async with AsyncSessionLocal() as session:
        # Create Organization 1
        org1_id = uuid.uuid4()
        org1 = Organization(id=org1_id, name="Test Org 1", slug="TEST-ORG-1", max_branches=5, default_currency_code="INR")
        session.add(org1)

        # Create Organization 2
        org2_id = uuid.uuid4()
        org2 = Organization(id=org2_id, name="Test Org 2", slug="TEST-ORG-2", max_branches=5, default_currency_code="USD")
        session.add(org2)
        await session.flush()

        # Create Owner for Org 1
        owner1_id = uuid.uuid4()
        owner1 = Owner(
            id=owner1_id,
            org_id=org1_id,
            owner_name="Org Owner 1",
            email="owner1@test.com",
            hashed_password="hash",
            email_verified=True
        )
        session.add(owner1)

        # Create Owner for Org 2
        owner2_id = uuid.uuid4()
        owner2 = Owner(
            id=owner2_id,
            org_id=org2_id,
            owner_name="Org Owner 2",
            email="owner2@test.com",
            hashed_password="hash",
            email_verified=True
        )
        session.add(owner2)
        await session.flush()

        # Create a Branch for Org 1
        branch1_id = uuid.uuid4()
        branch1 = OrgBranch(
            id=branch1_id,
            org_id=org1_id,
            branch_name="Branch 1",
            branch_code="BR1",
            internal_slug="branch-1",
            created_by=owner1_id
        )
        session.add(branch1)
        await session.flush()

        await session.commit()

    return {
        "org1_id": org1_id,
        "owner1_id": owner1_id,
        "branch1_id": branch1_id,
        "org2_id": org2_id,
        "owner2_id": owner2_id
    }

def get_headers(owner_id, org_id, email="owner@test.com"):
    token = create_access_token(str(owner_id), str(org_id), email, role="owner")
    return {
        "Authorization": f"Bearer {token}",
        "X-Request-ID": str(uuid.uuid4()),
        "X-Forwarded-For": "127.0.0.1"
    }

@pytest.mark.asyncio
async def test_membership_plans_api(client, test_data):
    org1_id = test_data["org1_id"]
    owner1_id = test_data["owner1_id"]
    branch1_id = test_data["branch1_id"]
    org2_id = test_data["org2_id"]
    owner2_id = test_data["owner2_id"]

    headers1 = get_headers(owner1_id, org1_id, "owner1@test.com")
    headers2 = get_headers(owner2_id, org2_id, "owner2@test.com")

    # 1. create org-wide plan
    res = await client.post("/membership-plans", json={
        "name": "Org Wide Plan",
        "price": 1000,
        "duration_value": 1,
        "duration_unit": "months"
    }, headers=headers1)
    assert res.status_code == 201
    plan1 = res.json()
    assert plan1["branch_id"] is None
    # 5. currency inherited from organization
    assert plan1["currency"] == "INR"

    # 3. plan_code generated sequentially per org
    # 4. plan_code unique per org
    assert plan1["plan_code"].startswith("TESTOR-")
    assert plan1["plan_code"].endswith("-001")
    plan1_id = plan1["id"]

    # 2. create branch-specific plan
    res = await client.post("/membership-plans", json={
        "name": "Branch Specific Plan",
        "price": 500,
        "duration_value": 1,
        "duration_unit": "months",
        "branch_id": str(branch1_id)
    }, headers=headers1)
    assert res.status_code == 201
    plan2 = res.json()
    assert plan2["branch_id"] == str(branch1_id)
    assert plan2["plan_code"].endswith("-002")

    # 6. negative price rejected
    res = await client.post("/membership-plans", json={
        "name": "Bad Price",
        "price": -10,
        "duration_value": 1,
        "duration_unit": "months"
    }, headers=headers1)
    assert res.status_code == 422

    # 7. zero duration rejected
    res = await client.post("/membership-plans", json={
        "name": "Bad Duration",
        "price": 10,
        "duration_value": 0,
        "duration_unit": "months"
    }, headers=headers1)
    assert res.status_code == 422

    # 8. max_members below 1 rejected
    res = await client.post("/membership-plans", json={
        "name": "Bad Max Members",
        "price": 10,
        "duration_value": 1,
        "duration_unit": "months",
        "max_members": 0
    }, headers=headers1)
    assert res.status_code == 422

    # 9. valid_until before valid_from rejected
    res = await client.post("/membership-plans", json={
        "name": "Bad Dates",
        "price": 10,
        "duration_value": 1,
        "duration_unit": "months",
        "valid_from": "2026-01-02T00:00:00Z",
        "valid_until": "2026-01-01T00:00:00Z"
    }, headers=headers1)
    assert res.status_code == 422

    # 10. branch_id from another org rejected
    res = await client.post("/membership-plans", json={
        "name": "Bad Branch",
        "price": 10,
        "duration_value": 1,
        "duration_unit": "months",
        "branch_id": str(branch1_id) # Branch belongs to org 1
    }, headers=headers2)
    assert res.status_code == 400
    assert "Branch not found" in res.json()["detail"]

    # 11. list active plans
    res = await client.get("/membership-plans", headers=headers1)
    assert res.status_code == 200
    assert len(res.json()) == 2

    # 13. archive plan
    res = await client.post(f"/membership-plans/{plan1_id}/archive", headers=headers1)
    assert res.status_code == 200
    assert res.json()["status"] == "archived"

    # 12. filter by status
    # 14. archived plan not returned in active filter
    res = await client.get("/membership-plans?plan_status=active", headers=headers1)
    assert res.status_code == 200
    assert len(res.json()) == 1
    assert res.json()[0]["name"] == "Branch Specific Plan"

    # Test reactivate archived fails
    res = await client.post(f"/membership-plans/{plan1_id}/activate", headers=headers1)
    assert res.status_code == 400

    # Deactivate the second plan
    plan2_id = plan2["id"]
    res = await client.post(f"/membership-plans/{plan2_id}/deactivate", headers=headers1)
    assert res.status_code == 200
    assert res.json()["status"] == "inactive"

    # 15. inactive plan can be reactivated
    res = await client.post(f"/membership-plans/{plan2_id}/activate", headers=headers1)
    assert res.status_code == 200
    assert res.json()["status"] == "active"

    # 16. org isolation: org A cannot access org B plans
    res = await client.get(f"/membership-plans/{plan1_id}", headers=headers2)
    assert res.status_code == 404
