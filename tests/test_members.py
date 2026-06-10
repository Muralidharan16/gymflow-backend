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
from app.models.gym import Gym
from app.models.org_branch import OrgBranch

async def clear_members_test_data():
    async with AsyncSessionLocal() as session:
        await session.execute(text("RESET ROLE"))
        await session.execute(text("SET session_replication_role = 'replica'"))
        await session.execute(text("DELETE FROM members;"))
        await session.execute(text("DELETE FROM gyms;"))
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
        org1_id = uuid.uuid4()
        org2_id = uuid.uuid4()
        org1 = Organization(id=org1_id, name="Test Org 1", slug="TEST-ORG-1", max_branches=5)
        org2 = Organization(id=org2_id, name="Test Org 2", slug="TEST-ORG-2", max_branches=5)
        session.add_all([org1, org2])
        await session.flush()

        owner1_id = uuid.uuid4()
        owner2_id = uuid.uuid4()
        owner1 = Owner(id=owner1_id, org_id=org1_id, owner_name="Owner 1", email="owner1@test.com", hashed_password="hash", email_verified=True)
        owner2 = Owner(id=owner2_id, org_id=org2_id, owner_name="Owner 2", email="owner2@test.com", hashed_password="hash", email_verified=True)
        session.add_all([owner1, owner2])
        await session.flush()

        branch1_id = uuid.uuid4()
        branch2_id = uuid.uuid4()
        branch_other_id = uuid.uuid4()
        branch1 = OrgBranch(id=branch1_id, org_id=org1_id, branch_name="Branch 1", branch_code="BR1", internal_slug="branch-1", created_by=owner1_id)
        branch2 = OrgBranch(id=branch2_id, org_id=org1_id, branch_name="Branch 2", branch_code="BR2", internal_slug="branch-2", created_by=owner1_id)
        branch_other = OrgBranch(id=branch_other_id, org_id=org2_id, branch_name="Other Branch", branch_code="OB1", internal_slug="other-branch", created_by=owner2_id)
        session.add_all([branch1, branch2, branch_other])
        await session.flush()

        gym1_id = uuid.uuid4()
        gym1 = Gym(id=gym1_id, org_id=org1_id, name="Legacy Gym", gymu_id="LEGACY-001")
        session.add(gym1)
        await session.flush()

        await session.commit()

        return {
            "org_id": org1_id,
            "owner_id": owner1_id,
            "branch_id": branch1_id,
            "branch2_id": branch2_id,
            "gym_id": gym1_id,
            "org2_id": org2_id,
            "owner2_id": owner2_id,
            "branch_other_id": branch_other_id,
        }

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
    assert data["org_id"] == str(test_data["org_id"])
    assert data["gym_id"] is None

@pytest.mark.asyncio
async def test_reject_missing_required_create_fields(client, test_data):
    headers = get_headers(test_data["owner_id"], test_data["org_id"])

    missing_name = await client.post(
        f"/organizations/{test_data['org_id']}/members",
        json={"phone": "9876543299"},
        headers=headers,
    )
    assert missing_name.status_code == 422

    missing_phone = await client.post(
        f"/organizations/{test_data['org_id']}/members",
        json={"name": "No Phone"},
        headers=headers,
    )
    assert missing_phone.status_code == 422

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
    payload = {"name": "Cross Org", "phone": "9876543213", "home_branch_id": str(test_data["branch_other_id"])}
    response = await client.post(f"/organizations/{test_data['org_id']}/members", json=payload, headers=headers)
    assert response.status_code == 400

@pytest.mark.asyncio
async def test_allow_same_phone_in_different_org(client, test_data):
    headers1 = get_headers(test_data["owner_id"], test_data["org_id"], "owner1@test.com")
    headers2 = get_headers(test_data["owner2_id"], test_data["org2_id"], "owner2@test.com")
    payload = {"name": "Shared Phone", "phone": "9876543214"}

    response1 = await client.post(f"/organizations/{test_data['org_id']}/members", json=payload, headers=headers1)
    response2 = await client.post(f"/organizations/{test_data['org2_id']}/members", json={**payload, "name": "Shared Phone Other"}, headers=headers2)

    assert response1.status_code == 200, response1.text
    assert response2.status_code == 200, response2.text
    assert response1.json()["data"]["org_id"] != response2.json()["data"]["org_id"]

@pytest.mark.asyncio
async def test_list_members_org_isolation_and_branch_filter(client, test_data):
    headers = get_headers(test_data["owner_id"], test_data["org_id"])
    headers2 = get_headers(test_data["owner2_id"], test_data["org2_id"], "owner2@test.com")
    await client.post(
        f"/organizations/{test_data['org_id']}/members",
        json={"name": "Branch One", "phone": "9876543215", "home_branch_id": str(test_data["branch_id"])},
        headers=headers,
    )
    await client.post(
        f"/organizations/{test_data['org_id']}/members",
        json={"name": "Branch Two", "phone": "9876543216", "home_branch_id": str(test_data["branch2_id"])},
        headers=headers,
    )
    await client.post(
        f"/organizations/{test_data['org2_id']}/members",
        json={"name": "Other Org", "phone": "9876543217", "home_branch_id": str(test_data["branch_other_id"])},
        headers=headers2,
    )
    
    response = await client.get(f"/organizations/{test_data['org_id']}/members", headers=headers)
    assert response.status_code == 200
    assert len(response.json()["data"]) == 2
    assert {m["org_id"] for m in response.json()["data"]} == {str(test_data["org_id"])}

    branch_response = await client.get(
        f"/organizations/{test_data['org_id']}/members",
        params={"home_branch_id": str(test_data["branch_id"])},
        headers=headers,
    )
    assert branch_response.status_code == 200
    assert len(branch_response.json()["data"]) == 1
    assert branch_response.json()["data"][0]["home_branch_id"] == str(test_data["branch_id"])

@pytest.mark.asyncio
async def test_get_member_by_id_enforces_org_isolation(client, test_data):
    headers1 = get_headers(test_data["owner_id"], test_data["org_id"], "owner1@test.com")
    headers2 = get_headers(test_data["owner2_id"], test_data["org2_id"], "owner2@test.com")
    create_resp = await client.post(
        f"/organizations/{test_data['org_id']}/members",
        json={"name": "Org One Member", "phone": "9876543218"},
        headers=headers1,
    )
    member_id = create_resp.json()["data"]["id"]

    wrong_path = await client.get(f"/organizations/{test_data['org2_id']}/members/{member_id}", headers=headers1)
    assert wrong_path.status_code == 403

    wrong_org_lookup = await client.get(f"/organizations/{test_data['org2_id']}/members/{member_id}", headers=headers2)
    assert wrong_org_lookup.status_code == 404

@pytest.mark.asyncio
async def test_update_member_allowed_fields_and_immutable_generated_fields(client, test_data):
    headers = get_headers(test_data["owner_id"], test_data["org_id"])
    create_resp = await client.post(f"/organizations/{test_data['org_id']}/members", json={"name": "Old Name", "phone": "9876543220"}, headers=headers)
    created = create_resp.json()["data"]
    member_id = created["id"]
    
    response = await client.patch(
        f"/organizations/{test_data['org_id']}/members/{member_id}",
        json={"name": "New Name", "member_uid": "SHOULD-NOT-CHANGE", "qr_token": "SHOULD-NOT-CHANGE"},
        headers=headers,
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["name"] == "New Name"
    assert data["member_uid"] == created["member_uid"]
    assert data["qr_token"] == created["qr_token"]

@pytest.mark.asyncio
async def test_soft_delete_member(client, test_data):
    headers = get_headers(test_data["owner_id"], test_data["org_id"])
    create_resp = await client.post(f"/organizations/{test_data['org_id']}/members", json={"name": "To Delete", "phone": "9876543221"}, headers=headers)
    member_id = create_resp.json()["data"]["id"]
    
    del_resp = await client.delete(f"/organizations/{test_data['org_id']}/members/{member_id}", headers=headers)
    assert del_resp.status_code == 200
    
    list_resp = await client.get(f"/organizations/{test_data['org_id']}/members", headers=headers)
    assert len(list_resp.json()["data"]) == 0

@pytest.mark.asyncio
async def test_legacy_gym_member_create_still_sets_org(client, test_data):
    headers = get_headers(test_data["owner_id"], test_data["org_id"])
    response = await client.post(
        f"/gyms/{test_data['gym_id']}/members",
        json={"name": "Legacy Member", "phone": "9876543222"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["gym_id"] == str(test_data["gym_id"])
    assert data["org_id"] == str(test_data["org_id"])
