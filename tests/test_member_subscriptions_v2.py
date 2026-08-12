import asyncio
import uuid
from datetime import date

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.database import AsyncSessionLocal, update_session_context
from app.core.security import create_access_token
from app.main import app
from app.models.auth import Owner
from app.models.enums import MemberStatus
from app.models.member import Member
from app.models.membership_plan import DurationUnit, MembershipPlan, PlanStatus
from app.models.org_branch import OrgBranch
from app.models.organization import Organization
from app.utils.subscription_dates import calculate_subscription_end_date


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


async def _set_owner_context(session, *, owner_id, org_id, request_suffix: str) -> None:
    await update_session_context(
        session,
        principal_id=str(owner_id),
        principal_type="owner",
        org_id=str(org_id),
        role="owner",
        ip_address="127.0.0.1",
        user_agent="pytest-member-subscriptions-v2",
        request_id=f"subscription-v2-{request_suffix}-{uuid.uuid4()}",
    )


@pytest_asyncio.fixture
async def test_data(admin_db_session, auth_db_session, db_session):
    """Create an isolated two-tenant subscription fixture without global teardown.

    The General CI database is disposable. Each test therefore uses unique
    tenant natural keys instead of truncating tenant-root/security tables.
    Root organization/owner setup stays on the administrative fixture identity;
    branch bootstrap uses the bounded auth identity; member/plan data uses the
    ordinary runtime identity under explicit tenant context.
    """
    suffix = uuid.uuid4().hex[:10]
    phone_seed = uuid.uuid4().int % 100_000_000

    org1_id = uuid.uuid4()
    org2_id = uuid.uuid4()
    owner1_id = uuid.uuid4()
    owner2_id = uuid.uuid4()
    owner1_email = f"owner1+{suffix}@test.com"
    owner2_email = f"owner2+{suffix}@test.com"

    admin_db_session.add_all(
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
            Owner(
                id=owner1_id,
                org_id=org1_id,
                owner_name="Owner 1",
                email=owner1_email,
                hashed_password="hash",
                email_verified=True,
            ),
            Owner(
                id=owner2_id,
                org_id=org2_id,
                owner_name="Owner 2",
                email=owner2_email,
                hashed_password="hash",
                email_verified=True,
            ),
        ]
    )
    await admin_db_session.commit()

    branch1_id = uuid.uuid4()
    branch2_id = uuid.uuid4()
    branch_other_id = uuid.uuid4()

    await _set_owner_context(
        auth_db_session,
        owner_id=owner1_id,
        org_id=org1_id,
        request_suffix=suffix,
    )
    auth_db_session.add_all(
        [
            OrgBranch(
                id=branch1_id,
                org_id=org1_id,
                branch_name="Branch 1",
                branch_code="BR1",
                internal_slug=f"branch-1-{suffix}",
                created_by=owner1_id,
            ),
            OrgBranch(
                id=branch2_id,
                org_id=org1_id,
                branch_name="Branch 2",
                branch_code="BR2",
                internal_slug=f"branch-2-{suffix}",
                created_by=owner1_id,
            ),
        ]
    )
    await auth_db_session.commit()

    await _set_owner_context(
        auth_db_session,
        owner_id=owner2_id,
        org_id=org2_id,
        request_suffix=suffix,
    )
    auth_db_session.add(
        OrgBranch(
            id=branch_other_id,
            org_id=org2_id,
            branch_name="Other Branch",
            branch_code="OB1",
            internal_slug=f"other-branch-{suffix}",
            created_by=owner2_id,
        )
    )
    await auth_db_session.commit()

    member1_id = uuid.uuid4()
    member2_id = uuid.uuid4()
    inactive_member_id = uuid.uuid4()
    member_other_id = uuid.uuid4()

    await _set_owner_context(
        db_session,
        owner_id=owner1_id,
        org_id=org1_id,
        request_suffix=suffix,
    )
    db_session.add_all(
        [
            Member(
                id=member1_id,
                org_id=org1_id,
                member_uid=f"M1-{suffix}",
                member_number=100,
                name="Primary Member",
                phone=f"9{phone_seed:08d}1",
                status=MemberStatus.active,
                is_active=True,
            ),
            Member(
                id=member2_id,
                org_id=org1_id,
                member_uid=f"M2-{suffix}",
                member_number=101,
                name="Second Member",
                phone=f"9{phone_seed:08d}2",
                status=MemberStatus.active,
                is_active=True,
            ),
            Member(
                id=inactive_member_id,
                org_id=org1_id,
                member_uid=f"M3-{suffix}",
                member_number=102,
                name="Inactive Member",
                phone=f"9{phone_seed:08d}3",
                status=MemberStatus.inactive,
                is_active=True,
            ),
        ]
    )

    org_wide_plan_id = uuid.uuid4()
    branch_plan_id = uuid.uuid4()
    inactive_plan_id = uuid.uuid4()
    archived_plan_id = uuid.uuid4()
    other_plan_id = uuid.uuid4()
    db_session.add_all(
        [
            MembershipPlan(
                id=org_wide_plan_id,
                org_id=org1_id,
                plan_code=f"PLAN-001-{suffix}",
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
                plan_code=f"PLAN-002-{suffix}",
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
                plan_code=f"PLAN-003-{suffix}",
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
                plan_code=f"PLAN-004-{suffix}",
                name="Archived Plan",
                price=100,
                currency="INR",
                duration_value=1,
                duration_unit=DurationUnit.months,
                max_members=1,
                status=PlanStatus.archived,
            ),
        ]
    )
    await db_session.commit()

    await _set_owner_context(
        db_session,
        owner_id=owner2_id,
        org_id=org2_id,
        request_suffix=suffix,
    )
    db_session.add_all(
        [
            Member(
                id=member_other_id,
                org_id=org2_id,
                member_uid=f"M4-{suffix}",
                member_number=100,
                name="Other Member",
                phone=f"9{phone_seed:08d}4",
                status=MemberStatus.active,
                is_active=True,
            ),
            MembershipPlan(
                id=other_plan_id,
                org_id=org2_id,
                plan_code=f"PLAN-005-{suffix}",
                name="Other Plan",
                price=100,
                currency="USD",
                duration_value=1,
                duration_unit=DurationUnit.months,
                max_members=1,
                status=PlanStatus.active,
            ),
        ]
    )
    await db_session.commit()

    return {
        "org1_id": org1_id,
        "org2_id": org2_id,
        "owner1_id": owner1_id,
        "owner2_id": owner2_id,
        "owner1_email": owner1_email,
        "owner2_email": owner2_email,
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
        "fixture_suffix": suffix,
        "phone_seed": phone_seed,
    }


async def create_subscription(client, test_data, **overrides):
    headers = get_headers(
        test_data["owner1_id"],
        test_data["org1_id"],
        test_data["owner1_email"],
    )
    payload = {
        "branch_id": str(test_data["branch1_id"]),
        "membership_plan_id": str(test_data["org_wide_plan_id"]),
        "primary_member_id": str(test_data["member1_id"]),
        "start_date": "2026-06-11",
    }
    payload.update(overrides)
    return await client.post(
        f"/organizations/{test_data['org1_id']}/member-subscriptions",
        json=payload,
        headers=headers,
    )


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
    inactive = await create_subscription(
        client,
        test_data,
        membership_plan_id=str(test_data["inactive_plan_id"]),
    )
    assert inactive.status_code == 400
    assert "active membership plans" in inactive.json()["detail"]

    archived = await create_subscription(
        client,
        test_data,
        membership_plan_id=str(test_data["archived_plan_id"]),
    )
    assert archived.status_code == 400
    assert "active membership plans" in archived.json()["detail"]


@pytest.mark.asyncio
async def test_reject_inactive_member(client, test_data):
    response = await create_subscription(
        client,
        test_data,
        primary_member_id=str(test_data["inactive_member_id"]),
    )
    assert response.status_code == 400
    assert "Member must be active" in response.json()["detail"]


@pytest.mark.asyncio
async def test_reject_cross_org_member_plan_and_branch(client, test_data):
    cross_member = await create_subscription(
        client,
        test_data,
        primary_member_id=str(test_data["member_other_id"]),
    )
    assert cross_member.status_code == 400

    cross_plan = await create_subscription(
        client,
        test_data,
        membership_plan_id=str(test_data["other_plan_id"]),
    )
    assert cross_plan.status_code == 400

    cross_branch = await create_subscription(
        client,
        test_data,
        branch_id=str(test_data["branch_other_id"]),
    )
    assert cross_branch.status_code == 400


@pytest.mark.asyncio
async def test_path_org_must_match_authenticated_org(client, test_data):
    headers = get_headers(
        test_data["owner1_id"],
        test_data["org1_id"],
        test_data["owner1_email"],
    )
    payload = {
        "branch_id": str(test_data["branch1_id"]),
        "membership_plan_id": str(test_data["org_wide_plan_id"]),
        "primary_member_id": str(test_data["member1_id"]),
    }
    response = await client.post(
        f"/organizations/{test_data['org2_id']}/member-subscriptions",
        json=payload,
        headers=headers,
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_list_and_detail_are_org_isolated(client, test_data):
    created = await create_subscription(client, test_data)
    subscription_id = created.json()["id"]
    headers1 = get_headers(
        test_data["owner1_id"],
        test_data["org1_id"],
        test_data["owner1_email"],
    )
    headers2 = get_headers(
        test_data["owner2_id"],
        test_data["org2_id"],
        test_data["owner2_email"],
    )

    list_response = await client.get(
        f"/organizations/{test_data['org1_id']}/member-subscriptions",
        headers=headers1,
    )
    assert list_response.status_code == 200
    assert list_response.json()["total"] == 1
    assert list_response.json()["data"][0]["id"] == subscription_id

    detail = await client.get(
        f"/organizations/{test_data['org1_id']}/member-subscriptions/{subscription_id}",
        headers=headers1,
    )
    assert detail.status_code == 200
    assert detail.json()["id"] == subscription_id
    assert len(detail.json()["members"]) == 1

    wrong_org_detail = await client.get(
        f"/organizations/{test_data['org2_id']}/member-subscriptions/{subscription_id}",
        headers=headers2,
    )
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

    second = await create_subscription(
        client,
        test_data,
        membership_plan_id=str(test_data["branch_plan_id"]),
    )
    assert second.status_code == 400
    assert "already has an active subscription" in second.json()["detail"]


@pytest.mark.asyncio
async def test_concurrent_subscription_code_generation_is_unique_and_sequential(client, test_data):
    headers = get_headers(
        test_data["owner1_id"],
        test_data["org1_id"],
        test_data["owner1_email"],
    )

    async with AsyncSessionLocal() as session:
        await _set_owner_context(
            session,
            owner_id=test_data["owner1_id"],
            org_id=test_data["org1_id"],
            request_suffix=test_data["fixture_suffix"],
        )
        extra_members = []
        for i in range(10):
            extra_members.append(
                Member(
                    id=uuid.uuid4(),
                    org_id=test_data["org1_id"],
                    member_uid=f"MC{test_data['fixture_suffix'][:6]}{i:03d}",
                    member_number=200 + i,
                    name=f"Concurrent Member {i}",
                    phone=f"8{test_data['phone_seed']:08d}{i % 10}",
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
        return await client.post(
            f"/organizations/{test_data['org1_id']}/member-subscriptions",
            json=payload,
            headers=headers,
        )

    responses = await asyncio.gather(
        *[create_for_member(member_id) for member_id in member_ids]
    )
    codes = set()
    for response in responses:
        assert response.status_code == 201, response.text
        codes.add(response.json()["subscription_code"])

    assert len(codes) == 10
    assert codes == {f"SUB-TESTOR-{i:03d}" for i in range(1, 11)}
