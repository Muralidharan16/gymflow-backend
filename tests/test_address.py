"""Address regressions with P2D geocoding boundary updates.

The historical address suite is loaded unchanged from address_regression_baseline.py.
Only tests whose contract was intentionally changed by P2D are redefined below;
all other regression functions remain part of this collected module.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from celery.exceptions import MaxRetriesExceededError
from sqlalchemy import select, text

_BASELINE_PATH = pathlib.Path(__file__).with_name("address_regression_baseline.py")
_SPEC = importlib.util.spec_from_file_location("_doers_address_regression_baseline", _BASELINE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("Unable to load address regression baseline")
_BASELINE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _BASELINE
_SPEC.loader.exec_module(_BASELINE)

for _name, _value in vars(_BASELINE).items():
    if _name.startswith("test_"):
        globals()[_name] = _value

from app.models.address import AddressAuditLog, OrganizationAddress
from app.tasks.geocoding import geocode_address_task


def _worker_geocode_row(
    *,
    address_id: uuid.UUID,
    org_id: uuid.UUID,
    line1: str = "FAIL",
) -> dict[str, object]:
    return {
        "id": address_id,
        "org_id": org_id,
        "address_line1": line1,
        "city": "Chennai",
        "state_province": "TN",
        "postal_code": "600002",
        "country_code": "IN",
        "formatted_address": None,
        "google_place_id": None,
        "validation_status": "pending",
        "geocode_attempts": 0,
        "next_retry_at": None,
    }


def _worker_session_factory_mock() -> tuple[MagicMock, MagicMock]:
    factory = MagicMock()
    db = MagicMock()
    factory.return_value.__enter__.return_value = db
    factory.return_value.__exit__.return_value = False
    return factory, db


def test_listener_address_field_changed() -> None:
    address = OrganizationAddress(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        address_line1="12 Anna Salai",
        city="Chennai",
        state_province="Tamil Nadu",
        postal_code="600002",
        country_code="IN",
        is_verified=True,
        formatted_address="12 Anna Salai, Chennai, TN, 600002, IN",
    )

    with patch("app.tasks.geocoding.geocode_address_task.delay") as mock_task:
        from app.models.address import receive_after_update

        connection = MagicMock()
        receive_after_update(None, connection, address)

    assert connection.execute.call_count == 2
    mock_task.assert_called_once_with(str(address.id), str(address.org_id))


def test_geocoding_retry_on_api_failure() -> None:
    address_id = uuid.uuid4()
    org_id = uuid.uuid4()
    factory, _ = _worker_session_factory_mock()

    with patch("app.core.database.WorkerSyncSessionLocal", factory), \
         patch(
             "app.tasks.geocoding._load_geocoding_input_sync",
             return_value=_worker_geocode_row(address_id=address_id, org_id=org_id),
         ), \
         patch("app.tasks.geocoding.geocode_address_task.retry") as mock_retry:
        geocode_address_task.run(str(address_id), str(org_id))

    mock_retry.assert_called_once()


def test_geocoding_retries_three_times_on_api_failure() -> None:
    address_id = uuid.uuid4()
    org_id = uuid.uuid4()
    factory, _ = _worker_session_factory_mock()
    request = MagicMock()
    request.retries = 2

    with patch("app.core.database.WorkerSyncSessionLocal", factory), \
         patch(
             "app.tasks.geocoding._load_geocoding_input_sync",
             return_value=_worker_geocode_row(address_id=address_id, org_id=org_id),
         ), \
         patch("celery.app.task.Task.request", new_callable=PropertyMock, return_value=request), \
         patch("app.tasks.geocoding.geocode_address_task.retry") as mock_retry:
        geocode_address_task.run(str(address_id), str(org_id))

    assert mock_retry.call_count == 1
    assert mock_retry.call_args.kwargs["countdown"] == 240


def test_geocoding_sets_failed_flag_after_max_retries() -> None:
    address_id = uuid.uuid4()
    org_id = uuid.uuid4()
    factory, _ = _worker_session_factory_mock()
    request = MagicMock()
    request.retries = 3

    with patch("app.core.database.WorkerSyncSessionLocal", factory), \
         patch(
             "app.tasks.geocoding._load_geocoding_input_sync",
             return_value=_worker_geocode_row(address_id=address_id, org_id=org_id),
         ), \
         patch("celery.app.task.Task.request", new_callable=PropertyMock, return_value=request), \
         patch(
             "app.tasks.geocoding.geocode_address_task.retry",
             side_effect=MaxRetriesExceededError("Max retries exceeded"),
         ), \
         patch("app.tasks.geocoding._mark_failure_sync") as mark_failure:
        with pytest.raises(MaxRetriesExceededError):
            geocode_address_task.run(str(address_id), str(org_id))

    kwargs = mark_failure.call_args.kwargs
    assert kwargs["address_id"] == address_id
    assert kwargs["org_id"] == org_id
    assert kwargs["permanent"] is True
    assert kwargs["retry_count"] == 4


def test_geocoding_creates_notification_after_max_retries() -> None:
    address_id = uuid.uuid4()
    org_id = uuid.uuid4()
    factory, _ = _worker_session_factory_mock()
    request = MagicMock()
    request.retries = 3

    with patch("app.core.database.WorkerSyncSessionLocal", factory), \
         patch(
             "app.tasks.geocoding._load_geocoding_input_sync",
             return_value=_worker_geocode_row(address_id=address_id, org_id=org_id),
         ), \
         patch("celery.app.task.Task.request", new_callable=PropertyMock, return_value=request), \
         patch(
             "app.tasks.geocoding.geocode_address_task.retry",
             side_effect=MaxRetriesExceededError("Max retries exceeded"),
         ), \
         patch("app.tasks.geocoding._mark_failure_sync") as mark_failure:
        with pytest.raises(MaxRetriesExceededError):
            geocode_address_task.run(str(address_id), str(org_id))

    assert mark_failure.call_count == 1
    assert "review and re-save" in mark_failure.call_args.kwargs[
        "notification_message"
    ].lower()


@pytest.mark.asyncio
async def test_audit_log_captured_on_update(admin_db_session, auth_db_session) -> None:
    """
    Validates that typed actor provenance and tenant context survive transaction
    boundaries while address triggers record immutable audit snapshots.

    Organization and actor creation are administrative root-fixture setup.
    Branch/state creation uses the dedicated bounded auth/bootstrap identity,
    matching production onboarding rather than borrowing migration-owner power.
    Address mutation remains on the reduced application runtime identity. Direct
    audit-table reads remain forbidden to app_runtime; immutable audit evidence is
    verified through the reduced migration/admin session under the same tenant RLS
    context.
    """
    from app.core.database import AsyncSessionLocal, update_session_context
    from app.models.organization import Organization
    from app.models.org_branch import OrgBranch, OrgBranchState
    from app.models.staff import GymOwner

    org_id = uuid.uuid4()
    branch_id = uuid.uuid4()
    owner_id = uuid.uuid4()

    admin_db_session.add(Organization(id=org_id, name="Test Gym Org"))
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

    await update_session_context(
        auth_db_session,
        principal_id=str(owner_id),
        principal_type="legacy_gym_owner",
        org_id=str(org_id),
        role="owner",
        ip_address="127.0.0.1",
        user_agent="pytest-auth-bootstrap-fixture",
        request_id=str(uuid.uuid4()),
    )

    branch = OrgBranch(
        id=branch_id,
        org_id=org_id,
        branch_name="Anna Nagar Main",
        branch_code="AN01",
        internal_slug="anna-nagar-main",
        timezone="UTC",
        currency_code="USD"
    )
    auth_db_session.add(branch)
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
    auth_db_session.add(branch_state)
    await auth_db_session.commit()

    async with AsyncSessionLocal() as db:
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

        addr.address_line1 = "enc:New Address Road"
        await db.commit()

        runtime_can_read_audit = (
            await db.execute(
                text(
                    "SELECT pg_catalog.has_table_privilege("
                    "current_user, 'public.branch_address_audit_log', 'SELECT')"
                )
            )
        ).scalar_one()
        assert runtime_can_read_audit is False

    await update_session_context(
        admin_db_session,
        principal_id=str(owner_id),
        principal_type="legacy_gym_owner",
        org_id=str(org_id),
        role="owner",
        ip_address="127.0.0.1",
        user_agent="pytest-admin-audit-verifier",
        request_id=str(uuid.uuid4()),
    )
    stmt = select(AddressAuditLog).where(AddressAuditLog.org_id == org_id)
    res = await admin_db_session.execute(stmt)
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
    assert str(logs[0].changed_by) == str(owner_id)
    assert logs[0].changed_by_type == "legacy_gym_owner"
    assert str(logs[0].ip_address) == "192.168.1.50"
