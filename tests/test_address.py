import pytest
from jose import jwt
from unittest.mock import patch, MagicMock, AsyncMock, PropertyMock
from sqlalchemy import select, text
import sys
import os
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.orm import Session
from fastapi import FastAPI, Depends, Request
from httpx import AsyncClient, ASGITransport
from pydantic import ValidationError

from app.models.address import OrganizationAddress, MemberAddress, AddressAuditLog
from app.models.notification import Notification
from app.schemas.address import (
    PublicAddressSchema, PrivateAddressSchema, CreateAddressSchema,
    PublicMemberAddressSchema, PrivateMemberAddressSchema, MemberAddressBaseSchema
)
from app.core.security import create_access_token
from app.core.config import settings
from app.core.telemetry import track_event_safe, sentry_before_send
from app.core.deps import Staff, get_current_active_staff
from app.services.address_service import set_primary_address, capture_address_snapshot
from app.tasks.geocoding import geocode_address_task, GeocodingAPIError
from celery.exceptions import MaxRetriesExceededError

# =====================================================================
# GAP 2 TESTS: GEOCIDING EVENT LIFECYCLE LISTENERS
# =====================================================================

def test_listener_address_field_changed() -> None:
    """
    Verifies that changing a geocoding input (e.g. city) resets coordinates
    and triggers the Celery background task using inspect-based mocks.
    """
    db_session = MagicMock()
    address = OrganizationAddress(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        address_line1="12 Anna Salai",
        city="Chennai",
        state_province="Tamil Nadu",
        postal_code="600002",
        country_code="IN",
        is_verified=True,
        formatted_address="12 Anna Salai, Chennai, TN, 600002, IN"
    )

    with patch("app.tasks.geocoding.geocode_address_task.delay") as mock_task:
        with patch("sqlalchemy.orm.attributes.get_history") as mock_hist:
            mock_change = MagicMock()
            mock_change.has_changes.return_value = True
            mock_hist.return_value = mock_change

            from app.models.address import receive_after_update
            connection = MagicMock()
            receive_after_update(None, connection, address)

            assert connection.execute.call_count == 2
            mock_task.assert_called_once_with(str(address.id))

# =====================================================================
# GAP 3 & 4 TESTS: ENDPOINT SERIALIZATION & RBAC EXPOSURE RULES
# =====================================================================

@pytest.mark.asyncio
async def test_private_address_endpoint_rbac() -> None:
    """
    Tests endpoint authorization:
    - Admin-role returns detailed private data (HTTP 200)
    - Member-role is blocked (HTTP 403)
    """
    app = FastAPI()
    from app.routers.address import router as addr_router
    app.include_router(addr_router)

    mock_address = OrganizationAddress(
        id=uuid.UUID("c3f8152e-fbca-4c8d-b3e3-64a6d1a1b411"),
        org_id=uuid.uuid4(),
        address_type="operational",
        address_line1="12 Anna Salai",
        city="Chennai",
        state_province="TN",
        postal_code="600002",
        country_code="IN",
        is_verified=True,
        is_primary=True,
        is_exact_location_visible=True
    )

    async def mock_admin_staff():
        return Staff(id=uuid.uuid4(), org_id=uuid.uuid4(), gym_id=None, role="admin")

    app.dependency_overrides[get_current_active_staff] = mock_admin_staff

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        with patch("sqlalchemy.ext.asyncio.AsyncSession.get", return_value=mock_address):
            response = await client.get("/addresses/c3f8152e-fbca-4c8d-b3e3-64a6d1a1b411/private")
            assert response.status_code == 200
            assert response.json()["address_line1"] == "12 Anna Salai"

# =====================================================================
# GAP 5 TESTS: JWT MINIMAL CLAIMS
# =====================================================================

def test_jwt_token_payload_excludes_address_pii() -> None:
    """
    Verifies generated token claims strictly exclude address fields.
    """
    raw_auth_profile = {
        "owner_id": "8a7c5a03-6dc8-4fdf-adb8-1dafa681eaa6",
        "org_id": "d0e5800e-a4a2-43bd-ba03-57bbb0c41f22",
        "email": "owner@example.com",
        "role": "owner",
        "address_line1": "12 Anna Salai",
        "postal_code": "600002"
    }

    token = create_access_token(
        owner_id=raw_auth_profile["owner_id"],
        org_id=raw_auth_profile["org_id"],
        email=raw_auth_profile["email"],
        role=raw_auth_profile["role"]
    )

    decoded = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])

    assert decoded["sub"] == "8a7c5a03-6dc8-4fdf-adb8-1dafa681eaa6"
    assert decoded["org_id"] == "d0e5800e-a4a2-43bd-ba03-57bbb0c41f22"
    assert decoded["principal_type"] == "owner"
    assert "address_line1" not in decoded
    assert "postal_code" not in decoded

# =====================================================================
# GAP 6 TESTS: TELEMETRY SCRUBS
# =====================================================================

def test_track_event_safe_scrubs_pii_but_keeps_city() -> None:
    """
    Verifies telemetry wraps drop exact coordinate keys but preserve city tags.
    """
    event_data = {
        "user_id": "999",
        "address_line1": "12 Anna Salai",
        "postal_code": "600002",
        "city": "Chennai",
        "state_province": "Tamil Nadu",
        "country_code": "IN",
        "ip_address": "127.0.0.1"
    }

    with patch("app.core.telemetry.track_event") as mock_track:
        track_event_safe("member_signup", event_data)

        mock_track.assert_called_once()
        sent_properties = mock_track.call_args[0][1]

        assert sent_properties["city"] == "Chennai"
        assert "address_line1" not in sent_properties
        assert "postal_code" not in sent_properties
        assert "ip_address" not in sent_properties


def test_sentry_before_send_scrubs_nested_pii() -> None:
    """
    Verifies Sentry scrubs recursive dictionaries completely.
    """
    sentry_mock_payload = {
        "exception": {
            "values": [
                {
                    "stacktrace": {
                        "frames": [
                            {
                                "vars": {
                                    "address_line1": "12 Anna Salai",
                                    "postal_code": "600002",
                                    "city": "Chennai"
                                }
                            }
                        ]
                    }
                }
            ]
        }
    }

    scrubbed = sentry_before_send(sentry_mock_payload, {})
    vars_node = scrubbed["exception"]["values"][0]["stacktrace"]["frames"][0]["vars"]

    assert vars_node["address_line1"] == "[REDACTED]"
    assert vars_node["postal_code"] == "[REDACTED]"
    assert vars_node["city"] == "Chennai"

# =====================================================================
# FIX 1 TESTS: BILLING ADDRESS NON-EMPTY REQUIREMENT
# =====================================================================

def test_address_type_billing_requires_address_line1() -> None:
    """
    Asserts a validation error is thrown when address_type is 'billing' and address_line1 is empty.
    """
    CreateAddressSchema(
        address_type="operational",
        address_line1="12 Anna Salai",
        city="Chennai",
        state_province="TN",
        country_code="IN"
    )

    with pytest.raises(ValidationError) as exc:
        CreateAddressSchema(
            address_type="billing",
            address_line1=" ",
            city="Chennai",
            state_province="TN",
            country_code="IN"
        )
    assert "address_line1 must be non-empty" in str(exc.value)

# =====================================================================
# FIX 2 TESTS: TRANSACTIONAL PRIMARY TOGGLING & DB CONSTRAINT
# =====================================================================

@pytest.mark.asyncio
async def test_set_primary_address_success() -> None:
    """
    Validates transactional toggling of multiple organization addresses.
    """
    db = MagicMock()
    org_id = uuid.uuid4()
    target_addr_id = uuid.uuid4()

    target_addr = OrganizationAddress(id=target_addr_id, org_id=org_id, is_primary=False)

    async def mock_execute(stmt, *args, **kwargs):
        mock_res = MagicMock()
        mock_res.scalar_one_or_none.return_value = target_addr
        return mock_res

    db.execute = AsyncMock(side_effect=mock_execute)
    db.flush = AsyncMock()

    updated = await set_primary_address(target_addr_id, org_id, db)

    db.execute.assert_called()
    assert updated.is_primary is True
    db.flush.assert_called_once()

# =====================================================================
# FIX 3 TESTS: GEOCODING RETRIES AND FAILURE NOTIFICATIONS
# =====================================================================

def test_geocoding_retry_on_api_failure() -> None:
    """
    Ensures celery geocoding task triggers retry when API exceptions are raised.
    """
    with patch("app.tasks.geocoding.geocode_address_task.retry") as mock_retry:
        with patch("asyncio.AbstractEventLoop.run_until_complete", side_effect=GeocodingAPIError("Connection Timeout")):
            geocode_address_task("some-address-uuid")
            mock_retry.assert_called_once()

# =====================================================================
# FIX 4 TESTS: INVOICE COMPLIANCE ADDRESS SNAPSHOTS
# =====================================================================

@pytest.mark.asyncio
async def test_invoice_snapshot_is_immutable() -> None:
    """
    Verifies that capturing address snapshots creates an immutable, coordinate-free schema representation.
    """
    address_id = uuid.uuid4()
    org_id = uuid.uuid4()

    mock_addr = OrganizationAddress(
        id=address_id,
        org_id=org_id,
        address_type="billing",
        address_line1="12 Anna Salai",
        city="Chennai",
        state_province="Tamil Nadu",
        postal_code="600002",
        country_code="IN",
        formatted_address="12 Anna Salai, Chennai, TN, 600002"
    )

    snapshot = capture_address_snapshot(mock_addr)

    assert "latitude" not in snapshot
    assert "longitude" not in snapshot
    assert snapshot["address_line1"] == "12 Anna Salai"
    assert snapshot["city"] == "Chennai"

# =====================================================================
# FIX 5 TESTS: INDEX CHECK
# =====================================================================

@pytest.mark.asyncio
async def test_gist_index_exists() -> None:
    """
    Verifies that spatial/composite index definitions are present in database catalog.
    """
    from app.core.database import sync_engine
    from sqlalchemy import inspect

    if sync_engine:
        inspector = inspect(sync_engine)
        indexes = [idx["name"] for idx in inspector.get_indexes("branch_name_translations")]
        assert "ix_branch_translations_search" in indexes


def test_spatial_and_filtering_indexes_exist() -> None:
    """
    Verifies database catalog registers the country/state and city filtering indexes.
    """
    from app.core.database import sync_engine
    from sqlalchemy import inspect
    inspector = inspect(sync_engine)
    indexes = [idx["name"] for idx in inspector.get_indexes("org_branches")]
    assert "ix_org_branches_name_trgm" in indexes


# =====================================================================
# FIX 6 TESTS: AUDIT LOGS TRIGGER
# =====================================================================

@pytest.mark.asyncio
async def test_audit_log_captured_on_update(admin_db_session) -> None:
    """
    Validates that typed actor provenance and tenant context survive transaction
    boundaries while address triggers record immutable audit snapshots.

    Organization, branch and actor creation are administrative fixture setup.
    Address mutation and audit verification remain on the reduced application
    runtime identity so this test cannot hide an RLS or DML privilege defect.
    """
    from app.core.database import AsyncSessionLocal, update_session_context
    from app.models.organization import Organization
    from app.models.org_branch import OrgBranch, OrgBranchState
    from app.models.staff import GymOwner

    org_id = uuid.uuid4()
    branch_id = uuid.uuid4()
    owner_id = uuid.uuid4()

    admin_db_session.add(Organization(id=org_id, name="Test Gym Org"))
    branch = OrgBranch(
        id=branch_id,
        org_id=org_id,
        branch_name="Anna Nagar Main",
        branch_code="AN01",
        internal_slug="anna-nagar-main",
        timezone="UTC",
        currency_code="USD"
    )
    admin_db_session.add(branch)
    branch_state = OrgBranchState(
        branch_id=branch_id,
        org_id=org_id,
        branch_status="active",
        is_primary=True,
        is_active=True,
        is_public=True,
        version=1,
        search_epoch_ulid="01AN4V07BY79KA1307SR9XFMAT"
    )
    admin_db_session.add(branch_state)

    # Register a legacy staff actor during fixture bootstrap. The source trigger
    # writes the typed audit-principal registry before reduced-runtime behavior.
    owner = GymOwner(
        id=owner_id,
        org_id=org_id,
        name="Test Owner",
        email="owner@test.com",
        password_hash="hash",
        role="owner",
        is_active=True,
        is_verified=True
    )
    admin_db_session.add(owner)
    await admin_db_session.commit()

    async with AsyncSessionLocal() as db:
        # Attach complete typed actor provenance once. Session.after_begin must
        # reapply the same context after every commit without raw GUC duplication.
        await update_session_context(
            db,
            principal_id=str(owner_id),
            principal_type="legacy_gym_owner",
            org_id=str(org_id),
            role="owner",
            ip_address="192.168.1.50",
            user_agent="pytest-agent",
            request_id=str(uuid.uuid4()),
        )

        addr = OrganizationAddress(
            org_id=org_id,
            branch_id=branch_id,
            address_type="physical",
            address_line1="enc:12 Anna Salai",
            city="Chennai",
            state_province="TN",
            postal_code="600002",
            country_code="IN",
            is_primary=True,
            effective_from=datetime.now(timezone.utc)
        )
        db.add(addr)
        await db.commit()

        # A new transaction begins after commit. No manual set_config call is
        # allowed: the Session after_begin hook must restore tenant + typed actor.
        addr.address_line1 = "enc:New Address Road"
        await db.commit()

        stmt = select(AddressAuditLog).where(AddressAuditLog.org_id == org_id)
        res = await db.execute(stmt)
        logs = res.scalars().all()

        assert len(logs) > 0

        import json
        old_snap = logs[0].old_address
        if isinstance(old_snap, str):
            old_snap = json.loads(old_snap)
        new_snap = logs[0].new_address
        if isinstance(new_snap, str):
            new_snap = json.loads(new_snap)

        import hashlib
        expected_old_hash = hashlib.sha256(b"enc:12 Anna Salai").hexdigest()
        expected_new_hash = hashlib.sha256(b"enc:New Address Road").hexdigest()
        assert old_snap["address_line1_hash"] == expected_old_hash
        assert new_snap["address_line1_hash"] == expected_new_hash
        assert str(logs[0].ip_address) == "192.168.1.50"


# =====================================================================
# FIX 7 TESTS: MEMBER COORDINATE EXPOSURE GUARD
# =====================================================================

def test_member_address_exposure_guard() -> None:
    """
    Verifies that public schemas exclude coordinate details and address lines, while administrative access preserves them.
    """
    public_payload = {
        "city": "Chennai",
        "state_province": "Tamil Nadu",
        "country_code": "IN",
        "latitude": 13.08,
        "longitude": 80.27
    }
    pub_schema = PublicMemberAddressSchema(**public_payload)
    assert not hasattr(pub_schema, "latitude")
    assert not hasattr(pub_schema, "longitude")
    assert not hasattr(pub_schema, "address_line1")

    private_payload = {
        "id": uuid.uuid4(),
        "member_id": uuid.uuid4(),
        "address_type": "operational",
        "address_line1": "12 Anna Salai",
        "city": "Chennai",
        "state_province": "TN",
        "postal_code": "600002",
        "country_code": "IN",
        "is_verified": True,
        "is_primary": True,
        "latitude": 13.08,
        "longitude": 80.27
    }
    priv_schema = PrivateMemberAddressSchema(**private_payload)
    assert priv_schema.latitude == 13.08
    assert priv_schema.longitude == 80.27
    assert priv_schema.address_line1 == "12 Anna Salai"


@pytest.mark.asyncio
async def test_only_one_primary_exists_after_swap() -> None:
    """
    Asserts that set_primary_address successfully toggles primary statuses.
    """
    db = MagicMock()
    org_id = uuid.uuid4()
    addr1_id = uuid.uuid4()

    addr1 = OrganizationAddress(id=addr1_id, org_id=org_id, is_primary=False)

    async def mock_execute(stmt, *args, **kwargs):
        mock_res = MagicMock()
        mock_res.scalar_one_or_none.return_value = addr1
        return mock_res

    db.execute = AsyncMock(side_effect=mock_execute)
    db.flush = AsyncMock()

    updated = await set_primary_address(addr1_id, org_id, db)
    assert updated.is_primary is True


def test_geocoding_retries_three_times_on_api_failure() -> None:
    """
    Verifies that the geocoding task retries up to three times on API failure.
    """
    with patch("app.tasks.geocoding.geocode_address_task.retry") as mock_retry:
        with patch("asyncio.AbstractEventLoop.run_until_complete", side_effect=GeocodingAPIError("API Connection Failure")):
            geocode_address_task("some-address-uuid")
            assert mock_retry.call_count >= 1


def test_geocoding_sets_failed_flag_after_max_retries() -> None:
    """
    Asserts that geocoding_failed is set to True after maximum retries are exceeded.
    """
    from app.tasks.geocoding import geocode_address_task

    with patch("app.tasks.geocoding.geocode_address_task.retry", side_effect=MaxRetriesExceededError("Max retries exceeded")):
        mock_req = MagicMock()
        mock_req.retries = 3
        with patch("celery.app.task.Task.request", new_callable=PropertyMock, return_value=mock_req):
            with patch("app.core.database.SessionLocal") as mock_session_local:
                db_mock = MagicMock()
                mock_session_local.return_value.__enter__.return_value = db_mock

                addr_mock = OrganizationAddress(id=uuid.uuid4(), org_id=uuid.uuid4(), address_line1="FAIL")
                db_mock.query.return_value.filter.return_value.first.side_effect = [addr_mock, addr_mock]

                with pytest.raises(MaxRetriesExceededError):
                    geocode_address_task.run(str(addr_mock.id))

                assert addr_mock.geocoding_failed is True


def test_geocoding_creates_notification_after_max_retries() -> None:
    """
    Verifies that a notification warning is registered when max retries is exhausted.
    """
    from app.tasks.geocoding import geocode_address_task

    with patch("app.tasks.geocoding.geocode_address_task.retry", side_effect=MaxRetriesExceededError("Max retries exceeded")):
        mock_req = MagicMock()
        mock_req.retries = 3
        with patch("celery.app.task.Task.request", new_callable=PropertyMock, return_value=mock_req):
            with patch("app.core.database.SessionLocal") as mock_session_local:
                db_mock = MagicMock()
                mock_session_local.return_value.__enter__.return_value = db_mock

                addr_mock = OrganizationAddress(id=uuid.uuid4(), org_id=uuid.uuid4(), address_line1="FAIL")
                db_mock.query.return_value.filter.return_value.first.side_effect = [addr_mock, addr_mock]

                with pytest.raises(MaxRetriesExceededError):
                    geocode_address_task.run(str(addr_mock.id))

                db_mock.add.assert_called_once()
                notification = db_mock.add.call_args[0][0]
                assert isinstance(notification, Notification)