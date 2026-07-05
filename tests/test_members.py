import pytest
import pytest_asyncio
import uuid
import asyncio
from datetime import date
from httpx import AsyncClient, ASGITransport
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from app.main import app
from app.core.database import AsyncSessionLocal
from app.core.security import create_access_token
from app.models.organization import Organization
from app.models.auth import Owner
from app.models.gym import Gym
from app.models.org_branch import OrgBranch
from app.models.member import Member, MemberStatus
from app.models.member_subscription_v2 import MemberSubscriptionV2, ModernSubscriptionStatus
from app.models.membership_plan import MembershipPlan, DurationUnit, PlanStatus
from conftest import cleanup_test_database_tables

async def clear_members_test_data():
    await cleanup_test_database_tables([
        "members",
        "subscription_members",
        "member_subscriptions_v2",
        "membership_plans",
        "organization_counters",
        "gyms",
        "org_branch_state",
        "org_branches",
        "owners",
        "organizations",
    ])

@pytest_asyncio.fixture(autouse=True)
async def cleanup_database():
    await clear_members_test_data()
    yield
    await clear_members_test_data()

@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac

@pytest_asyncio.fixture
async def test_data():
    async with AsyncSessionLocal() as session:
        await session.execute(text("RESET ROLE"))
        org1_id = uuid.uuid4()
        org2_id = uuid.uuid4()
        org1 = Organization(id=org1_id, name="Test Org 1", slug="TEST-ORG-1", max_branches=5)
        org2 = Organization(id=org2_id, name="Test Org 2", slug="TEST-ORG-2", max_branches=5)
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

        gym1_id = uuid.uuid4()
        gym1 = Gym(id=gym1_id, org_id=org1_id, name="Legacy Gym", gymu_id="LEGACY-001")
        session.add(gym1)
        await session.flush()

        await session.commit()

        return {
            "org_id": org1_id,
            "owner_id": owner1_id,
            "branch_id": branch1_id,
            "branch2_id": branch2_id,
            "gym_id": gym1_id,
            "org2_id": org2_id,
            "owner2_id": owner2_id,
            "branch_other_id": branch_other_id,
        }

def get_headers(owner_id, org_id, email="owner@test.com"):
    token = create_access_token(str(owner_id), str(org_id), email, role="owner")
    return {"Authorization": f"Bearer {token}", "X-Request-ID": str(uuid.uuid4()), "X-Forwarded-For": "127.0.0.1"}

def valid_member_payload(test_data, **overrides):
    payload = {
        "name": "Jane Doe",
        "phone": "9876543210",
        "date_of_birth": "1990-01-01",
        "emergency_contact_name": "9876543211",
        "emergency_contact_phone": "9876543211",
        "home_branch_id": str(test_data["branch_id"]),
    }
    payload.update(overrides)
    return payload

@pytest.mark.asyncio
async def test_create_member_required_profile_data(client, test_data):
    headers = get_headers(test_data["owner_id"], test_data["org_id"])
    payload = valid_member_payload(
        test_data,
        phone="+91 98765 43210",
        emergency_contact_phone="09876543211",
        blood_group="O+",
    )
    response = await client.post(f"/organizations/{test_data['org_id']}/members", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["name"] == "Jane Doe"
    assert data["phone"] == "9876543210"
    assert data["date_of_birth"] == "1990-01-01"
    assert data["emergency_contact_name"] == "9876543211"
    assert data["emergency_contact_phone"] == "9876543211"
    assert data["home_branch_id"] == str(test_data["branch_id"])
    assert data["blood_group"] == "O+"
    assert "member_uid" in data
    assert data["member_number"] == 100
    assert data["member_display_code"] == "TESTOR-100"
    assert data["org_id"] == str(test_data["org_id"])
    assert data["gym_id"] is None

@pytest.mark.asyncio
async def test_member_numbers_are_org_scoped_and_sequential(client, test_data):
    headers1 = get_headers(test_data["owner_id"], test_data["org_id"], "owner1@test.com")
    headers2 = get_headers(test_data["owner2_id"], test_data["org2_id"], "owner2@test.com")

    first = await client.post(
        f"/organizations/{test_data['org_id']}/members",
        json=valid_member_payload(test_data, name="First Number", phone="9876543400"),
        headers=headers1,
    )
    second = await client.post(
        f"/organizations/{test_data['org_id']}/members",
        json=valid_member_payload(test_data, name="Second Number", phone="9876543401"),
        headers=headers1,
    )
    other_org = await client.post(
        f"/organizations/{test_data['org2_id']}/members",
        json=valid_member_payload(
            test_data,
            name="Other Org First",
            phone="9876543402",
            home_branch_id=str(test_data["branch_other_id"]),
        ),
        headers=headers2,
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert other_org.status_code == 200, other_org.text
    assert first.json()["data"]["member_number"] == 100
    assert second.json()["data"]["member_number"] == 101
    assert other_org.json()["data"]["member_number"] == 100

@pytest.mark.asyncio
async def test_concurrent_member_number_generation_is_unique_and_sequential(client, test_data):
    headers = get_headers(test_data["owner_id"], test_data["org_id"])

    async def create_member(index: int):
        return await client.post(
            f"/organizations/{test_data['org_id']}/members",
            json=valid_member_payload(
                test_data,
                name=f"Concurrent {index}",
                phone=f"98765435{index:02d}",
            ),
            headers=headers,
        )

    responses = await asyncio.gather(*(create_member(index) for index in range(8)))
    assert all(response.status_code == 200 for response in responses), [response.text for response in responses]
    numbers = sorted(response.json()["data"]["member_number"] for response in responses)
    assert numbers == list(range(100, 108))

@pytest.mark.asyncio
async def test_client_supplied_member_number_is_ignored_and_update_cannot_change_it(client, test_data):
    headers = get_headers(test_data["owner_id"], test_data["org_id"])
    create_resp = await client.post(
        f"/organizations/{test_data['org_id']}/members",
        json={**valid_member_payload(test_data, name="Immutable Number", phone="9876543410"), "member_number": 999},
        headers=headers,
    )
    assert create_resp.status_code == 200, create_resp.text
    created = create_resp.json()["data"]
    assert created["member_number"] == 100

    update_resp = await client.patch(
        f"/organizations/{test_data['org_id']}/members/{created['id']}",
        json={"name": "Still Immutable", "member_number": 999},
        headers=headers,
    )
    assert update_resp.status_code == 200, update_resp.text
    assert update_resp.json()["data"]["member_number"] == 100

@pytest.mark.asyncio
async def test_duplicate_member_number_same_org_is_rejected(test_data):
    async with AsyncSessionLocal() as session:
        await session.execute(text("RESET ROLE"))
        member_a = Member(
            id=uuid.uuid4(),
            org_id=test_data["org_id"],
            home_branch_id=test_data["branch_id"],
            member_uid="DUP-A",
            member_number=100,
            name="Duplicate A",
            phone="9876543420",
            status=MemberStatus.active,
            is_active=True,
        )
        member_b = Member(
            id=uuid.uuid4(),
            org_id=test_data["org_id"],
            home_branch_id=test_data["branch_id"],
            member_uid="DUP-B",
            member_number=100,
            name="Duplicate B",
            phone="9876543421",
            status=MemberStatus.active,
            is_active=True,
        )
        session.add_all([member_a, member_b])
        with pytest.raises(IntegrityError):
            await session.commit()

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,expected_status",
    [
        ("name", 422),
        ("phone", 422),
        ("date_of_birth", 400),
        ("emergency_contact_name", 400),
        ("home_branch_id", 400),
    ],
)
async def test_reject_missing_required_modern_create_fields(client, test_data, field, expected_status):
    headers = get_headers(test_data["owner_id"], test_data["org_id"])
    payload = valid_member_payload(test_data, phone="9876543299")
    payload.pop(field)
    response = await client.post(
        f"/organizations/{test_data['org_id']}/members",
        json=payload,
        headers=headers,
    )
    assert response.status_code == expected_status, response.text

@pytest.mark.asyncio
async def test_create_member_allows_missing_emergency_contact_no_2(client, test_data):
    headers = get_headers(test_data["owner_id"], test_data["org_id"])
    payload = valid_member_payload(test_data, phone="9876543297")
    payload.pop("emergency_contact_phone")
    response = await client.post(
        f"/organizations/{test_data['org_id']}/members",
        json=payload,
        headers=headers,
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["emergency_contact_name"] == "9876543211"
    assert data["emergency_contact_phone"] is None

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value,expected_status",
    [
        ("phone", "12345", 400),
        ("emergency_contact_name", "12345", 400),
        ("emergency_contact_phone", "12345", 400),
        ("date_of_birth", "2999-01-01", 422),
        ("date_of_birth", "1800-01-01", 422),
        ("date_of_birth", "2025-01-01", 422),
        ("blood_group", "Z+", 422),
    ],
)
async def test_reject_invalid_member_profile_values(client, test_data, field, value, expected_status):
    headers = get_headers(test_data["owner_id"], test_data["org_id"])
    payload = valid_member_payload(test_data, phone="9876543298")
    payload[field] = value
    response = await client.post(
        f"/organizations/{test_data['org_id']}/members",
        json=payload,
        headers=headers,
    )
    assert response.status_code == expected_status, response.text

@pytest.mark.asyncio
async def test_accept_valid_blood_groups(client, test_data):
    headers = get_headers(test_data["owner_id"], test_data["org_id"])
    blood_groups = ["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"]
    for index, blood_group in enumerate(blood_groups):
        response = await client.post(
            f"/organizations/{test_data['org_id']}/members",
            json=valid_member_payload(
                test_data,
                name=f"Blood Group {index}",
                phone=f"98765432{index:02d}",
                emergency_contact_phone=f"98765433{index:02d}",
                blood_group=blood_group,
            ),
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["blood_group"] == blood_group

@pytest.mark.asyncio
async def test_reject_duplicate_active_phone(client, test_data):
    headers = get_headers(test_data["owner_id"], test_data["org_id"])
    payload = valid_member_payload(test_data, name="First", phone="9876543212")
    await client.post(f"/organizations/{test_data['org_id']}/members", json=payload, headers=headers)
    
    payload_dup = valid_member_payload(test_data, name="Duplicate", phone="9876543212")
    response = await client.post(f"/organizations/{test_data['org_id']}/members", json=payload_dup, headers=headers)
    assert response.status_code == 400

@pytest.mark.asyncio
async def test_reject_cross_org_home_branch(client, test_data):
    headers = get_headers(test_data["owner_id"], test_data["org_id"])
    payload = valid_member_payload(
        test_data,
        name="Cross Org",
        phone="9876543213",
        home_branch_id=str(test_data["branch_other_id"]),
    )
    response = await client.post(f"/organizations/{test_data['org_id']}/members", json=payload, headers=headers)
    assert response.status_code == 400

@pytest.mark.asyncio
async def test_allow_same_phone_in_different_org(client, test_data):
    headers1 = get_headers(test_data["owner_id"], test_data["org_id"], "owner1@test.com")
    headers2 = get_headers(test_data["owner2_id"], test_data["org2_id"], "owner2@test.com")
    payload = valid_member_payload(test_data, name="Shared Phone", phone="9876543214")

    response1 = await client.post(f"/organizations/{test_data['org_id']}/members", json=payload, headers=headers1)
    response2 = await client.post(
        f"/organizations/{test_data['org2_id']}/members",
        json={
            **payload,
            "name": "Shared Phone Other",
            "home_branch_id": str(test_data["branch_other_id"]),
        },
        headers=headers2,
    )

    assert response1.status_code == 200, response1.text
    assert response2.status_code == 200, response2.text
    assert response1.json()["data"]["org_id"] != response2.json()["data"]["org_id"]

@pytest.mark.asyncio
async def test_list_members_org_isolation_and_branch_filter(client, test_data):
    headers = get_headers(test_data["owner_id"], test_data["org_id"])
    headers2 = get_headers(test_data["owner2_id"], test_data["org2_id"], "owner2@test.com")
    await client.post(
        f"/organizations/{test_data['org_id']}/members",
        json=valid_member_payload(test_data, name="Branch One", phone="9876543215"),
        headers=headers,
    )
    await client.post(
        f"/organizations/{test_data['org_id']}/members",
        json=valid_member_payload(
            test_data,
            name="Branch Two",
            phone="9876543216",
            home_branch_id=str(test_data["branch2_id"]),
        ),
        headers=headers,
    )
    await client.post(
        f"/organizations/{test_data['org2_id']}/members",
        json=valid_member_payload(
            test_data,
            name="Other Org",
            phone="9876543217",
            home_branch_id=str(test_data["branch_other_id"]),
        ),
        headers=headers2,
    )
    
    response = await client.get(f"/organizations/{test_data['org_id']}/members", headers=headers)
    assert response.status_code == 200
    assert len(response.json()["data"]) == 2
    assert {m["org_id"] for m in response.json()["data"]} == {str(test_data["org_id"])}

    branch_response = await client.get(
        f"/organizations/{test_data['org_id']}/members",
        params={"home_branch_id": str(test_data["branch_id"])},
        headers=headers,
    )
    assert branch_response.status_code == 200
    assert len(branch_response.json()["data"]) == 1
    assert branch_response.json()["data"][0]["home_branch_id"] == str(test_data["branch_id"])

@pytest.mark.asyncio
async def test_member_search_by_number_name_phone_and_branch(client, test_data):
    headers = get_headers(test_data["owner_id"], test_data["org_id"])
    await client.post(
        f"/organizations/{test_data['org_id']}/members",
        json=valid_member_payload(test_data, name="Alpha Search", phone="9876543430"),
        headers=headers,
    )
    await client.post(
        f"/organizations/{test_data['org_id']}/members",
        json=valid_member_payload(
            test_data,
            name="Beta Lookup",
            phone="9876543431",
            home_branch_id=str(test_data["branch2_id"]),
        ),
        headers=headers,
    )

    by_number = await client.get(
        f"/organizations/{test_data['org_id']}/members",
        params={"search": "100"},
        headers=headers,
    )
    assert by_number.status_code == 200
    assert [member["name"] for member in by_number.json()["data"]] == ["Alpha Search"]

    by_name = await client.get(
        f"/organizations/{test_data['org_id']}/members",
        params={"search": "lookup"},
        headers=headers,
    )
    assert by_name.status_code == 200
    assert [member["name"] for member in by_name.json()["data"]] == ["Beta Lookup"]

    by_phone = await client.get(
        f"/organizations/{test_data['org_id']}/members",
        params={"search": "3430"},
        headers=headers,
    )
    assert by_phone.status_code == 200
    assert [member["name"] for member in by_phone.json()["data"]] == ["Alpha Search"]

    by_branch = await client.get(
        f"/organizations/{test_data['org_id']}/members",
        params={"branch_id": str(test_data["branch2_id"])},
        headers=headers,
    )
    assert by_branch.status_code == 200
    assert [member["name"] for member in by_branch.json()["data"]] == ["Beta Lookup"]

@pytest.mark.asyncio
async def test_member_search_active_subscription_projection(client, test_data):
    headers = get_headers(test_data["owner_id"], test_data["org_id"])
    create_resp = await client.post(
        f"/organizations/{test_data['org_id']}/members",
        json=valid_member_payload(test_data, name="Subscribed Member", phone="9876543440"),
        headers=headers,
    )
    assert create_resp.status_code == 200, create_resp.text
    member = create_resp.json()["data"]
    unsubscribed_resp = await client.post(
        f"/organizations/{test_data['org_id']}/members",
        json=valid_member_payload(test_data, name="Available Member", phone="9876543441"),
        headers=headers,
    )
    assert unsubscribed_resp.status_code == 200, unsubscribed_resp.text
    unsubscribed_member = unsubscribed_resp.json()["data"]

    async with AsyncSessionLocal() as session:
        await session.execute(text("RESET ROLE"))
        plan_id = uuid.uuid4()
        subscription_id = uuid.uuid4()
        plan = MembershipPlan(
            id=plan_id,
            org_id=test_data["org_id"],
            plan_code="SEARCH-001",
            name="Search Plan",
            price=1500,
            currency="INR",
            duration_value=1,
            duration_unit=DurationUnit.months,
            max_members=1,
            status=PlanStatus.active,
        )
        session.add(plan)
        await session.flush()
        session.add(
            MemberSubscriptionV2(
                id=subscription_id,
                org_id=test_data["org_id"],
                branch_id=test_data["branch_id"],
                membership_plan_id=plan_id,
                primary_member_id=uuid.UUID(member["id"]),
                subscription_code="SUB-SEARCH-001",
                start_date=date(2026, 6, 13),
                end_date=date(2026, 7, 13),
                status=ModernSubscriptionStatus.active,
                price_snapshot=1500,
                currency_code="INR",
                duration_value_snapshot=1,
                duration_unit_snapshot=DurationUnit.months,
                max_members_snapshot=1,
            )
        )
        await session.commit()

    response = await client.get(
        f"/organizations/{test_data['org_id']}/members",
        params={"search": "Subscribed"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    result = response.json()["data"][0]
    assert result["has_active_subscription"] is True
    assert result["active_subscription_id"] == str(subscription_id)
    assert result["home_branch_name"] == "Branch 1"

    no_branch_filter = await client.get(
        f"/organizations/{test_data['org_id']}/members",
        params={"status": "active", "has_active_subscription": "false", "page": 1, "page_size": 20},
        headers=headers,
    )
    assert no_branch_filter.status_code == 200
    available_members = no_branch_filter.json()["data"]
    available_member = next(member for member in available_members if member["id"] == unsubscribed_member["id"])
    assert available_member["has_active_subscription"] is False
    assert available_member["active_subscription_id"] is None
    assert all(member["id"] != result["id"] for member in available_members)

    available = await client.get(
        f"/organizations/{test_data['org_id']}/members",
        params={"has_active_subscription": "false"},
        headers=headers,
    )
    assert available.status_code == 200
    assert all(member["id"] != result["id"] for member in available.json()["data"])

@pytest.mark.asyncio
async def test_get_member_by_id_enforces_org_isolation(client, test_data):
    headers1 = get_headers(test_data["owner_id"], test_data["org_id"], "owner1@test.com")
    headers2 = get_headers(test_data["owner2_id"], test_data["org2_id"], "owner2@test.com")
    create_resp = await client.post(
        f"/organizations/{test_data['org_id']}/members",
        json=valid_member_payload(test_data, name="Org One Member", phone="9876543218"),
        headers=headers1,
    )
    member_id = create_resp.json()["data"]["id"]

    wrong_path = await client.get(f"/organizations/{test_data['org2_id']}/members/{member_id}", headers=headers1)
    assert wrong_path.status_code == 403

    wrong_org_lookup = await client.get(f"/organizations/{test_data['org2_id']}/members/{member_id}", headers=headers2)
    assert wrong_org_lookup.status_code == 404

@pytest.mark.asyncio
async def test_update_member_allowed_fields_and_immutable_generated_fields(client, test_data):
    headers = get_headers(test_data["owner_id"], test_data["org_id"])
    create_resp = await client.post(
        f"/organizations/{test_data['org_id']}/members",
        json=valid_member_payload(test_data, name="Old Name", phone="9876543220"),
        headers=headers,
    )
    created = create_resp.json()["data"]
    member_id = created["id"]
    
    response = await client.patch(
        f"/organizations/{test_data['org_id']}/members/{member_id}",
        json={"name": "New Name", "member_uid": "SHOULD-NOT-CHANGE", "qr_token": "SHOULD-NOT-CHANGE"},
        headers=headers,
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["name"] == "New Name"
    assert data["member_uid"] == created["member_uid"]
    assert data["qr_token"] == created["qr_token"]

@pytest.mark.asyncio
async def test_update_member_validates_required_profile_fields(client, test_data):
    headers = get_headers(test_data["owner_id"], test_data["org_id"])
    create_resp = await client.post(
        f"/organizations/{test_data['org_id']}/members",
        json=valid_member_payload(test_data, name="Editable", phone="9876543223"),
        headers=headers,
    )
    member_id = create_resp.json()["data"]["id"]

    invalid_name = await client.patch(
        f"/organizations/{test_data['org_id']}/members/{member_id}",
        json={"name": " "},
        headers=headers,
    )
    assert invalid_name.status_code == 422

    invalid_phone = await client.patch(
        f"/organizations/{test_data['org_id']}/members/{member_id}",
        json={"emergency_contact_phone": "123"},
        headers=headers,
    )
    assert invalid_phone.status_code == 400

@pytest.mark.asyncio
async def test_soft_delete_member(client, test_data):
    headers = get_headers(test_data["owner_id"], test_data["org_id"])
    create_resp = await client.post(
        f"/organizations/{test_data['org_id']}/members",
        json=valid_member_payload(test_data, name="To Delete", phone="9876543221"),
        headers=headers,
    )
    member_id = create_resp.json()["data"]["id"]
    
    del_resp = await client.delete(f"/organizations/{test_data['org_id']}/members/{member_id}", headers=headers)
    assert del_resp.status_code == 200
    
    list_resp = await client.get(f"/organizations/{test_data['org_id']}/members", headers=headers)
    assert len(list_resp.json()["data"]) == 0

@pytest.mark.asyncio
async def test_legacy_gym_member_create_still_sets_org(client, test_data):
    headers = get_headers(test_data["owner_id"], test_data["org_id"])
    response = await client.post(
        f"/gyms/{test_data['gym_id']}/members",
        json={"name": "Legacy Member", "phone": "9876543222"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["gym_id"] == str(test_data["gym_id"])
    assert data["org_id"] == str(test_data["org_id"])
