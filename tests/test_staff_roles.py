import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from fastapi import Depends
from httpx import ASGITransport, AsyncClient

from app.core.database import update_session_context
from app.core.redis import close_redis, init_redis
from app.core.security import create_access_token
from app.main import app
from app.models.auth import Owner
from app.models.org_branch import OrgBranch, OrgBranchState
from app.models.organization import Organization
from app.models.organization_user import OrganizationUser


@pytest_asyncio.fixture(autouse=True)
async def redis_connection():
    """Keep Redis available without globally deleting shared test keys."""
    await init_redis()
    try:
        yield
    finally:
        await close_redis()


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac


@pytest_asyncio.fixture
async def test_data(auth_db_session):
    """Create an isolated tenant through bounded test identities only."""
    suffix = uuid.uuid4().hex[:10]
    org_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    branch_id = uuid.uuid4()
    owner_email = f"owner-roles+{suffix}@test.com"

    auth_db_session.add(
        Organization(
            id=org_id,
            name=f"Staff Roles Test Org {suffix}",
            slug=f"staff-roles-{suffix}",
            max_branches=5,
            default_currency_code="INR",
        )
    )
    await auth_db_session.flush()

    auth_db_session.add(
        Owner(
            id=owner_id,
            org_id=org_id,
            owner_name="Org Owner",
            email=owner_email,
            hashed_password="hash",
            email_verified=True,
        )
    )
    await auth_db_session.commit()

    await update_session_context(
        auth_db_session,
        principal_id=str(owner_id),
        principal_type="owner",
        org_id=str(org_id),
        role="owner",
        ip_address="127.0.0.1",
        user_agent="pytest-staff-roles",
        request_id=f"staff-roles-{suffix}-{uuid.uuid4()}",
    )

    # The application requires a corresponding organization user for role FKs.
    auth_db_session.add(
        OrganizationUser(
            id=owner_id,
            org_id=org_id,
            name="Org Owner User",
            email=owner_email,
            password_hash="hash",
            is_active=True,
            is_verified=True,
        )
    )
    await auth_db_session.flush()

    auth_db_session.add(
        OrgBranch(
            id=branch_id,
            org_id=org_id,
            branch_name="Branch Alpha",
            branch_code="ALPHA01",
            internal_slug=f"branch-alpha-{suffix}",
            created_by=owner_id,
        )
    )
    await auth_db_session.flush()

    auth_db_session.add(
        OrgBranchState(
            branch_id=branch_id,
            org_id=org_id,
            search_epoch_ulid="01AN4V076P0000000000000000",
            branch_status="active",
            is_active=True,
        )
    )
    await auth_db_session.commit()

    return {
        "suffix": suffix,
        "org_id": org_id,
        "owner_id": owner_id,
        "owner_email": owner_email,
        "branch_id": branch_id,
    }


def _owner_headers(test_data):
    token = create_access_token(
        str(test_data["owner_id"]),
        str(test_data["org_id"]),
        test_data["owner_email"],
        role="owner",
    )
    return {
        "Authorization": f"Bearer {token}",
        "X-Request-ID": str(uuid.uuid4()),
        "X-Forwarded-For": "127.0.0.1",
    }


@pytest.mark.asyncio
async def test_organization_user_flow(client, test_data):
    suffix = test_data["suffix"]
    headers = _owner_headers(test_data)
    email = f"joe+{suffix}@test.com"

    response = await client.post(
        "/organizations/users",
        json={
            "name": "Gym Manager Joe",
            "email": email,
            "password": "Password123!",
            "phone": "+1234567890",
            "is_active": True,
        },
        headers=headers,
    )
    assert response.status_code == 201
    user_data = response.json()["data"]
    assert user_data["name"] == "Gym Manager Joe"
    assert user_data["email"] == email
    assert user_data["is_active"] is True
    user_id = user_data["id"]

    response = await client.get(
        f"/organizations/users/{user_id}",
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["data"]["name"] == "Gym Manager Joe"

    response = await client.get("/organizations/users", headers=headers)
    assert response.status_code == 200
    assert any(
        user["id"] == user_id for user in response.json()["data"]
    )

    response = await client.patch(
        f"/organizations/users/{user_id}",
        json={"name": "Joe Updated", "phone": "+9876543210"},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["data"]["name"] == "Joe Updated"
    assert response.json()["data"]["phone"] == "+9876543210"


@pytest.mark.asyncio
async def test_branch_staff_role_assignment(client, test_data):
    suffix = test_data["suffix"]
    branch_id = test_data["branch_id"]
    headers = _owner_headers(test_data)
    email = f"bob+{suffix}@test.com"

    res_user = await client.post(
        "/organizations/users",
        json={
            "name": "Trainer Bob",
            "email": email,
            "password": "TrainerPassword123!",
            "phone": "+1122334455",
            "is_active": True,
        },
        headers=headers,
    )
    assert res_user.status_code == 201
    user_id = res_user.json()["data"]["id"]

    role_payload = {
        "user_id": user_id,
        "role": "trainer",
        "effective_from": datetime.now(timezone.utc).isoformat(),
        "metadata": {"certifications": ["NASM-CPT"]},
    }
    response = await client.post(
        f"/branches/{branch_id}/staff",
        json=role_payload,
        headers=headers,
    )
    assert response.status_code == 201
    role_data = response.json()["data"]
    assert role_data["role"] == "trainer"
    assert role_data["user_id"] == user_id
    assert role_data["metadata"]["certifications"] == ["NASM-CPT"]
    assignment_id = role_data["id"]

    response_overlap = await client.post(
        f"/branches/{branch_id}/staff",
        json=role_payload,
        headers=headers,
    )
    assert response_overlap.status_code == 400
    assert (
        "already has an active or scheduled role assignment"
        in response_overlap.json()["detail"]
    )

    response_list = await client.get(
        f"/branches/{branch_id}/staff",
        headers=headers,
    )
    assert response_list.status_code == 200
    assert len(response_list.json()["data"]) == 1
    assert response_list.json()["data"][0]["user_id"] == user_id

    response_revoke = await client.delete(
        f"/branches/{branch_id}/staff/{assignment_id}",
        headers=headers,
    )
    assert response_revoke.status_code == 200
    assert response_revoke.json()["data"]["revoked_at"] is not None
    assert response_revoke.json()["data"]["revoked_by"] is not None

    response_list_empty = await client.get(
        f"/branches/{branch_id}/staff?include_inactive=false",
        headers=headers,
    )
    assert response_list_empty.status_code == 200
    assert len(response_list_empty.json()["data"]) == 0

    response_list_all = await client.get(
        f"/branches/{branch_id}/staff?include_inactive=true",
        headers=headers,
    )
    assert response_list_all.status_code == 200
    assert len(response_list_all.json()["data"]) == 1


@pytest.mark.asyncio
async def test_access_control_via_dependency(client, test_data):
    suffix = test_data["suffix"]
    org_id = test_data["org_id"]
    branch_id = test_data["branch_id"]
    owner_headers = _owner_headers(test_data)
    user1_email = f"user1+{suffix}@test.com"
    user2_email = f"user2+{suffix}@test.com"

    user1_res = await client.post(
        "/organizations/users",
        json={
            "name": "User One",
            "email": user1_email,
            "password": "Password123!",
        },
        headers=owner_headers,
    )
    assert user1_res.status_code == 201
    user1_id = user1_res.json()["data"]["id"]

    user2_res = await client.post(
        "/organizations/users",
        json={
            "name": "User Two",
            "email": user2_email,
            "password": "Password123!",
        },
        headers=owner_headers,
    )
    assert user2_res.status_code == 201
    user2_id = user2_res.json()["data"]["id"]

    role_res = await client.post(
        f"/branches/{branch_id}/staff",
        json={
            "user_id": user1_id,
            "role": "trainer",
            "effective_from": datetime.now(timezone.utc).isoformat(),
        },
        headers=owner_headers,
    )
    assert role_res.status_code == 201

    from app.core.deps import require_branch_staff_role

    @app.get("/branches/{branch_id}/test-trainer-endpoint")
    async def dummy_endpoint(
        staff=Depends(await require_branch_staff_role(["trainer"])),
    ):
        return {"status": "ok", "user_id": str(staff.id)}

    user1_token = create_access_token(
        str(user1_id),
        str(org_id),
        user1_email,
        role="trainer",
    )
    response_user1 = await client.get(
        f"/branches/{branch_id}/test-trainer-endpoint",
        headers={"Authorization": f"Bearer {user1_token}"},
    )
    assert response_user1.status_code == 200
    assert response_user1.json()["status"] == "ok"

    user2_token = create_access_token(
        str(user2_id),
        str(org_id),
        user2_email,
        role="trainer",
    )
    response_user2 = await client.get(
        f"/branches/{branch_id}/test-trainer-endpoint",
        headers={"Authorization": f"Bearer {user2_token}"},
    )
    assert response_user2.status_code == 403
    assert "do not have the required role" in response_user2.json()["detail"]

    response_owner = await client.get(
        f"/branches/{branch_id}/test-trainer-endpoint",
        headers=owner_headers,
    )
    assert response_owner.status_code == 200
    assert response_owner.json()["status"] == "ok"
