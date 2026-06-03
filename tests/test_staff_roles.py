import pytest
import pytest_asyncio
import uuid
from httpx import AsyncClient, ASGITransport
from datetime import datetime, timezone, timedelta
from sqlalchemy import select, text
from unittest.mock import patch

from fastapi import Depends
from app.main import app
from app.core.database import AsyncSessionLocal
from app.core.security import create_access_token
from app.models.organization import Organization
from app.models.auth import Owner
from app.models.org_branch import OrgBranch, OrgBranchState
from app.models.organization_user import OrganizationUser, BranchStaffRole, BranchStaffRoleEnum
from app.core.redis import init_redis, get_redis_utils, close_redis

@pytest_asyncio.fixture(autouse=True)
async def cleanup_database_and_redis():
    await init_redis()
    redis_utils = get_redis_utils()
    await redis_utils.client.flushdb()
    
    yield
    
    async with AsyncSessionLocal() as session:
        # Reset role to superuser before running cleanup commands
        await session.execute(text("RESET ROLE"))
        # Bypass append-only and history checks
        await session.execute(text("SET session_replication_role = 'replica'"))
        # Drop all rows created during tests in correct dependency order
        await session.execute(text("DELETE FROM branch_staff_roles;"))
        await session.execute(text("TRUNCATE TABLE branch_audit_log CASCADE;"))
        await session.execute(text("DELETE FROM organization_members;"))
        await session.execute(text("DELETE FROM organization_users;"))
        await session.execute(text("DELETE FROM organization_addresses;"))
        await session.execute(text("DELETE FROM member_addresses;"))
        await session.execute(text("DELETE FROM org_branch_state;"))
        await session.execute(text("DELETE FROM org_branches;"))
        await session.execute(text("DELETE FROM owners;"))
        await session.execute(text("DELETE FROM organizations;"))
        await session.execute(text("SET session_replication_role = 'origin'"))
        await session.commit()
    await close_redis()


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac

@pytest_asyncio.fixture
async def test_data():
    async with AsyncSessionLocal() as session:
        # Reset role to superuser and grant permissions
        await session.execute(text("RESET ROLE"))
        await session.execute(text("GRANT SELECT, INSERT, UPDATE, DELETE ON public.branch_audit_log TO app_rls_executor;"))
        await session.execute(text("GRANT SELECT, INSERT, UPDATE, DELETE ON public.branch_audit_log_y2026_m05 TO app_rls_executor;"))
        await session.commit()

    async with AsyncSessionLocal() as session:
        # 1. Create Organization
        org_id = uuid.uuid4()
        org = Organization(id=org_id, name="Staff Roles Test Org", max_branches=5)
        session.add(org)
        await session.flush()

        # 2. Create Owner (admin)
        owner_id = uuid.uuid4()
        owner = Owner(
            id=owner_id,
            org_id=org_id,
            owner_name="Org Owner",
            email="owner_roles@test.com",
            hashed_password="hash",
            email_verified=True
        )
        session.add(owner)
        await session.flush()

        # 2b. Add a corresponding user in organization_users for owner so FK doesn't fail
        org_user_owner = OrganizationUser(
            id=owner_id,
            org_id=org_id,
            name="Org Owner User",
            email="owner_roles@test.com",
            password_hash="hash",
            is_active=True,
            is_verified=True
        )
        session.add(org_user_owner)
        await session.flush()

        # 3. Create a Branch
        branch_id = uuid.uuid4()
        branch = OrgBranch(
            id=branch_id,
            org_id=org_id,
            branch_name="Branch Alpha",
            branch_code="ALPHA01",
            internal_slug="branch-alpha",
            created_by=owner_id
        )
        session.add(branch)
        await session.flush()

        # Create branch state to make branch active
        branch_state = OrgBranchState(
            branch_id=branch_id,
            org_id=org_id,
            search_epoch_ulid="01AN4V076P0000000000000000",
            branch_status="active",
            is_active=True
        )
        session.add(branch_state)
        await session.commit()

    return {
        "org_id": org_id,
        "owner_id": owner_id,
        "branch_id": branch_id
    }

@pytest.mark.asyncio
async def test_organization_user_flow(client, test_data):
    org_id = test_data["org_id"]
    owner_id = test_data["owner_id"]
    
    # Create admin auth headers
    token = create_access_token(str(owner_id), str(org_id), "owner_roles@test.com", role="owner")
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Request-ID": str(uuid.uuid4()),
        "X-Forwarded-For": "127.0.0.1"
    }

    # 1. Create Organization User
    user_payload = {
        "name": "Gym Manager Joe",
        "email": "joe@test.com",
        "password": "Password123!",
        "phone": "+1234567890",
        "is_active": True
    }
    
    response = await client.post("/organizations/users", json=user_payload, headers=headers)
    assert response.status_code == 201
    user_data = response.json()["data"]
    assert user_data["name"] == "Gym Manager Joe"
    assert user_data["email"] == "joe@test.com"
    assert user_data["is_active"] is True
    
    user_id = user_data["id"]

    # 2. Get Organization User
    response = await client.get(f"/organizations/users/{user_id}", headers=headers)
    assert response.status_code == 200
    assert response.json()["data"]["name"] == "Gym Manager Joe"

    # 3. List Organization Users
    response = await client.get("/organizations/users", headers=headers)
    assert response.status_code == 200
    assert len(response.json()["data"]) >= 1

    # 4. Update Organization User
    update_payload = {
        "name": "Joe Updated",
        "phone": "+9876543210"
    }
    response = await client.patch(f"/organizations/users/{user_id}", json=update_payload, headers=headers)
    assert response.status_code == 200
    assert response.json()["data"]["name"] == "Joe Updated"
    assert response.json()["data"]["phone"] == "+9876543210"

@pytest.mark.asyncio
async def test_branch_staff_role_assignment(client, test_data):
    org_id = test_data["org_id"]
    owner_id = test_data["owner_id"]
    branch_id = test_data["branch_id"]
    
    token = create_access_token(str(owner_id), str(org_id), "owner_roles@test.com", role="owner")
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Request-ID": str(uuid.uuid4()),
        "X-Forwarded-For": "127.0.0.1"
    }

    # 1. Create staff user
    user_payload = {
        "name": "Trainer Bob",
        "email": "bob@test.com",
        "password": "TrainerPassword123!",
        "phone": "+1122334455",
        "is_active": True
    }
    res_user = await client.post("/organizations/users", json=user_payload, headers=headers)
    user_id = res_user.json()["data"]["id"]

    # 2. Assign role 'trainer' to Bob
    role_payload = {
        "user_id": user_id,
        "role": "trainer",
        "effective_from": datetime.now(timezone.utc).isoformat(),
        "metadata": {"certifications": ["NASM-CPT"]}
    }
    response = await client.post(f"/branches/{branch_id}/staff", json=role_payload, headers=headers)
    assert response.status_code == 201
    role_data = response.json()["data"]
    assert role_data["role"] == "trainer"
    assert role_data["user_id"] == user_id
    assert role_data["metadata"]["certifications"] == ["NASM-CPT"]
    
    assignment_id = role_data["id"]

    # 3. Test overlap restriction: try to assign Trainer role again for Bob in same branch
    response_overlap = await client.post(f"/branches/{branch_id}/staff", json=role_payload, headers=headers)
    assert response_overlap.status_code == 400
    assert "already has an active or scheduled role assignment" in response_overlap.json()["detail"]

    # 4. List Branch Staff Roles
    response_list = await client.get(f"/branches/{branch_id}/staff", headers=headers)
    assert response_list.status_code == 200
    assert len(response_list.json()["data"]) == 1
    assert response_list.json()["data"][0]["user_id"] == user_id

    # 5. Revoke Role
    response_revoke = await client.delete(f"/branches/{branch_id}/staff/{assignment_id}", headers=headers)
    assert response_revoke.status_code == 200
    assert response_revoke.json()["data"]["revoked_at"] is not None
    assert response_revoke.json()["data"]["revoked_by"] is not None

    # 6. List branch staff with include_inactive=False (should be empty now)
    response_list_empty = await client.get(f"/branches/{branch_id}/staff?include_inactive=false", headers=headers)
    assert response_list_empty.status_code == 200
    assert len(response_list_empty.json()["data"]) == 0

    # 7. List branch staff with include_inactive=True (should have 1)
    response_list_all = await client.get(f"/branches/{branch_id}/staff?include_inactive=true", headers=headers)
    assert response_list_all.status_code == 200
    assert len(response_list_all.json()["data"]) == 1

@pytest.mark.asyncio
async def test_access_control_via_dependency(client, test_data):
    org_id = test_data["org_id"]
    owner_id = test_data["owner_id"]
    branch_id = test_data["branch_id"]

    owner_token = create_access_token(str(owner_id), str(org_id), "owner_roles@test.com", role="owner")
    owner_headers = {
        "Authorization": f"Bearer {owner_token}",
        "X-Request-ID": str(uuid.uuid4()),
        "X-Forwarded-For": "127.0.0.1"
    }

    # 1. Create 2 Users: user1 (will be assigned trainer), user2 (no roles assigned)
    user1_res = await client.post("/organizations/users", json={
        "name": "User One", "email": "user1@test.com", "password": "Password123!"
    }, headers=owner_headers)
    user1_id = user1_res.json()["data"]["id"]

    user2_res = await client.post("/organizations/users", json={
        "name": "User Two", "email": "user2@test.com", "password": "Password123!"
    }, headers=owner_headers)
    user2_id = user2_res.json()["data"]["id"]

    # 2. Assign trainer role to user1
    await client.post(f"/branches/{branch_id}/staff", json={
        "user_id": user1_id,
        "role": "trainer",
        "effective_from": datetime.now(timezone.utc).isoformat()
    }, headers=owner_headers)

    # 3. Create a dummy endpoint protected by require_branch_staff_role
    from app.core.deps import require_branch_staff_role
    
    @app.get("/branches/{branch_id}/test-trainer-endpoint")
    async def dummy_endpoint(
        staff=Depends(await require_branch_staff_role(["trainer"]))
    ):
        return {"status": "ok", "user_id": str(staff.id)}

    # Test as user1 (has active trainer role): should succeed
    user1_token = create_access_token(str(user1_id), str(org_id), "user1@test.com", role="trainer")
    user1_headers = {"Authorization": f"Bearer {user1_token}"}
    response_user1 = await client.get(f"/branches/{branch_id}/test-trainer-endpoint", headers=user1_headers)
    assert response_user1.status_code == 200
    assert response_user1.json()["status"] == "ok"

    # Test as user2 (no active roles): should be blocked (403)
    user2_token = create_access_token(str(user2_id), str(org_id), "user2@test.com", role="trainer")
    user2_headers = {"Authorization": f"Bearer {user2_token}"}
    response_user2 = await client.get(f"/branches/{branch_id}/test-trainer-endpoint", headers=user2_headers)
    assert response_user2.status_code == 403
    assert "do not have the required role" in response_user2.json()["detail"]

    # Test as Org Owner: should bypass check and succeed
    response_owner = await client.get(f"/branches/{branch_id}/test-trainer-endpoint", headers=owner_headers)
    assert response_owner.status_code == 200
    assert response_owner.json()["status"] == "ok"
