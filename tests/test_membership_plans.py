import pytest
import pytest_asyncio
import uuid
from httpx import AsyncClient, ASGITransport
import asyncio
from datetime import datetime, timezone, timedelta

from app.main import app
from app.core.database import update_session_context
from app.core.security import create_access_token
from app.models.organization import Organization
from app.models.auth import Owner
from app.models.org_branch import OrgBranch


async def _set_owner_context(session, *, owner_id, org_id, request_suffix: str) -> None:
    await update_session_context(
        session,
        principal_id=str(owner_id),
        principal_type="owner",
        org_id=str(org_id),
        role="owner",
        ip_address="127.0.0.1",
        user_agent="pytest-membership-plans",
        request_id=f"membership-plans-{request_suffix}-{uuid.uuid4()}",
    )


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def test_data(auth_db_session):
    """Create isolated membership-plan tenants without global teardown."""
    suffix = uuid.uuid4().hex[:10]

    org1_id = uuid.uuid4()
    org2_id = uuid.uuid4()
    org3_id = uuid.uuid4()
    owner1_id = uuid.uuid4()
    owner2_id = uuid.uuid4()
    owner3_id = uuid.uuid4()

    owner1_email = f"owner1+{suffix}@test.com"
    owner2_email = f"owner2+{suffix}@test.com"
    owner3_email = f"owner3+{suffix}@test.com"

    auth_db_session.add_all(
        [
            Organization(
                id=org1_id,
                name=f"Test Org 1 {suffix}",
                slug=f"TEST-ORG-1-{suffix}",
                max_branches=5,
                default_currency_code="INR",
            ),
            Organization(
                id=org2_id,
                name=f"Test Org 2 {suffix}",
                slug=f"TEST-ORG-2-{suffix}",
                max_branches=5,
                default_currency_code="USD",
            ),
            Organization(
                id=org3_id,
                name=f"Fallback Currency Org {suffix}",
                slug=f"FALLBACK-CURRENCY-{suffix}",
                max_branches=5,
            ),
        ]
    )
    # Organization/Owner ORM mappings do not encode their insert dependency.
    # Keep PostgreSQL's FK as the authority while ordering this unit of work.
    await auth_db_session.flush()

    auth_db_session.add_all(
        [
            Owner(
                id=owner1_id,
                org_id=org1_id,
                owner_name="Org Owner 1",
                email=owner1_email,
                hashed_password="hash",
                email_verified=True,
            ),
            Owner(
                id=owner2_id,
                org_id=org2_id,
                owner_name="Org Owner 2",
                email=owner2_email,
                hashed_password="hash",
                email_verified=True,
            ),
            Owner(
                id=owner3_id,
                org_id=org3_id,
                owner_name="Fallback Owner",
                email=owner3_email,
                hashed_password="hash",
                email_verified=True,
            ),
        ]
    )
    await auth_db_session.commit()

    branch1_id = uuid.uuid4()
    await _set_owner_context(
        auth_db_session,
        owner_id=owner1_id,
        org_id=org1_id,
        request_suffix=suffix,
    )
    auth_db_session.add(
        OrgBranch(
            id=branch1_id,
            org_id=org1_id,
            branch_name="Branch 1",
            branch_code="BR1",
            internal_slug=f"branch-1-{suffix}",
            created_by=owner1_id,
        )
    )
    await auth_db_session.commit()

    return {
        "org1_id": org1_id,
        "owner1_id": owner1_id,
        "owner1_email": owner1_email,
        "branch1_id": branch1_id,
        "org2_id": org2_id,
        "owner2_id": owner2_id,
        "owner2_email": owner2_email,
        "org3_id": org3_id,
        "owner3_id": owner3_id,
        "owner3_email": owner3_email,
    }


def get_headers(owner_id, org_id, email="owner@test.com"):
    token = create_access_token(str(owner_id), str(org_id), email, role="owner")
    return {
        "Authorization": f"Bearer {token}",
        "X-Request-ID": str(uuid.uuid4()),
        "X-Forwarded-For": "127.0.0.1"
    }


async def create_plan(client, headers, **overrides):
    payload = {
        "name": "Monthly Access",
        "price": 1500,
        "duration_value": 1,
        "duration_unit": "months",
        "max_members": 1,
    }
    payload.update(overrides)
    return await client.post("/membership-plans", json=payload, headers=headers)


@pytest.mark.asyncio
async def test_create_realistic_plan_styles_and_limited_time_offer(client, test_data):
    headers1 = get_headers(test_data["owner1_id"], test_data["org1_id"], test_data["owner1_email"])

    monthly = await create_plan(client, headers1)
    assert monthly.status_code == 201
    monthly_plan = monthly.json()
    assert monthly_plan["name"] == "Monthly Access"
    assert monthly_plan["price"] == 1500
    assert monthly_plan["duration_value"] == 1
    assert monthly_plan["duration_unit"] == "months"
    assert monthly_plan["max_members"] == 1
    assert monthly_plan["branch_id"] is None
    assert monthly_plan["currency"] == "INR"
    assert monthly_plan["plan_code"].endswith("-001")

    couple = await create_plan(
        client,
        headers1,
        name="Couple Offer",
        price=2500,
        max_members=2,
    )
    assert couple.status_code == 201
    couple_plan = couple.json()
    assert couple_plan["name"] == "Couple Offer"
    assert couple_plan["price"] == 2500
    assert couple_plan["max_members"] == 2
    assert couple_plan["plan_code"].endswith("-002")

    family = await create_plan(
        client,
        headers1,
        name="Family Pack",
        price=4000,
        duration_value=3,
        max_members=4,
    )
    assert family.status_code == 201
    family_plan = family.json()
    assert family_plan["name"] == "Family Pack"
    assert family_plan["price"] == 4000
    assert family_plan["duration_value"] == 3
    assert family_plan["max_members"] == 4
    assert family_plan["plan_code"].endswith("-003")

    valid_from = datetime.now(timezone.utc)
    valid_until = valid_from + timedelta(days=14)
    limited = await create_plan(
        client,
        headers1,
        name="Limited Time Offer",
        price=999,
        valid_from=valid_from.isoformat(),
        valid_until=valid_until.isoformat(),
    )
    assert limited.status_code == 201
    limited_plan = limited.json()
    assert limited_plan["valid_from"] is not None
    assert limited_plan["valid_until"] is not None


@pytest.mark.asyncio
async def test_validation_boundaries_and_zero_price_policy(client, test_data):
    headers1 = get_headers(test_data["owner1_id"], test_data["org1_id"], test_data["owner1_email"])

    bad_dates = await create_plan(
        client,
        headers1,
        name="Bad Dates",
        price=10,
        valid_from="2026-01-02T00:00:00Z",
        valid_until="2026-01-01T00:00:00Z",
    )
    assert bad_dates.status_code == 422

    bad_price = await create_plan(client, headers1, name="Bad Price", price=-10)
    assert bad_price.status_code == 422

    zero_price = await create_plan(client, headers1, name="Free Trial Plan", price=0)
    assert zero_price.status_code == 201
    assert zero_price.json()["price"] == 0

    bad_duration = await create_plan(client, headers1, name="Bad Duration", duration_value=0)
    assert bad_duration.status_code == 422

    bad_max_members = await create_plan(client, headers1, name="Bad Max Members", max_members=0)
    assert bad_max_members.status_code == 422


@pytest.mark.asyncio
async def test_plan_code_and_currency_are_server_managed(client, test_data):
    headers1 = get_headers(test_data["owner1_id"], test_data["org1_id"], test_data["owner1_email"])
    headers2 = get_headers(test_data["owner2_id"], test_data["org2_id"], test_data["owner2_email"])
    headers3 = get_headers(test_data["owner3_id"], test_data["org3_id"], test_data["owner3_email"])

    create_res = await create_plan(
        client,
        headers1,
        name="Payload Tamper",
        plan_code="HACK-999",
        currency="USD",
    )
    assert create_res.status_code == 201
    plan = create_res.json()
    assert plan["plan_code"].startswith("TESTOR-")
    assert plan["plan_code"].endswith("-001")
    assert plan["currency"] == "INR"

    update_res = await client.patch(
        f"/membership-plans/{plan['id']}",
        json={
            "name": "Payload Tamper Updated",
            "price": 1800,
            "plan_code": "HACK-UPDATED",
            "currency": "EUR",
        },
        headers=headers1,
    )
    assert update_res.status_code == 200
    updated = update_res.json()
    assert updated["name"] == "Payload Tamper Updated"
    assert updated["price"] == 1800
    assert updated["plan_code"] == plan["plan_code"]
    assert updated["currency"] == "INR"

    org2_res = await create_plan(client, headers2, name="Org 2 Plan", price=100)
    assert org2_res.status_code == 201
    assert org2_res.json()["plan_code"].endswith("-001")
    assert org2_res.json()["currency"] == "USD"

    fallback_res = await create_plan(client, headers3, name="Fallback Currency Plan", price=100)
    assert fallback_res.status_code == 201
    assert fallback_res.json()["currency"] == "INR"


@pytest.mark.asyncio
async def test_branch_specific_plans_and_org_isolation(client, test_data):
    headers1 = get_headers(test_data["owner1_id"], test_data["org1_id"], test_data["owner1_email"])
    headers2 = get_headers(test_data["owner2_id"], test_data["org2_id"], test_data["owner2_email"])
    branch1_id = test_data["branch1_id"]

    org_wide = await create_plan(client, headers1, name="Org Wide Plan")
    assert org_wide.status_code == 201
    assert org_wide.json()["branch_id"] is None

    branch_specific = await create_plan(
        client,
        headers1,
        name="Branch Specific Plan",
        price=500,
        branch_id=str(branch1_id),
    )
    assert branch_specific.status_code == 201
    assert branch_specific.json()["branch_id"] == str(branch1_id)

    wrong_org = await create_plan(
        client,
        headers2,
        name="Bad Branch",
        price=10,
        branch_id=str(branch1_id),
    )
    assert wrong_org.status_code == 400
    assert "Branch not found" in wrong_org.json()["detail"]

    filtered = await client.get(f"/membership-plans?branch_id={branch1_id}", headers=headers1)
    assert filtered.status_code == 200
    assert [plan["name"] for plan in filtered.json()] == ["Branch Specific Plan"]

    isolated = await client.get(f"/membership-plans/{org_wide.json()['id']}", headers=headers2)
    assert isolated.status_code == 404


@pytest.mark.asyncio
async def test_status_lifecycle_listing_and_archived_guards(client, test_data):
    headers1 = get_headers(test_data["owner1_id"], test_data["org1_id"], test_data["owner1_email"])

    active_res = await create_plan(client, headers1, name="Active Lifecycle Plan")
    inactive_res = await create_plan(client, headers1, name="Inactive Lifecycle Plan")
    archived_from_active_res = await create_plan(client, headers1, name="Archive From Active")
    archived_from_inactive_res = await create_plan(client, headers1, name="Archive From Inactive")
    assert active_res.status_code == inactive_res.status_code == archived_from_active_res.status_code == archived_from_inactive_res.status_code == 201

    active_plan = active_res.json()
    inactive_plan = inactive_res.json()
    archived_from_active = archived_from_active_res.json()
    archived_from_inactive = archived_from_inactive_res.json()

    deactivate_res = await client.post(f"/membership-plans/{inactive_plan['id']}/deactivate", headers=headers1)
    assert deactivate_res.status_code == 200
    assert deactivate_res.json()["status"] == "inactive"

    activate_res = await client.post(f"/membership-plans/{inactive_plan['id']}/activate", headers=headers1)
    assert activate_res.status_code == 200
    assert activate_res.json()["status"] == "active"

    deactivate_again = await client.post(f"/membership-plans/{inactive_plan['id']}/deactivate", headers=headers1)
    assert deactivate_again.status_code == 200
    assert deactivate_again.json()["status"] == "inactive"

    active_archive_res = await client.post(f"/membership-plans/{archived_from_active['id']}/archive", headers=headers1)
    assert active_archive_res.status_code == 200
    assert active_archive_res.json()["status"] == "archived"
    assert active_archive_res.json()["archived_at"] is not None

    await client.post(f"/membership-plans/{archived_from_inactive['id']}/deactivate", headers=headers1)
    inactive_archive_res = await client.post(f"/membership-plans/{archived_from_inactive['id']}/archive", headers=headers1)
    assert inactive_archive_res.status_code == 200
    assert inactive_archive_res.json()["status"] == "archived"

    active_list = await client.get("/membership-plans?plan_status=active", headers=headers1)
    assert active_list.status_code == 200
    assert [plan["name"] for plan in active_list.json()] == ["Active Lifecycle Plan"]

    inactive_list = await client.get("/membership-plans?plan_status=inactive", headers=headers1)
    assert inactive_list.status_code == 200
    assert [plan["name"] for plan in inactive_list.json()] == ["Inactive Lifecycle Plan"]

    archived_list = await client.get("/membership-plans?plan_status=archived", headers=headers1)
    assert archived_list.status_code == 200
    archived_names = {plan["name"] for plan in archived_list.json()}
    assert archived_names == {"Archive From Active", "Archive From Inactive"}

    archived_id = archived_from_active["id"]
    reactivate_archived = await client.post(f"/membership-plans/{archived_id}/activate", headers=headers1)
    assert reactivate_archived.status_code == 400
    assert "Cannot reactivate archived plan" in reactivate_archived.json()["detail"]

    edit_archived = await client.patch(
        f"/membership-plans/{archived_id}",
        json={"name": "Edited Archived Plan"},
        headers=headers1,
    )
    assert edit_archived.status_code == 400
    assert "Cannot update archived plans" in edit_archived.json()["detail"]

    deactivate_archived = await client.post(f"/membership-plans/{archived_id}/deactivate", headers=headers1)
    assert deactivate_archived.status_code == 400


@pytest.mark.asyncio
async def test_concurrent_plan_code_generation_is_unique_and_sequential(client, test_data):
    org1_id = test_data["org1_id"]
    owner1_id = test_data["owner1_id"]

    headers1 = get_headers(owner1_id, org1_id, test_data["owner1_email"])

    async def create_plan(i):
        return await client.post("/membership-plans", json={
            "name": f"Concurrent {i}",
            "price": 50,
            "duration_value": 1,
            "duration_unit": "months"
        }, headers=headers1)

    tasks = [create_plan(i) for i in range(10)]
    responses = await asyncio.gather(*tasks)

    codes = set()
    for response in responses:
        assert response.status_code == 201
        codes.add(response.json()["plan_code"])

    assert len(codes) == 10
    assert codes == {f"TESTOR-{i:03d}" for i in range(1, 11)}
