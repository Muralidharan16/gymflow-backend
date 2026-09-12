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
    """Keep Redis state isolated without mutating shared database rows.

    Database isolation is achieved with a unique tenant identity per test. This
    intentionally avoids cross-domain TRUNCATE/CASCADE cleanup and exercises
    the same tenant/RLS boundaries the application relies on in production.
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
    email = f"branch_contacts_api_{tenant_suffix}@example.com"

    # Register a tenant unique to this test instead of truncating shared tables.
    signup_payload = {
        "org_name": f"API Test Gym {tenant_suffix}",
        "owner_name": "API Tester",
        "email": email,
        "password": "StrongPassword123!",
        "facility_type": "gym"
    }

    with patch("app.services.auth_service.send_verification_email", return_value=True) as mock_send:
        resp = await client.post("/auth/signup", json=signup_payload)
        assert resp.status_code == 200
        raw_token = mock_send.call_args[1]["raw_token"]
        verify_resp = await client.get(
            f"/auth/verify?token={raw_token}",
            follow_redirects=False,
        )
        assert verify_resp.status_code == 307
        assert verify_resp.headers["location"].endswith("/auth/verify-success")

    # Login
    login_payload = {
        "email": email,
        "password": "StrongPassword123!"
    }
    login_resp = await client.post("/auth/login", json=login_payload)
    assert login_resp.status_code == 200

    # Complete Onboarding to create a branch
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

    # Get branch_id
    branches_resp = await client.get("/branches")
    assert branches_resp.status_code == 200
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
    assert len(data) >= 2  # Principal branch creates 2 initially

    # Check they all belong to this branch
    for contact in data:
        assert contact["branch_id"] == branch_id


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
    assert create_resp.status_code == 201
    contact_id = create_resp.json()["id"]

    # Delete it
    del_resp = await client.delete(f"/branches/{branch_id}/contacts/{contact_id}")
    assert del_resp.status_code == 204

    # Verify it doesn't show in list
    list_resp = await client.get(f"/branches/{branch_id}/contacts")
    ids = [contact["id"] for contact in list_resp.json()]
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
    assert create_resp.status_code == 201
    contact_id = create_resp.json()["id"]
    assert create_resp.json()["is_primary"] is False

    # Promote it
    prom_resp = await client.post(
        f"/branches/{branch_id}/contacts/{contact_id}/promote",
        json={"contact_kind": "email"},
    )
    assert prom_resp.status_code == 200
    assert prom_resp.json()["is_primary"] is True

    # Verify list only has 1 primary email
    list_resp = await client.get(f"/branches/{branch_id}/contacts")
    emails = [contact for contact in list_resp.json() if contact["contact_kind"] == "email"]
    primary_emails = [contact for contact in emails if contact["is_primary"]]
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
    assert create_resp.status_code == 201
    contact_id = create_resp.json()["id"]

    audit_resp = await client.get(f"/branches/{branch_id}/contacts/{contact_id}/audit")
    assert audit_resp.status_code == 200
    audits = audit_resp.json()
    assert len(audits) >= 1
    assert audits[0]["action"] == "INSERT"
