import asyncio
import uuid
from datetime import date

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.core.security import create_access_token
from app.main import app
from app.models.auth import Owner
from app.models.enums import MemberStatus
from app.models.member import Member
from app.models.membership_plan import DurationUnit, MembershipPlan, PlanStatus
from app.models.org_branch import OrgBranch
from app.models.organization import Organization
from app.utils.subscription_dates import calculate_subscription_end_date
from conftest import cleanup_test_database_tables


async def clear_subscription_v2_test_data():
    await cleanup_test_database_tables([
        "subscription_members",
        "member_subscriptions_v2",
        "members",
        "membership_plans",
        "organization_counters",
        "org_branch_state",
        "org_branches",
        "owners",
        "organizations",
    ])


@pytest_asyncio.fixture(autouse=True)
async def cleanup_database():
    await clear_subscription_v2_test_data()
    yield
    await clear_subscription_v2_test_data()


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


def get_headers(owner_id, org_id, email="owner@test.com"):
    token = create_access_token(str(owner_id), str(org_id), email, role="owner")
    return {
        "Authorization": f"Bearer {token}",
        "X-Request-ID": str(uuid.uuid4()),
        "X-Forwarded-For": "127.0.0.1",
    }


@pytest_asyncio.fixture
async def test_data():
    async with AsyncSessionLocal() as session:
        await session.execute(text("RESET ROLE"))

        org1_id = uuid.uuid4()
        org2_id = uuid.uuid4()
        org1 = Organization(id=org1_id, name="Test Org 1", slug="TEST-ORG-1", max_branches=5, default_currency_code="INR")
        org2 = Organization(id=org2_id, name="Test Org 2", slug="TEST-ORG-2", max_branches=5, default_currency_code="USD")
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

        member1_id = uuid.uuid4()
        member2_id = uuid.uuid4()
        inactive_member_id = uuid.uuid4()
        member_other_id = uuid.uuid4()
        members = [
            Member(id=member1_id, org_id=org1_id, member_uid="M001", name="Primary Member", phone="9876500001", status=MemberStatus.active, is_active=True),
            Member(id=member2_id, org_id=org1_id, member_uid="M002", name="Second Member", phone="9876500002", status=MemberStatus.active, is_active=True),
            Member(id=inactive_member_id, org_id=org1_id, member_uid="M003", name="Inactive Member", phone="9876500003", status=MemberStatus.inactive, is_active=True),
            Member(id=member_other_id, org_id=org2_id, member_uid="M004", name="Other Member", phone="9876500004", status=MemberStatus.active, is_active=True),
        ]
        session.add_all(members)
        await session.flush()

        org_wide_plan_id = uuid.uuid4()
        branch_plan_id = uuid.uuid4()
        inactive_plan_id = uuid.uuid4()
        archived_plan_id = uuid.uuid4()
        other_plan_id = uuid.uuid4()
        plans = [
            MembershipPlan(
                id=org_wide_plan_id,
                org_id=org1_id,
                plan_code="PLAN-001",
                name="Monthly Access",
                price=1500,
                currency="INR",
                duration_value=1,
                duration_unit=DurationUnit.months,
                max_members=1,
                status=PlanStatus.active,
            ),
            MembershipPlan(
                id=branch_plan_id,
                org_id=org1_id,
                branch_id=branch1_id,
                plan_code="PLAN-002",
                name="Family Pack",
                price=4000,
                currency="INR",
                duration_value=30,
                duration_unit=DurationUnit.days,
                max_members=4,
                status=PlanStatus.active,
            ),
            MembershipPlan(
                id=inactive_plan_id,
                org_id=org1_id,
                plan_code="PLAN-003",
                name="Inactive Plan",
                price=100,
                currency="INR",
                duration_value=1,
                duration_unit=DurationUnit.months,
                max_members=1,
                status=PlanStatus.inactive,
            ),
            MembershipPlan(
                id=archived_plan_id,
                org_id=org1_id,
                plan_code="PLAN-004",
                name="Archived Plan",
                price=100,
                currency="INR",
                duration_value=1,
                duration_unit=DurationUnit.months,
                max_members=1,
                status=PlanStatus.archived,
            ),
            MembershipPlan(
                id=other_plan_id,
                org_id=org2_id,
                plan_code="PLAN-005",
                name="Other Plan",
                price=100,
                currency="USD",
                duration_value=1,
                duration_unit=DurationUnit.months,
                max_members=1,
                status=PlanStatus.active,
            ),
        ]
        session.add_all(plans)
        await session.commit()

        return {
            "org1_id": org1_id,
            "org2_id": org2_id,
            "owner1_id": owner1_id,
            "owner2_id": owner2_id,
            "branch1_id": branch1_id,
            "branch2_id": branch2_id,
            "branch_other_id": branch_other_id,
            "member1_id": member1_id,
            "member2_id": member2_id,
            "inactive_member_id": inactive_member_id,
            "member_other_id": member_other_id,
            "org_wide_plan_id": org_wide_plan_id,
            "branch_plan_id": branch_plan_id,
            "inactive_plan_id": inactive_plan_id,
            "archived_plan_id": archived_plan_id,
            "other_plan_id": other_plan_id,
        }


async def create_subscription(client, test_data, **overrides):
    headers = get_headers(test_data["owner1_id"], test_data["org1_id"], "owner1@test.com")
    payload = {
        "branch_id": str(test_data["branch1_id"]),
        "membership_plan_id": str(test_data["org_wide_plan_id"]),
        "primary_member_id": str(test_data["member1_id"]),
        "start_date": "2026-06-11",
    }
    payload.update(overrides)
    return await client.post(f"/organizations/{test_data['org1_id']}/member-subscriptions", json=payload, headers=headers)


@pytest.mark.parametrize(
    "start,duration_value,duration_unit,expected",
    [
        (date(2026, 6, 11), 30, "days", date(2026, 7, 11)),
        (date(2026, 6, 11), 1, "months", date(2026, 7, 11)),
        (date(2026, 6, 11), 1, "years", date(2027, 6, 11)),
        (date(2026, 1, 31), 1, "months", date(2026, 2, 28)),
    ],
)
def test_calculate_subscription_end_date(start, duration_value, duration_unit, expected):
    assert calculate_subscription_end_date(start, duration_value, duration_unit) == expected


@pytest.mark.asyncio
async def test_create_subscription_org_wide_plan_snapshots_and_primary_slot(client, test_data):
    response = await create_subscription(client, test_data)
    assert response.status_code == 201, response.text
    data = response.json()

    assert data["subscription_code"].startswith("SUB-TESTOR-")
    assert data["org_id"] == str(test_data["org1_id"])
    assert data["branch_id"] == str(test_data["branch1_id"])
    assert data["membership_plan_id"] == str(test_data["org_wide_plan_id"])
    assert data["primary_member_id"] == str(test_data["member1_id"])
    assert data["start_date"] == "2026-06-11"
    assert data["end_date"] == "2026-07-11"
    assert data["status"] == "active"
    assert data["price_snapshot"] == "1500.00"
    assert data["currency_code"] == "INR"
    assert data["duration_value_snapshot"] == 1
    assert data["duration_unit_snapshot"] == "months"
    assert data["max_members_snapshot"] == 1
    assert len(data["members"]) == 1
    assert data["members"][0]["slot_number"] == 1
    assert data["members"][0]["role"] == "primary"
    assert data["members"][0]["member_id"] == str(test_data["member1_id"])


@pytest.mark.asyncio
async def test_create_subscription_branch_specific_plan_same_branch(client, test_data):
    response = await create_subscription(
        client,
        test_data,
        membership_plan_id=str(test_data["branch_plan_id"]),
        primary_member_id=str(test_data["member2_id"]),
    )
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["max_members_snapshot"] == 4
    assert data["end_date"] == "2026-07-11"


@pytest.mark.asyncio
async def test_reject_branch_specific_plan_for_different_branch(client, test_data):
    response = await create_subscription(
        client,
        test_data,
        branch_id=str(test_data["branch2_id"]),
        membership_plan_id=str(test_data["branch_plan_id"]),
    )
    assert response.status_code == 400
    assert "not available" in response.json()["detail"]


@pytest.mark.asyncio
async def test_reject_inactive_and_archived_plans(client, test_data):
    inactive = await create_subscription(client, test_data, membership_plan_id=str(test_data["inactive_plan_id"]))
    assert inactive.status_code == 400
    assert "active membership plans" in inactive.json()["detail"]

    archived = await create_subscription(client, test_data, membership_plan_id=str(test_data["archived_plan_id"]))
    assert archived.status_code == 400
    assert "active membership plans" in archived.json()["detail"]


@pytest.mark.asyncio
async def test_reject_inactive_member(client, test_data):
    response = await create_subscription(client, test_data, primary_member_id=str(test_data["inactive_member_id"]))
    assert response.status_code == 400
    assert "Member must be active" in response.json()["detail"]


@pytest.mark.asyncio
async def test_reject_cross_org_member_plan_and_branch(client, test_data):
    cross_member = await create_subscription(client, test_data, primary_member_id=str(test_data["member_other_id"]))
    assert cross_member.status_code == 400

    cross_plan = await create_subscription(client, test_data, membership_plan_id=str(test_data["other_plan_id"]))
    assert cross_plan.status_code == 400

    cross_branch = await create_subscription(client, test_data, branch_id=str(test_data["branch_other_id"]))
    assert cross_branch.status_code == 400


@pytest.mark.asyncio
async def test_path_org_must_match_authenticated_org(client, test_data):
    headers = get_headers(test_data["owner1_id"], test_data["org1_id"], "owner1@test.com")
    payload = {
        "branch_id": str(test_data["branch1_id"]),
        "membership_plan_id": str(test_data["org_wide_plan_id"]),
        "primary_member_id": str(test_data["member1_id"]),
    }
    response = await client.post(f"/organizations/{test_data['org2_id']}/member-subscriptions", json=payload, headers=headers)
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_list_and_detail_are_org_isolated(client, test_data):
    created = await create_subscription(client, test_data)
    subscription_id = created.json()["id"]
    headers1 = get_headers(test_data["owner1_id"], test_data["org1_id"], "owner1@test.com")
    headers2 = get_headers(test_data["owner2_id"], test_data["org2_id"], "owner2@test.com")

    list_response = await client.get(f"/organizations/{test_data['org1_id']}/member-subscriptions", headers=headers1)
    assert list_response.status_code == 200
    assert list_response.json()["total"] == 1
    assert list_response.json()["data"][0]["id"] == subscription_id

    detail = await client.get(f"/organizations/{test_data['org1_id']}/member-subscriptions/{subscription_id}", headers=headers1)
    assert detail.status_code == 200
    assert detail.json()["id"] == subscription_id
    assert len(detail.json()["members"]) == 1

    wrong_org_detail = await client.get(f"/organizations/{test_data['org2_id']}/member-subscriptions/{subscription_id}", headers=headers2)
    assert wrong_org_detail.status_code == 404


@pytest.mark.asyncio
async def test_server_managed_fields_are_ignored(client, test_data):
    response = await create_subscription(
        client,
        test_data,
        subscription_code="HACK-999",
        end_date="2099-01-01",
        price_snapshot=1,
        currency_code="USD",
        status="cancelled",
    )
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["subscription_code"] != "HACK-999"
    assert data["end_date"] == "2026-07-11"
    assert data["price_snapshot"] == "1500.00"
    assert data["currency_code"] == "INR"
    assert data["status"] == "active"


@pytest.mark.asyncio
async def test_duplicate_active_subscription_for_primary_member_is_rejected(client, test_data):
    first = await create_subscription(client, test_data)
    assert first.status_code == 201

    second = await create_subscription(client, test_data, membership_plan_id=str(test_data["branch_plan_id"]))
    assert second.status_code == 400
    assert "already has an active subscription" in second.json()["detail"]


@pytest.mark.asyncio
async def test_concurrent_subscription_code_generation_is_unique_and_sequential(client, test_data):
    headers = get_headers(test_data["owner1_id"], test_data["org1_id"], "owner1@test.com")

    async with AsyncSessionLocal() as session:
        extra_members = []
        for i in range(10):
            extra_members.append(
                Member(
                    id=uuid.uuid4(),
                    org_id=test_data["org1_id"],
                    member_uid=f"MC{i:03d}",
                    name=f"Concurrent Member {i}",
                    phone=f"98765100{i:02d}",
                    status=MemberStatus.active,
                    is_active=True,
                )
            )
        session.add_all(extra_members)
        await session.commit()
        member_ids = [member.id for member in extra_members]

    async def create_for_member(member_id):
        payload = {
            "branch_id": str(test_data["branch1_id"]),
            "membership_plan_id": str(test_data["org_wide_plan_id"]),
            "primary_member_id": str(member_id),
            "start_date": "2026-06-11",
        }
        return await client.post(f"/organizations/{test_data['org1_id']}/member-subscriptions", json=payload, headers=headers)

    responses = await asyncio.gather(*[create_for_member(member_id) for member_id in member_ids])
    codes = set()
    for response in responses:
        assert response.status_code == 201, response.text
        codes.add(response.json()["subscription_code"])

    assert len(codes) == 10
    assert codes == {f"SUB-TESTOR-{i:03d}" for i in range(1, 11)}
