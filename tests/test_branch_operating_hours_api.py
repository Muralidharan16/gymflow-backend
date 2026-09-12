import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from typing import AsyncGenerator
import sys
import os
import uuid
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app
from app.core.redis import init_redis, get_redis_utils, close_redis


@pytest_asyncio.fixture(autouse=True)
async def isolate_redis_state():
    """Keep cache state isolated without mutating shared database rows.

    Every test provisions a unique tenant identity. Database isolation therefore
    comes from real tenant/RLS boundaries instead of TRUNCATE/CASCADE cleanup.
    This suite must remain executable through the reduced application/auth
    identities and must never require destructive cross-domain fixture powers.
    """
    await close_redis()
    await init_redis()
    redis_utils = get_redis_utils()
    await redis_utils.client.flushdb()

    yield

    await redis_utils.client.flushdb()


@pytest_asyncio.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test"
    ) as ac:
        yield ac


@pytest_asyncio.fixture
async def auth_session(client):
    tenant_suffix = uuid.uuid4().hex
    email = f"branch_hours_api_{tenant_suffix}@example.com"

    # Register a tenant unique to this test.  Do not clean shared tenant roots or
    # protected branch/contact tables between tests: uniqueness plus forced RLS
    # is the production isolation contract we want this API suite to exercise.
    signup_payload = {
        "org_name": f"API Test Gym {tenant_suffix}",
        "owner_name": "API Tester",
        "email": email,
        "password": "StrongPassword123!",
        "facility_type": "gym"
    }

    with patch("app.services.auth_service.send_verification_email", return_value=True) as mock_send:
        signup_resp = await client.post("/auth/signup", json=signup_payload)
        assert signup_resp.status_code == 200
        raw_token = mock_send.call_args[1]["raw_token"]
        verify_resp = await client.get(
            f"/auth/verify?token={raw_token}",
            follow_redirects=False,
        )
        assert verify_resp.status_code == 307
        assert verify_resp.headers["location"].endswith("/auth/verify-success")

    login_payload = {
        "email": email,
        "password": "StrongPassword123!"
    }
    login_resp = await client.post("/auth/login", json=login_payload)
    assert login_resp.status_code == 200

    onboard_payload = {
        "phone": "9876543210",
        "address_line1": "123 Main Street",
        "city": "Chennai",
        "state": "Tamil Nadu",
        "pincode": "600001",
        "country": "India"
    }
    onboard_resp = await client.post("/onboarding/complete", json=onboard_payload)
    assert onboard_resp.status_code == 200

    branches_resp = await client.get("/branches")
    assert branches_resp.status_code == 200
    branch_id = branches_resp.json()["data"][0]["id"]

    return {"client": client, "branch_id": branch_id, "email": email}


@pytest.mark.asyncio
async def test_api_get_empty_branch_hours(auth_session):
    client = auth_session["client"]
    branch_id = auth_session["branch_id"]

    resp = await client.get(f"/branches/{branch_id}/hours")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_api_put_and_get_branch_hours(auth_session):
    client = auth_session["client"]
    branch_id = auth_session["branch_id"]

    payload = {
        "schedules": [
            {
                "day_of_week": 1,
                "valid_from": "2026-01-01",
                "open_time": "08:00",
                "close_time": "20:00",
                "is_closed": False,
                "is_24_hours": False
            }
        ]
    }

    resp = await client.put(f"/branches/{branch_id}/hours", json=payload)
    assert resp.status_code == 200

    get_resp = await client.get(f"/branches/{branch_id}/hours")
    assert get_resp.status_code == 200
    data = get_resp.json()
    assert len(data) == 1
    assert data[0]["day_of_week"] == 1
    assert data[0]["open_time"] == "08:00:00"


@pytest.mark.asyncio
async def test_api_put_and_get_org_default_hours(auth_session):
    client = auth_session["client"]

    empty_resp = await client.get("/organizations/hours")
    assert empty_resp.status_code == 200
    assert empty_resp.json() == []

    payload = {
        "schedules": [
            {
                "day_of_week": 2,
                "valid_from": "2026-01-01",
                "is_24_hours": True
            }
        ]
    }

    put_resp = await client.put("/organizations/hours", json=payload)
    assert put_resp.status_code == 200

    get_resp = await client.get("/organizations/hours")
    assert get_resp.status_code == 200
    data = get_resp.json()
    assert len(data) == 1
    assert data[0]["is_24_hours"] is True


@pytest.mark.asyncio
async def test_api_put_and_get_special_hours(auth_session):
    client = auth_session["client"]
    branch_id = auth_session["branch_id"]

    payload = {
        "schedules": [
            {
                "special_date": "2026-12-25",
                "is_closed": True,
                "reason": "Christmas"
            }
        ]
    }

    resp = await client.put(f"/branches/{branch_id}/special-hours", json=payload)
    assert resp.status_code == 200

    get_resp = await client.get(f"/branches/{branch_id}/special-hours")
    assert get_resp.status_code == 200
    data = get_resp.json()
    assert len(data) == 1
    assert data[0]["special_date"] == "2026-12-25"
    assert data[0]["is_closed"] is True


@pytest.mark.asyncio
async def test_api_projection_works(auth_session):
    client = auth_session["client"]
    branch_id = auth_session["branch_id"]

    # Projection doesn't exist initially until built by Celery worker.
    # We expect 404.
    resp = await client.get(f"/branches/{branch_id}/hours/projection")
    assert resp.status_code == 404
    assert "not been configured" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_unauthorized_cross_tenant(auth_session):
    branch_id = auth_session["branch_id"]

    other_client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    other_suffix = uuid.uuid4().hex
    other_email = f"branch_hours_other_{other_suffix}@example.com"
    signup_payload = {
        "org_name": f"Other Gym {other_suffix}",
        "owner_name": "Other Tester",
        "email": other_email,
        "password": "StrongPassword123!",
        "facility_type": "gym"
    }
    with patch("app.services.auth_service.send_verification_email", return_value=True) as mock_send:
        signup_resp = await other_client.post("/auth/signup", json=signup_payload)
        assert signup_resp.status_code == 200
        raw_token = mock_send.call_args[1]["raw_token"]
        verify_resp = await other_client.get(
            f"/auth/verify?token={raw_token}",
            follow_redirects=False,
        )
        assert verify_resp.status_code == 307

    login_resp = await other_client.post("/auth/login", json={
        "email": other_email,
        "password": "StrongPassword123!"
    })
    assert login_resp.status_code == 200

    resp = await other_client.get(f"/branches/{branch_id}/hours")

    # The route itself is valid for authenticated staff. PostgreSQL forced RLS
    # must deterministically hide the foreign tenant's rows.
    assert resp.status_code == 200
    assert resp.json() == []

    await other_client.aclose()


@pytest.mark.asyncio
async def test_soft_deleted_not_returned(auth_session):
    client = auth_session["client"]
    branch_id = auth_session["branch_id"]

    payload = {
        "schedules": [
            {
                "day_of_week": 1,
                "valid_from": "2026-01-01",
                "open_time": "08:00",
                "close_time": "20:00"
            }
        ]
    }

    await client.put(f"/branches/{branch_id}/hours", json=payload)

    payload_empty = {"schedules": []}
    await client.put(f"/branches/{branch_id}/hours", json=payload_empty)

    get_resp = await client.get(f"/branches/{branch_id}/hours")
    assert get_resp.status_code == 200
    assert get_resp.json() == []


@pytest.mark.asyncio
async def test_api_multi_slot_hours(auth_session):
    client = auth_session["client"]
    branch_id = auth_session["branch_id"]

    payload = {
        "schedules": [
            {
                "day_of_week": 0,
                "slot_index": 1,
                "valid_from": "2026-01-01",
                "open_time": "05:00",
                "close_time": "10:00"
            },
            {
                "day_of_week": 0,
                "slot_index": 2,
                "valid_from": "2026-01-01",
                "open_time": "16:00",
                "close_time": "22:00"
            }
        ]
    }

    resp = await client.put(f"/branches/{branch_id}/hours", json=payload)
    assert resp.status_code == 200

    get_resp = await client.get(f"/branches/{branch_id}/hours")
    assert get_resp.status_code == 200
    data = get_resp.json()
    assert len(data) == 2

    data.sort(key=lambda x: x["slot_index"])

    assert data[0]["day_of_week"] == 0
    assert data[0]["slot_index"] == 1
    assert data[0]["open_time"] == "05:00:00"
    assert data[0]["close_time"] == "10:00:00"

    assert data[1]["day_of_week"] == 0
    assert data[1]["slot_index"] == 2
    assert data[1]["open_time"] == "16:00:00"
    assert data[1]["close_time"] == "22:00:00"


@pytest.mark.asyncio
async def test_api_overlapping_slots_rejected(auth_session):
    client = auth_session["client"]
    branch_id = auth_session["branch_id"]

    payload = {
        "schedules": [
            {
                "day_of_week": 0,
                "slot_index": 1,
                "valid_from": "2026-01-01",
                "open_time": "05:00",
                "close_time": "10:00"
            },
            {
                "day_of_week": 0,
                "slot_index": 2,
                "valid_from": "2026-01-01",
                "open_time": "09:00",
                "close_time": "12:00"
            }
        ]
    }

    resp = await client.put(f"/branches/{branch_id}/hours", json=payload)
    assert resp.status_code == 422
    assert "overlapping" in resp.text.lower()


@pytest.mark.asyncio
async def test_api_overnight_slots_rejected(auth_session):
    client = auth_session["client"]
    branch_id = auth_session["branch_id"]

    payload = {
        "schedules": [
            {
                "day_of_week": 0,
                "slot_index": 1,
                "valid_from": "2026-01-01",
                "open_time": "22:00",
                "close_time": "04:00"
            }
        ]
    }

    resp = await client.put(f"/branches/{branch_id}/hours", json=payload)
    assert resp.status_code == 422
    assert "overnight" in resp.text.lower()


@pytest.mark.asyncio
async def test_api_special_hours_empty_deletes_all(auth_session):
    client = auth_session["client"]
    branch_id = auth_session["branch_id"]

    payload = {
        "schedules": [
            {
                "special_date": "2026-12-25",
                "is_closed": True,
                "reason": "Christmas"
            }
        ]
    }
    resp = await client.put(f"/branches/{branch_id}/special-hours", json=payload)
    assert resp.status_code == 200

    get_resp = await client.get(f"/branches/{branch_id}/special-hours")
    assert len(get_resp.json()) == 1

    resp_empty = await client.put(f"/branches/{branch_id}/special-hours", json={"schedules": []})
    assert resp_empty.status_code == 200

    get_resp_empty = await client.get(f"/branches/{branch_id}/special-hours")
    assert get_resp_empty.json() == []


@pytest.mark.asyncio
async def test_api_special_hours_partial_replacement(auth_session):
    client = auth_session["client"]
    branch_id = auth_session["branch_id"]

    payload = {
        "schedules": [
            {
                "special_date": "2026-12-24",
                "is_closed": True,
                "reason": "Christmas Eve"
            },
            {
                "special_date": "2026-12-25",
                "is_closed": True,
                "reason": "Christmas"
            }
        ]
    }
    resp = await client.put(f"/branches/{branch_id}/special-hours", json=payload)
    assert resp.status_code == 200

    payload_one = {
        "schedules": [
            {
                "special_date": "2026-12-25",
                "is_closed": True,
                "reason": "Christmas"
            }
        ]
    }
    resp_one = await client.put(f"/branches/{branch_id}/special-hours", json=payload_one)
    assert resp_one.status_code == 200

    get_resp = await client.get(f"/branches/{branch_id}/special-hours")
    data = get_resp.json()
    assert len(data) == 1
    assert data[0]["special_date"] == "2026-12-25"
