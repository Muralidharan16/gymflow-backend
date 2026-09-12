import uuid
from datetime import timezone
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.database import update_session_context
from app.core.redis import close_redis, init_redis
from app.main import app
from app.models.auth import Owner
from app.schemas.branch_contacts import BranchContactORM, ContactKind


@pytest_asyncio.fixture(autouse=True)
async def redis_connection():
    """Keep Redis available without globally deleting another test's keys."""
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


@pytest.mark.asyncio
async def test_onboarding_creates_contacts_successfully(client, auth_db_session):
    suffix = uuid.uuid4().hex[:12]
    email = f"onboard-test+{suffix}@example.com"

    # 1. Sign up with a test-unique identity. The disposable database may hold
    # rows from earlier tests; isolation must come from unique tenant data, not
    # destructive tenant-root cleanup.
    signup_payload = {
        "org_name": f"Onboard Test Gym {suffix}",
        "owner_name": "Test Owner",
        "email": email,
        "password": "StrongPassword123!",
        "facility_type": "gym",
    }

    with patch(
        "app.services.auth_service.send_verification_email",
        return_value=True,
    ) as mock_send:
        resp = await client.post("/auth/signup", json=signup_payload)
        assert resp.status_code == 200
        raw_token = mock_send.call_args[1]["raw_token"]

        # Verify (this creates Owner, Org, Gym)
        verify_resp = await client.get(
            f"/auth/verify?token={raw_token}",
            follow_redirects=False,
        )
        assert verify_resp.status_code == 307

    # 2. Login to get session cookies
    login_resp = await client.post(
        "/auth/login",
        json={
            "email": email,
            "password": "StrongPassword123!",
        },
    )
    assert login_resp.status_code == 200

    # 3. Complete onboarding
    onboard_payload = {
        "phone": "9876543210",
        "address_line1": "123 Main Street",
        "address_line2": "Suite 4B",
        "city": "Chennai",
        "state": "Tamil Nadu",
        "pincode": "600001",
        "country": "India",
    }
    onboard_resp = await client.post(
        "/onboarding/complete",
        json=onboard_payload,
    )
    assert onboard_resp.status_code == 200

    # 4. Verify persisted contacts through the bounded auth/runtime test
    # identity with the same typed tenant context production requests install.
    owner_result = await auth_db_session.execute(
        select(Owner).where(Owner.email == email)
    )
    owner = owner_result.scalar_one()
    await update_session_context(
        auth_db_session,
        principal_id=str(owner.id),
        principal_type="owner",
        org_id=str(owner.org_id),
        role="owner",
        ip_address="127.0.0.1",
        user_agent="pytest-onboarding-contacts",
        request_id=f"onboarding-contacts-{suffix}",
    )

    contacts_result = await auth_db_session.execute(
        select(BranchContactORM).where(
            BranchContactORM.org_id == owner.org_id
        )
    )
    contacts = contacts_result.scalars().all()
    assert len(contacts) == 2

    phone_contact = next(
        contact
        for contact in contacts
        if contact.contact_kind == ContactKind.PHONE
    )
    email_contact = next(
        contact
        for contact in contacts
        if contact.contact_kind == ContactKind.EMAIL
    )

    assert phone_contact.phone_e164 == "+919876543210"
    assert phone_contact.is_primary is True
    assert phone_contact.visibility_scope == "public"

    assert email_contact.email_raw == email
    assert email_contact.is_primary is True
    assert email_contact.visibility_scope == "public"

    # 5. RLS-scoped branch listing must expose this tenant's principal branch.
    branches_resp = await client.get("/branches")
    assert branches_resp.status_code == 200
    branches_data = branches_resp.json()["data"]
    principal_branch = next(
        branch for branch in branches_data if branch["contact_email"] == email
    )
    assert principal_branch["contact_phone"] in {
        "(987) 654-3210",
        "+919876543210",
        "098765 43210",
    }
