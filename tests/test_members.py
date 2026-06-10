import pytest
import pytest_asyncio
import uuid
from httpx import AsyncClient, ASGITransport
from sqlalchemy import text
from app.main import app
from app.core.database import AsyncSessionLocal
from app.core.security import create_access_token
from app.models.organization import Organization
from app.models.auth import Owner
from app.models.org_branch import OrgBranch

async def clear_members_test_data():
    async with AsyncSessionLocal() as session:
        await session.execute(text("RESET ROLE"))
        await session.execute(text("SET session_replication_role = 'replica'"))
        await session.execute(text("DELETE FROM members;"))
        await session.execute(text("DELETE FROM org_branch_state;"))
        await session.execute(text("DELETE FROM org_branches;"))
        await session.execute(text("DELETE FROM owners;"))
        await session.execute(text("DELETE FROM organizations;"))
        await session.execute(text("SET session_replication_role = 'origin'"))
        await session.commit()

@pytest_asyncio.fixture(autouse=True)
async def cleanup_database():
    await clear_members_test_data()
    yield
    await clear_members_test_data()

@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac

@pytest_asyncio.fixture
async def test_data():
    async with AsyncSessionLocal() as session:
        await session.execute(text("RESET ROLE"))
        org_id = uuid.uuid4()
        org = Organization(id=org_id, name="Test Org", slug="TEST-ORG", max_branches=5)
        session.add(org)
        await session.flush()

        owner_id = uuid.uuid4()
        owner = Owner(id=owner_id, org_id=org_id, owner_name="Owner", email="owner@test.com", hashed_password="hash", email_verified=True)
        session.add(owner)
        await session.flush()

        branch_id = uuid.uuid4()
        branch = OrgBranch(id=branch_id, org_id=org_id, branch_name="Branch 1", branch_code="BR1", internal_slug="branch-1", created_by=owner_id)
        session.add(branch)
        await session.flush()

        await session.commit()

        return {"org_id": org_id, "owner_id": owner_id, "branch_id": branch_id}

def get_headers(owner_id, org_id, email="owner@test.com"):
    token = create_access_token(str(owner_id), str(org_id), email, role="owner")
    return {"Authorization": f"Bearer {token}", "X-Request-ID": str(uuid.uuid4()), "X-Forwarded-For": "127.0.0.1"}

@pytest.mark.asyncio
async def test_create_member_minimum_data(client, test_data):
    headers = get_headers(test_data["owner_id"], test_data["org_id"])
    payload = {"name": "Jane Doe", "phone": "9876543210"}
    response = await client.post(f"/organizations/{test_data['org_id']}/members", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["name"] == "Jane Doe"
    assert data["phone"] == "9876543210"
    assert "member_uid" in data

@pytest.mark.asyncio
async def test_create_member_optional_data(client, test_data):
    headers = get_headers(test_data["owner_id"], test_data["org_id"])
    payload = {
        "name": "John Smith",
        "phone": "9876543211",
        "email": "john@smith.com",
        "gender": "male",
        "date_of_birth": "1990-01-01",
        "home_branch_id": str(test_data["branch_id"])
    }
    response = await client.post(f"/organizations/{test_data['org_id']}/members", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["email"] == "john@smith.com"
    assert data["home_branch_id"] == str(test_data["branch_id"])

@pytest.mark.asyncio
async def test_reject_duplicate_active_phone(client, test_data):
    headers = get_headers(test_data["owner_id"], test_data["org_id"])
    payload = {"name": "First", "phone": "9876543212"}
    await client.post(f"/organizations/{test_data['org_id']}/members", json=payload, headers=headers)
    
    payload_dup = {"name": "Duplicate", "phone": "9876543212"}
    response = await client.post(f"/organizations/{test_data['org_id']}/members", json=payload_dup, headers=headers)
    assert response.status_code == 400

@pytest.mark.asyncio
async def test_reject_cross_org_home_branch(client, test_data):
    headers = get_headers(test_data["owner_id"], test_data["org_id"])
    payload = {"name": "Cross Org", "phone": "9876543213", "home_branch_id": str(uuid.uuid4())}
    response = await client.post(f"/organizations/{test_data['org_id']}/members", json=payload, headers=headers)
    assert response.status_code == 400

@pytest.mark.asyncio
async def test_list_members_org_isolation(client, test_data):
    headers = get_headers(test_data["owner_id"], test_data["org_id"])
    payload = {"name": "Jane Doe", "phone": "9876543214"}
    await client.post(f"/organizations/{test_data['org_id']}/members", json=payload, headers=headers)
    
    response = await client.get(f"/organizations/{test_data['org_id']}/members", headers=headers)
    assert response.status_code == 200
    assert len(response.json()["data"]) == 1

@pytest.mark.asyncio
async def test_update_member(client, test_data):
    headers = get_headers(test_data["owner_id"], test_data["org_id"])
    create_resp = await client.post(f"/organizations/{test_data['org_id']}/members", json={"name": "Old Name", "phone": "9876543215"}, headers=headers)
    member_id = create_resp.json()["data"]["id"]
    
    response = await client.patch(f"/organizations/{test_data['org_id']}/members/{member_id}", json={"name": "New Name"}, headers=headers)
    assert response.status_code == 200
    assert response.json()["data"]["name"] == "New Name"

@pytest.mark.asyncio
async def test_soft_delete_member(client, test_data):
    headers = get_headers(test_data["owner_id"], test_data["org_id"])
    create_resp = await client.post(f"/organizations/{test_data['org_id']}/members", json={"name": "To Delete", "phone": "9876543216"}, headers=headers)
    member_id = create_resp.json()["data"]["id"]
    
    del_resp = await client.delete(f"/organizations/{test_data['org_id']}/members/{member_id}", headers=headers)
    assert del_resp.status_code == 200
    
    list_resp = await client.get(f"/organizations/{test_data['org_id']}/members", headers=headers)
    assert len(list_resp.json()["data"]) == 0
