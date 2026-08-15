from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest

from app.core.redis import redis_client
from app.core.security import create_access_token
from test_staff_roles import client, test_data, _owner_headers


@pytest.mark.asyncio
async def test_p3d_revoked_role_cannot_survive_stale_jwt_or_redis_cache(
    client,
    test_data,
) -> None:
    """Revocation must beat both JWT lifetime and a deliberately stale cache."""
    branch_id = test_data["branch_id"]
    org_id = test_data["org_id"]
    owner_headers = _owner_headers(test_data)
    suffix = test_data["suffix"]
    staff_email = f"p3d-revocation+{suffix}@test.com"

    user_response = await client.post(
        "/organizations/users",
        json={
            "name": "P3D Revocation Trainer",
            "email": staff_email,
            "password": "P3DRevocationPassword123!",
            "is_active": True,
        },
        headers=owner_headers,
    )
    assert user_response.status_code == 201
    user_id = uuid.UUID(user_response.json()["data"]["id"])

    assignment_response = await client.post(
        f"/branches/{branch_id}/staff",
        json={
            "user_id": str(user_id),
            "role": "trainer",
            "effective_from": datetime.now(timezone.utc).isoformat(),
        },
        headers=owner_headers,
    )
    assert assignment_response.status_code == 201
    assignment_id = assignment_response.json()["data"]["id"]

    stale_token = create_access_token(
        str(user_id),
        str(org_id),
        staff_email,
        role="trainer",
        branch_ids=[str(branch_id)],
        principal_type="organization_user",
    )
    stale_headers = {"Authorization": f"Bearer {stale_token}"}

    before_list = await client.get("/branches", headers=stale_headers)
    assert before_list.status_code == 200
    assert str(branch_id) in {row["id"] for row in before_list.json()["data"]}

    before_state = await client.get(
        f"/branches/{branch_id}/state",
        headers=stale_headers,
    )
    assert before_state.status_code == 200

    revoke_response = await client.delete(
        f"/branches/{branch_id}/staff/{assignment_id}",
        headers=owner_headers,
    )
    assert revoke_response.status_code == 200

    # Re-introduce exactly the stale role cache that a failed invalidation or a
    # delayed cache delete could otherwise leave behind. Authorization must not
    # consult this value.
    await redis_client.set(
        f"user:branch_roles:{user_id}",
        json.dumps({str(branch_id): ["trainer"]}),
        ex=3600,
    )

    after_list = await client.get("/branches", headers=stale_headers)
    assert after_list.status_code == 200
    assert str(branch_id) not in {row["id"] for row in after_list.json()["data"]}

    after_state = await client.get(
        f"/branches/{branch_id}/state",
        headers=stale_headers,
    )
    assert after_state.status_code == 403
    assert "authorized scope" in after_state.json()["detail"]
