
from __future__ import annotations

import asyncio
import base64
import inspect
import json
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
import yaml
from fastapi import Request
from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as app_database
from app.core.config import settings
from app.core.security import create_access_token
from app.main import app
from app.platform_billing.api import checkout as checkout_api
from app.platform_billing.domain.capability import OperationClass
from app.platform_billing.models.billing_account import PlatformBillingAccount
from app.platform_billing.models.catalog import (
    PlatformPlanVersion,
    PlatformPolicyVersion,
    PlatformPrice,
    PlatformProduct,
)
from app.platform_billing.models.provider import PlatformProviderCustomer, PlatformProviderOperation
from app.platform_billing.models.subscription import PlatformSubscription
from app.platform_billing.policies.capability_registry import get_capability_registry
from tests.platform_billing.test_phase1_schema import cleanup_phase1_tables

ADMIN_ORG_ID = uuid.uuid4()
OWNER_ORG_ID = uuid.uuid4()


@pytest_asyncio.fixture(autouse=True)
async def _cleanup():
    await cleanup_phase1_tables()
    yield
    await cleanup_phase1_tables()


@pytest_asyncio.fixture(autouse=True)
async def setup_test_db_override():
    original_override = app.dependency_overrides.get(app_database.get_db)

    async def custom_override_get_db(request: Request = None) -> AsyncGenerator[AsyncSession, None]:
        async with app_database.AsyncSessionLocal() as session:
            org_id = None
            if request:
                auth = request.headers.get("Authorization")
                if auth and auth.startswith("Bearer "):
                    token = auth.split(" ", 1)[1]
                    try:
                        payload_b64 = token.split(".")[1]
                        payload_b64 += "=" * ((4 - len(payload_b64) % 4) % 4)
                        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
                        org_id = payload.get("org_id")
                    except Exception:
                        pass

            try:
                await app_database.SessionContextInitializer.initialize(
                    session,
                    user_id="00000000-0000-0000-0000-000000000000",
                    org_id=org_id,
                    gym_id=None,
                    trace_id="test",
                    role="unknown",
                )
                if org_id:
                    await session.execute(
                        text("SELECT pg_catalog.set_config('app.current_org_id', :oid, false)"),
                        {"oid": org_id},
                    )
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[app_database.get_db] = custom_override_get_db
    yield
    if original_override:
        app.dependency_overrides[app_database.get_db] = original_override
    else:
        app.dependency_overrides.pop(app_database.get_db, None)


@pytest.fixture
def admin_token_headers() -> dict[str, str]:
    token = create_access_token(str(uuid.uuid4()), str(ADMIN_ORG_ID), "admin@test.com", role="admin")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def owner_token_headers() -> dict[str, str]:
    token = create_access_token(str(uuid.uuid4()), str(OWNER_ORG_ID), "owner@test.com", role="owner")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def provider_call_recorder(monkeypatch) -> list[object]:
    import app.platform_billing.providers.fake as fake_provider

    calls: list[object] = []
    original_execute = fake_provider.DeterministicFakeProvider.execute

    async def wrapped_execute(self, request):
        calls.append(request)
        return await original_execute(self, request)

    monkeypatch.setattr(fake_provider.DeterministicFakeProvider, "execute", wrapped_execute)
    return calls


@pytest_asyncio.fixture
async def platform_catalog(db_session: AsyncSession) -> dict[str, uuid.UUID | str]:
    product = PlatformProduct(
        id=uuid.uuid4(),
        code=f"TEST-PRODUCT-{uuid.uuid4().hex[:8].upper()}",
        name="Test Product",
        description="For fake checkout tests",
        status="active",
    )
    db_session.add(product)

    dummy_sha = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    rand_ver = uuid.uuid4().int % 1000000
    dunning = PlatformPolicyVersion(
        id=uuid.uuid4(),
        code=f"DUNNING-{uuid.uuid4().hex[:8].upper()}",
        policy_type="dunning",
        version=rand_ver,
        status="published",
        published_at=datetime.now(timezone.utc),
        payload_sha256=dummy_sha,
    )
    cancel = PlatformPolicyVersion(
        id=uuid.uuid4(),
        code=f"CANCEL-{uuid.uuid4().hex[:8].upper()}",
        policy_type="cancellation",
        version=rand_ver,
        status="published",
        published_at=datetime.now(timezone.utc),
        payload_sha256=dummy_sha,
    )
    downgrade = PlatformPolicyVersion(
        id=uuid.uuid4(),
        code=f"DOWN-{uuid.uuid4().hex[:8].upper()}",
        policy_type="downgrade",
        version=rand_ver,
        status="published",
        published_at=datetime.now(timezone.utc),
        payload_sha256=dummy_sha,
    )
    db_session.add_all([dunning, cancel, downgrade])

    plan_code = f"TEST-PLAN-{uuid.uuid4().hex[:8].upper()}"
    plan = PlatformPlanVersion(
        id=uuid.uuid4(),
        product_id=product.id,
        version=1,
        code=plan_code,
        display_name="Test Plan",
        status="published",
        published_at=datetime.now(timezone.utc),
        dunning_policy_version_id=dunning.id,
        cancellation_policy_version_id=cancel.id,
        downgrade_policy_version_id=downgrade.id,
    )
    db_session.add(plan)

    price = PlatformPrice(
        id=uuid.uuid4(),
        plan_version_id=plan.id,
        code=f"TEST-PRICE-{uuid.uuid4().hex[:8].upper()}",
        country_code=None,
        currency_code="USD",
        amount_minor=1000,
        billing_interval="month",
        interval_count=1,
        tax_behavior="exclusive",
        status="active",
        valid_from=datetime.now(timezone.utc) - timedelta(minutes=1),
        published_at=datetime.now(timezone.utc),
    )
    db_session.add(price)

    plan2_code = f"TEST-PLAN-2-{uuid.uuid4().hex[:8].upper()}"
    plan2 = PlatformPlanVersion(
        id=uuid.uuid4(),
        product_id=product.id,
        version=2,
        code=plan2_code,
        display_name="Test Plan 2",
        status="published",
        published_at=datetime.now(timezone.utc),
        dunning_policy_version_id=dunning.id,
        cancellation_policy_version_id=cancel.id,
        downgrade_policy_version_id=downgrade.id,
    )
    db_session.add(plan2)
    price2 = PlatformPrice(
        id=uuid.uuid4(),
        plan_version_id=plan2.id,
        code=f"TEST-PRICE-2-{uuid.uuid4().hex[:8].upper()}",
        country_code=None,
        currency_code="USD",
        amount_minor=2000,
        billing_interval="month",
        interval_count=1,
        tax_behavior="exclusive",
        status="active",
        valid_from=datetime.now(timezone.utc) - timedelta(minutes=1),
        published_at=datetime.now(timezone.utc),
    )
    db_session.add(price2)

    plan3_code = f"TEST-PLAN-AMBIGUOUS-{uuid.uuid4().hex[:8].upper()}"
    plan3 = PlatformPlanVersion(
        id=uuid.uuid4(),
        product_id=product.id,
        version=3,
        code=plan3_code,
        display_name="Test Plan 3",
        status="published",
        published_at=datetime.now(timezone.utc),
        dunning_policy_version_id=dunning.id,
        cancellation_policy_version_id=cancel.id,
        downgrade_policy_version_id=downgrade.id,
    )
    db_session.add(plan3)
    price3a = PlatformPrice(
        id=uuid.uuid4(),
        plan_version_id=plan3.id,
        code=f"TEST-PRICE-3A-{uuid.uuid4().hex[:8].upper()}",
        country_code=None,
        currency_code="USD",
        amount_minor=3000,
        billing_interval="month",
        interval_count=1,
        tax_behavior="exclusive",
        status="active",
        valid_from=datetime.now(timezone.utc) - timedelta(minutes=1),
        published_at=datetime.now(timezone.utc),
    )
    price3b = PlatformPrice(
        id=uuid.uuid4(),
        plan_version_id=plan3.id,
        code=f"TEST-PRICE-3B-{uuid.uuid4().hex[:8].upper()}",
        country_code=None,
        currency_code="EUR",
        amount_minor=3000,
        billing_interval="month",
        interval_count=1,
        tax_behavior="exclusive",
        status="active",
        valid_from=datetime.now(timezone.utc) - timedelta(minutes=1),
        published_at=datetime.now(timezone.utc),
    )
    db_session.add_all([price3a, price3b])

    await db_session.commit()
    return {
        "product_id": product.id,
        "plan_id": plan.id,
        "plan_code": plan_code,
        "price_id": price.id,
        "plan2_id": plan2.id,
        "plan2_code": plan2_code,
        "plan_ambiguous_code": plan3_code,
    }


async def _seed_organization(db_session: AsyncSession, org_id: uuid.UUID, label: str) -> None:
    await db_session.execute(
        text("SELECT pg_catalog.set_config('app.current_org_id', :org_id, true)"),
        {"org_id": str(org_id)},
    )
    await db_session.execute(
        text(
            """
            INSERT INTO organizations (id, name, slug, tier, is_active, max_branches, default_currency_code)
            VALUES (:org_id, :name, :slug, 'basic', true, 1, 'USD')
            ON CONFLICT (id) DO NOTHING
            """
        ),
        {"org_id": str(org_id), "name": label, "slug": f"{label.lower().replace(' ', '-')}-{uuid.uuid4().hex[:8]}"},
    )


async def _seed_provider_customer(
    db_session: AsyncSession,
    *,
    org_id: uuid.UUID = ADMIN_ORG_ID,
    provider_code: str = "fake",
    status: str = "active",
    external_ref: str | None = None,
) -> PlatformProviderCustomer:
    await _seed_organization(db_session, org_id, f"Org {org_id.hex[:8]}")
    customer = PlatformProviderCustomer(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider_code=provider_code,
        status=status,
        external_customer_ref=external_ref or f"cus_{provider_code}_{uuid.uuid4().hex[:8]}",
    )
    db_session.add(customer)
    await db_session.commit()
    return customer


@pytest_asyncio.fixture
async def fake_customer(db_session: AsyncSession) -> PlatformProviderCustomer:
    customer = await _seed_provider_customer(
        db_session,
        org_id=ADMIN_ORG_ID,
        provider_code="fake",
        status="active",
        external_ref="cus_fake123",
    )
    await _seed_provider_customer(
        db_session,
        org_id=OWNER_ORG_ID,
        provider_code="fake",
        status="active",
        external_ref="cus_fake456",
    )
    return customer


def _enable_fake_checkout(monkeypatch) -> None:
    monkeypatch.setattr(settings, "PLATFORM_BILLING_FAKE_CHECKOUT_ENABLED", True)
    monkeypatch.setattr(settings, "PLATFORM_BILLING_PROVIDER_MODE", "fake")
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")


async def _post_checkout(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    idempotency_key: str,
    plan_code: str,
    billing_interval: str | None = None,
):
    payload = {"plan_code": plan_code}
    if billing_interval is not None:
        payload["billing_interval"] = billing_interval
    return await client.post(
        "/api/v1/platform-billing/checkout-sessions",
        headers={**headers, "Idempotency-Key": idempotency_key},
        json=payload,
    )


async def _operation_rows(
    db_session: AsyncSession,
    *,
    org_id: uuid.UUID,
    idempotency_key: str,
) -> list[PlatformProviderOperation]:
    await db_session.execute(
        text("SELECT pg_catalog.set_config('app.current_org_id', :org_id, true)"),
        {"org_id": str(org_id)},
    )
    result = await db_session.execute(
        select(PlatformProviderOperation)
        .where(
            PlatformProviderOperation.organization_id == org_id,
            PlatformProviderOperation.provider_code == "fake",
            PlatformProviderOperation.idempotency_key == idempotency_key,
        )
        .order_by(PlatformProviderOperation.created_at, PlatformProviderOperation.id)
    )
    return list(result.scalars().all())


async def _provider_operation_count(db_session: AsyncSession, org_id: uuid.UUID) -> int:
    await db_session.execute(
        text("SELECT pg_catalog.set_config('app.current_org_id', :org_id, true)"),
        {"org_id": str(org_id)},
    )
    result = await db_session.execute(
        select(func.count()).select_from(PlatformProviderOperation).where(
            PlatformProviderOperation.organization_id == org_id
        )
    )
    return int(result.scalar_one())


async def _seed_current_subscription(
    db_session: AsyncSession,
    *,
    status: str,
    platform_catalog: dict[str, uuid.UUID | str],
) -> PlatformSubscription:
    await _seed_organization(db_session, ADMIN_ORG_ID, "Subscription Org")
    billing_account = PlatformBillingAccount(
        id=uuid.uuid4(),
        organization_id=ADMIN_ORG_ID,
        status="active",
        legal_name="Subscription Org",
        billing_email="billing@example.test",
        country_code="US",
        default_currency_code="USD",
        city="Test City",
    )
    db_session.add(billing_account)
    now = datetime.now(timezone.utc)
    subscription = PlatformSubscription(
        id=uuid.uuid4(),
        organization_id=ADMIN_ORG_ID,
        billing_account_id=billing_account.id,
        status=status,
        current_plan_version_id=platform_catalog["plan_id"],
        current_price_id=platform_catalog["price_id"],
        policy_snapshot_json={},
        started_at=now - timedelta(days=1),
        current_period_start=now - timedelta(days=1),
        current_period_end=now + timedelta(days=29),
        cancel_at_period_end=status == "cancel_scheduled",
        cancellation_requested_at=now if status == "cancel_scheduled" else None,
        cancellation_effective_at=now + timedelta(days=29) if status == "cancel_scheduled" else None,
    )
    db_session.add(subscription)
    await db_session.commit()
    return subscription


def test_checkout_configuration_surface_has_one_authoritative_fake_checkout_path():
    related = sorted(
        name
        for name in settings.__class__.model_fields
        if "CHECKOUT" in name or name == "PLATFORM_BILLING_PROVIDER_MODE"
    )
    assert related == [
        "PLATFORM_BILLING_CHECKOUT",
        "PLATFORM_BILLING_FAKE_CHECKOUT_ENABLED",
        "PLATFORM_BILLING_FAKE_CHECKOUT_SIMULATION_ENABLED",
        "PLATFORM_BILLING_PROVIDER_MODE",
    ]
    assert not hasattr(settings, "ENABLE_FAKE_CHECKOUT_API")
    assert not hasattr(settings, "PLATFORM_BILLING_CHECKOUT_ENABLED")
    assert settings.PLATFORM_BILLING_FAKE_CHECKOUT_ENABLED is False
    assert settings.PLATFORM_BILLING_FAKE_CHECKOUT_SIMULATION_ENABLED is False
    assert settings.PLATFORM_BILLING_PROVIDER_MODE == "disabled"


def test_checkout_route_capability_manifest_and_registry_agree():
    registry = get_capability_registry()
    assert registry.get("commercial_transactions:create") is None
    assert registry.get("commercial_transactions:view") is None
    post_capability = registry.get("platform_billing.change_plan")
    get_capability = registry.get("platform_billing.view")
    assert post_capability is not None
    assert post_capability.operation_class == OperationClass.financial
    assert get_capability is not None
    assert get_capability.operation_class == OperationClass.safe_read

    source = inspect.getsource(checkout_api.create_checkout_session)
    assert 'require_platform_capability("platform_billing.change_plan", OperationClass.financial.value)' in source
    assert "commercial_transactions" not in source
    assert "PlatformProviderCustomer" not in inspect.getsource(checkout_api)

    manifest = yaml.safe_load(Path("tests/platform_billing/fixtures/phase3_route_inventory.yaml").read_text())
    routes = {
        (entry["method"], entry["normalized_route_path"]): entry
        for entry in manifest["migrated_routes"]
    }
    assert routes[("POST", "/api/v1/platform-billing/checkout-sessions")]["proposed_capability"] == "platform_billing.change_plan"
    assert routes[("POST", "/api/v1/platform-billing/checkout-sessions")]["operation_class"] == "financial"
    assert routes[("GET", "/api/v1/platform-billing/checkout-operations/{operation_id}")]["proposed_capability"] == "platform_billing.view"
    assert routes[("GET", "/api/v1/platform-billing/checkout-operations/{operation_id}")]["operation_class"] == "safe_read"


@pytest.mark.asyncio
async def test_checkout_disabled_by_flag(
    client: AsyncClient, admin_token_headers: dict[str, str], platform_catalog, monkeypatch
):
    monkeypatch.setattr(settings, "PLATFORM_BILLING_FAKE_CHECKOUT_ENABLED", False)
    response = await _post_checkout(
        client,
        admin_token_headers,
        idempotency_key="test-idem-key-12345678",
        plan_code=platform_catalog["plan_code"],
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "PLATFORM_BILLING_FAKE_CHECKOUT_DISABLED"


@pytest.mark.asyncio
async def test_checkout_disabled_in_production(
    client: AsyncClient, admin_token_headers: dict[str, str], monkeypatch, platform_catalog
):
    monkeypatch.setattr(settings, "PLATFORM_BILLING_FAKE_CHECKOUT_ENABLED", True)
    monkeypatch.setattr(settings, "PLATFORM_BILLING_PROVIDER_MODE", "fake")
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    response = await _post_checkout(
        client,
        admin_token_headers,
        idempotency_key="test-idem-key-12345678",
        plan_code=platform_catalog["plan_code"],
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "PLATFORM_BILLING_FAKE_CHECKOUT_DISABLED"


@pytest.mark.asyncio
async def test_checkout_missing_idempotency_key(
    client: AsyncClient, admin_token_headers: dict[str, str], monkeypatch, platform_catalog
):
    _enable_fake_checkout(monkeypatch)
    response = await client.post(
        "/api/v1/platform-billing/checkout-sessions",
        headers=admin_token_headers,
        json={"plan_code": platform_catalog["plan_code"]},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    [
        "amount_minor",
        "currency_code",
        "price",
        "discount",
        "tax",
        "provider_price_id",
        "provider_customer_id",
        "organization_id",
        "tenant_id",
        "success",
        "paid",
        "payment_status",
        "subscription_status",
        "idempotency_key",
    ],
)
async def test_checkout_request_rejects_client_controlled_money_identity_and_status_fields(
    client: AsyncClient,
    admin_token_headers: dict[str, str],
    monkeypatch,
    platform_catalog,
    field: str,
):
    _enable_fake_checkout(monkeypatch)
    response = await client.post(
        "/api/v1/platform-billing/checkout-sessions",
        headers={**admin_token_headers, "Idempotency-Key": f"schema-test-{uuid.uuid4().hex}"},
        json={"plan_code": platform_catalog["plan_code"], field: "client-controlled"},
    )
    assert response.status_code == 422
    assert "extra_forbidden" in response.text or "Extra inputs are not permitted" in response.text


@pytest.mark.asyncio
async def test_checkout_idempotency_key_format(
    client: AsyncClient, admin_token_headers: dict[str, str], monkeypatch, platform_catalog
):
    _enable_fake_checkout(monkeypatch)

    cases = [
        ("a" * 15, 422, None),
        ("a" * 16, 422, "PROVIDER_CUSTOMER_MISSING"),
        ("a" * 160, 422, "PROVIDER_CUSTOMER_MISSING"),
        ("a" * 161, 422, None),
        ("invalid chars-12345", 422, None),
        ("invalid-chars-!@#$-1234", 422, None),
        (f"  {'a' * 16}  ", 422, "PROVIDER_CUSTOMER_MISSING"),
    ]
    for key, expected_status, expected_code in cases:
        response = await _post_checkout(
            client,
            admin_token_headers,
            idempotency_key=key,
            plan_code=platform_catalog["plan_code"],
        )
        assert response.status_code == expected_status
        if expected_code:
            assert response.json()["detail"]["code"] == expected_code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["missing", "inactive", "wrong_provider", "cross_tenant"],
)
async def test_checkout_provider_customer_prerequisites_are_service_enforced(
    client: AsyncClient,
    admin_token_headers: dict[str, str],
    monkeypatch,
    platform_catalog,
    db_session: AsyncSession,
    provider_call_recorder,
    case: str,
):
    _enable_fake_checkout(monkeypatch)
    if case == "inactive":
        await _seed_provider_customer(db_session, org_id=ADMIN_ORG_ID, provider_code="fake", status="inactive")
    elif case == "wrong_provider":
        await _seed_provider_customer(db_session, org_id=ADMIN_ORG_ID, provider_code="other_fake", status="active")
    elif case == "cross_tenant":
        await _seed_provider_customer(db_session, org_id=OWNER_ORG_ID, provider_code="fake", status="active")

    response = await _post_checkout(
        client,
        admin_token_headers,
        idempotency_key=f"{case.replace('_', '-')}-customer-123456",
        plan_code=platform_catalog["plan_code"],
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "PROVIDER_CUSTOMER_MISSING"
    assert provider_call_recorder == []
    assert await _provider_operation_count(db_session, ADMIN_ORG_ID) == 0


@pytest.mark.asyncio
async def test_checkout_catalog_pricing_is_server_authoritative(
    client: AsyncClient,
    admin_token_headers: dict[str, str],
    monkeypatch,
    platform_catalog,
    fake_customer,
    db_session: AsyncSession,
    provider_call_recorder,
):
    _enable_fake_checkout(monkeypatch)

    await db_session.execute(
        text("SELECT pg_catalog.set_config('app.current_org_id', :org_id, true)"),
        {"org_id": str(ADMIN_ORG_ID)},
    )
    yearly_price = PlatformPrice(
        id=uuid.uuid4(),
        plan_version_id=platform_catalog["plan_id"],
        code=f"TEST-YEAR-{uuid.uuid4().hex[:8].upper()}",
        country_code=None,
        currency_code="USD",
        amount_minor=10000,
        billing_interval="year",
        interval_count=1,
        tax_behavior="exclusive",
        status="active",
        valid_from=datetime.now(timezone.utc) - timedelta(minutes=1),
        published_at=datetime.now(timezone.utc),
    )
    db_session.add(yearly_price)
    await db_session.commit()

    ambiguous = await _post_checkout(
        client,
        admin_token_headers,
        idempotency_key="ambiguous-interval-1234",
        plan_code=platform_catalog["plan_code"],
    )
    assert ambiguous.status_code == 422
    assert ambiguous.json()["detail"]["code"] == "MULTIPLE_PRICES_FOUND"

    response = await _post_checkout(
        client,
        admin_token_headers,
        idempotency_key="interval-month-12345",
        plan_code=platform_catalog["plan_code"],
        billing_interval="month",
    )
    assert response.status_code == 200
    assert provider_call_recorder[-1].amount_minor == 1000
    assert provider_call_recorder[-1].currency_code == "USD"
    assert provider_call_recorder[-1].price_id == platform_catalog["price_id"]


@pytest.mark.asyncio
async def test_checkout_rejects_unpublished_plan_and_non_active_price(
    client: AsyncClient,
    admin_token_headers: dict[str, str],
    monkeypatch,
    platform_catalog,
    fake_customer,
    db_session: AsyncSession,
):
    _enable_fake_checkout(monkeypatch)
    base_plan = await db_session.get(PlatformPlanVersion, platform_catalog["plan_id"])
    assert base_plan is not None

    draft_code = f"DRAFT-PLAN-{uuid.uuid4().hex[:8].upper()}"
    draft_plan = PlatformPlanVersion(
        id=uuid.uuid4(),
        product_id=platform_catalog["product_id"],
        version=99,
        code=draft_code,
        display_name="Draft Plan",
        status="draft",
        dunning_policy_version_id=base_plan.dunning_policy_version_id,
        cancellation_policy_version_id=base_plan.cancellation_policy_version_id,
        downgrade_policy_version_id=base_plan.downgrade_policy_version_id,
    )
    db_session.add(draft_plan)
    db_session.add(
        PlatformPrice(
            id=uuid.uuid4(),
            plan_version_id=draft_plan.id,
            code=f"DRAFT-PRICE-{uuid.uuid4().hex[:8].upper()}",
            country_code=None,
            currency_code="USD",
            amount_minor=4000,
            billing_interval="month",
            interval_count=1,
            tax_behavior="exclusive",
            status="active",
            valid_from=datetime.now(timezone.utc) - timedelta(minutes=1),
            published_at=datetime.now(timezone.utc),
        )
    )

    inactive_code = f"DRAFT-PRICE-PLAN-{uuid.uuid4().hex[:8].upper()}"
    inactive_price_plan = PlatformPlanVersion(
        id=uuid.uuid4(),
        product_id=platform_catalog["product_id"],
        version=100,
        code=inactive_code,
        display_name="Draft Price Plan",
        status="published",
        published_at=datetime.now(timezone.utc),
        dunning_policy_version_id=base_plan.dunning_policy_version_id,
        cancellation_policy_version_id=base_plan.cancellation_policy_version_id,
        downgrade_policy_version_id=base_plan.downgrade_policy_version_id,
    )
    db_session.add(inactive_price_plan)
    db_session.add(
        PlatformPrice(
            id=uuid.uuid4(),
            plan_version_id=inactive_price_plan.id,
            code=f"DRAFT-ONLY-PRICE-{uuid.uuid4().hex[:8].upper()}",
            country_code=None,
            currency_code="USD",
            amount_minor=5000,
            billing_interval="month",
            interval_count=1,
            tax_behavior="exclusive",
            status="draft",
            valid_from=datetime.now(timezone.utc) - timedelta(minutes=1),
            published_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    unpublished = await _post_checkout(
        client,
        admin_token_headers,
        idempotency_key="unpublished-plan-123",
        plan_code=draft_code,
    )
    assert unpublished.status_code == 404
    assert unpublished.json()["detail"]["code"] == "PLAN_NOT_FOUND"

    inactive_price = await _post_checkout(
        client,
        admin_token_headers,
        idempotency_key="inactive-price-1234",
        plan_code=inactive_code,
    )
    assert inactive_price.status_code == 404
    assert inactive_price.json()["detail"]["code"] == "PLAN_NOT_FOUND"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["active", "trialing", "cancel_scheduled"])
async def test_checkout_initial_purchase_rejects_existing_current_subscription_before_provider_work(
    client: AsyncClient,
    admin_token_headers: dict[str, str],
    monkeypatch,
    platform_catalog,
    fake_customer,
    db_session: AsyncSession,
    provider_call_recorder,
    status: str,
):
    _enable_fake_checkout(monkeypatch)
    await _seed_current_subscription(db_session, status=status, platform_catalog=platform_catalog)

    response = await _post_checkout(
        client,
        admin_token_headers,
        idempotency_key=f"current-sub-{status.replace('_', '-')}-12345",
        plan_code=platform_catalog["plan2_code"],
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "VERSIONED_FLOW_REQUIRED"
    assert provider_call_recorder == []
    assert await _provider_operation_count(db_session, ADMIN_ORG_ID) == 0


@pytest.mark.asyncio
async def test_checkout_success_replay_conflict_and_get_are_tenant_scoped(
    client: AsyncClient,
    admin_token_headers: dict[str, str],
    owner_token_headers: dict[str, str],
    monkeypatch,
    platform_catalog,
    fake_customer,
):
    _enable_fake_checkout(monkeypatch)
    key = "test-idem-key-12345678"
    response = await _post_checkout(client, admin_token_headers, idempotency_key=key, plan_code=platform_catalog["plan_code"])
    assert response.status_code == 200
    data = response.json()
    assert data["operation_status"] == "succeeded"
    assert data["checkout_session_reference"]
    assert data["replayed"] is False
    assert data["browser_authoritative"] is False

    replay = await _post_checkout(client, admin_token_headers, idempotency_key=key, plan_code=platform_catalog["plan_code"])
    assert replay.status_code == 200
    replay_data = replay.json()
    assert replay_data["replayed"] is True
    assert replay_data["operation_id"] == data["operation_id"]
    assert replay_data["checkout_session_reference"] == data["checkout_session_reference"]

    conflict = await _post_checkout(client, admin_token_headers, idempotency_key=key, plan_code=platform_catalog["plan2_code"])
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "IDEMPOTENCY_REQUEST_CONFLICT"

    cross_tenant_read = await client.get(
        f"/api/v1/platform-billing/checkout-operations/{data['operation_id']}",
        headers=owner_token_headers,
    )
    assert cross_tenant_read.status_code == 404

    owner_create = await _post_checkout(client, owner_token_headers, idempotency_key=key, plan_code=platform_catalog["plan_code"])
    assert owner_create.status_code == 200
    assert owner_create.json()["operation_id"] != data["operation_id"]


@pytest.mark.asyncio
async def test_checkout_transaction_boundaries_prove_no_active_transaction(
    client: AsyncClient, admin_token_headers: dict[str, str], monkeypatch, platform_catalog, fake_customer
):
    _enable_fake_checkout(monkeypatch)

    lock_probe_results: list[bool] = []
    import app.platform_billing.providers.fake as fake_provider

    async def transaction_probe(call_request) -> bool:
        async with app_database.AsyncSessionLocal() as probe_session:
            await probe_session.execute(
                text("SELECT pg_catalog.set_config('app.current_org_id', :org1, true)"),
                {"org1": str(ADMIN_ORG_ID)},
            )
            try:
                await probe_session.execute(
                    text(
                        """
                        SELECT id
                        FROM platform_provider_operations
                        WHERE id = :operation_id
                        FOR UPDATE NOWAIT
                        """
                    ),
                    {"operation_id": call_request.operation_id},
                )
                lock_probe_results.append(True)
            except Exception:
                lock_probe_results.append(False)
        return False

    original_init = fake_provider.DeterministicFakeProvider.__init__

    def mock_init(self, *args, **kwargs):
        kwargs["transaction_probe"] = transaction_probe
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(fake_provider.DeterministicFakeProvider, "__init__", mock_init)

    response = await _post_checkout(
        client,
        admin_token_headers,
        idempotency_key="tx-boundary-key-1234",
        plan_code=platform_catalog["plan_code"],
    )

    assert response.status_code == 200
    assert lock_probe_results == [True]


@pytest.mark.asyncio
async def test_checkout_bearer_token_boundary_uses_central_auth_not_route_decoding(
    client: AsyncClient, admin_token_headers: dict[str, str], monkeypatch, platform_catalog
):
    _enable_fake_checkout(monkeypatch)
    source = inspect.getsource(checkout_api)
    assert "decode_token" not in source
    assert "jwt.decode" not in source

    valid_bearer = await _post_checkout(
        client,
        admin_token_headers,
        idempotency_key="bearer-test-12345678",
        plan_code=platform_catalog["plan_code"],
    )
    assert valid_bearer.status_code == 422
    assert valid_bearer.json()["detail"]["code"] == "PROVIDER_CUSTOMER_MISSING"

    missing = await client.post(
        "/api/v1/platform-billing/checkout-sessions",
        headers={"Idempotency-Key": "bearer-test-12345679"},
        json={"plan_code": platform_catalog["plan_code"]},
    )
    assert missing.status_code == 401

    malformed_scheme = await client.post(
        "/api/v1/platform-billing/checkout-sessions",
        headers={"Authorization": "Token 123", "Idempotency-Key": "bearer-test-12345680"},
        json={"plan_code": platform_catalog["plan_code"]},
    )
    assert malformed_scheme.status_code == 401

    malformed_bearer = await client.post(
        "/api/v1/platform-billing/checkout-sessions",
        headers={"Authorization": "Bearer not-a-jwt", "Idempotency-Key": "bearer-test-12345681"},
        json={"plan_code": platform_catalog["plan_code"]},
    )
    assert malformed_bearer.status_code == 401

    client.cookies.set("access_token", admin_token_headers["Authorization"].replace("Bearer ", ""))
    cookie_only = await client.post(
        "/api/v1/platform-billing/checkout-sessions",
        headers={"Idempotency-Key": "bearer-test-12345682"},
        json={"plan_code": platform_catalog["plan_code"]},
    )
    assert cookie_only.status_code == 401

    client.cookies.set("access_token", "invalid_cookie")
    bearer_plus_unrelated_cookie = await _post_checkout(
        client,
        admin_token_headers,
        idempotency_key="bearer-test-12345683",
        plan_code=platform_catalog["plan_code"],
    )
    assert bearer_plus_unrelated_cookie.status_code == 422
    assert bearer_plus_unrelated_cookie.json()["detail"]["code"] == "PROVIDER_CUSTOMER_MISSING"


@pytest.mark.asyncio
async def test_checkout_capability_denial_is_exact_and_precedes_provider_work(
    client: AsyncClient,
    admin_token_headers: dict[str, str],
    monkeypatch,
    platform_catalog,
    fake_customer,
    provider_call_recorder,
):
    from app.platform_billing.domain.capability_decision import CapabilityDecision
    from app.platform_billing.services import capability_authorization_service as auth_service

    _enable_fake_checkout(monkeypatch)
    monkeypatch.setattr(settings, "PLATFORM_BILLING_SHADOW_RESOLVER", True)
    monkeypatch.setattr(settings, "PLATFORM_BILLING_ENFORCEMENT", True)

    async def deny(self, **kwargs):
        assert kwargs["capability_key"] == "platform_billing.change_plan"
        assert kwargs["operation_class"] == OperationClass.financial.value
        return auth_service.AuthorizationServiceResult(
            decision=CapabilityDecision(
                allowed=False,
                decision_code="PLATFORM_ACCESS_DENIED",
                safe_reason_code="access_mode_denied",
                capability_key="platform_billing.change_plan",
                operation_class=OperationClass.financial.value,
                access_mode="read_only",
                required_feature_key=None,
                entitlement_value=None,
                usage_value=None,
                limit_value=None,
                projection_freshness="fresh",
                fallback_used=False,
                recompute_attempted=False,
                source_subscription_version=1,
                decision_timestamp=datetime.now(timezone.utc),
            )
        )

    monkeypatch.setattr(auth_service.CapabilityAuthorizationService, "authorize", deny)
    response = await _post_checkout(
        client,
        admin_token_headers,
        idempotency_key="cap-denied-1234567",
        plan_code=platform_catalog["plan_code"],
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "PLATFORM_ACCESS_DENIED"
    assert provider_call_recorder == []


@pytest.mark.asyncio
async def test_checkout_get_capability_denial_is_not_masked_by_not_found(
    client: AsyncClient,
    admin_token_headers: dict[str, str],
    monkeypatch,
    platform_catalog,
    fake_customer,
):
    from app.platform_billing.domain.capability_decision import CapabilityDecision
    from app.platform_billing.services import capability_authorization_service as auth_service

    _enable_fake_checkout(monkeypatch)
    create = await _post_checkout(
        client,
        admin_token_headers,
        idempotency_key="get-denial-create-123",
        plan_code=platform_catalog["plan_code"],
    )
    assert create.status_code == 200
    operation_id = create.json()["operation_id"]

    monkeypatch.setattr(settings, "PLATFORM_BILLING_SHADOW_RESOLVER", True)
    monkeypatch.setattr(settings, "PLATFORM_BILLING_ENFORCEMENT", True)

    async def deny(self, **kwargs):
        assert kwargs["capability_key"] == "platform_billing.view"
        assert kwargs["operation_class"] == OperationClass.safe_read.value
        return auth_service.AuthorizationServiceResult(
            decision=CapabilityDecision(
                allowed=False,
                decision_code="PLATFORM_ACCESS_DENIED",
                safe_reason_code="access_mode_denied",
                capability_key="platform_billing.view",
                operation_class=OperationClass.safe_read.value,
                access_mode="blocked",
                required_feature_key=None,
                entitlement_value=None,
                usage_value=None,
                limit_value=None,
                projection_freshness="fresh",
                fallback_used=False,
                recompute_attempted=False,
                source_subscription_version=1,
                decision_timestamp=datetime.now(timezone.utc),
            )
        )

    monkeypatch.setattr(auth_service.CapabilityAuthorizationService, "authorize", deny)
    response = await client.get(
        f"/api/v1/platform-billing/checkout-operations/{operation_id}",
        headers=admin_token_headers,
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "PLATFORM_ACCESS_DENIED"


@pytest.mark.asyncio
@pytest.mark.parametrize("iteration", range(20))
async def test_checkout_concurrent_same_request_has_one_operation_one_provider_call_one_session(
    client: AsyncClient,
    admin_token_headers: dict[str, str],
    monkeypatch,
    platform_catalog,
    fake_customer,
    provider_call_recorder,
    db_session: AsyncSession,
    iteration,
):
    _enable_fake_checkout(monkeypatch)
    idempotency_key = f"concurrent-same-{iteration}-{uuid.uuid4().hex[:16]}"
    tasks = [
        _post_checkout(client, admin_token_headers, idempotency_key=idempotency_key, plan_code=platform_catalog["plan_code"])
        for _ in range(10)
    ]
    responses = await asyncio.gather(*tasks)
    statuses = [response.status_code for response in responses]
    assert statuses == [200] * 10

    payloads = [response.json() for response in responses]
    operation_ids = {payload["operation_id"] for payload in payloads}
    session_refs = {payload["checkout_session_reference"] for payload in payloads}
    assert len(operation_ids) == 1
    assert len(session_refs) == 1
    assert None not in session_refs
    assert sum(1 for payload in payloads if payload["replayed"] is False) == 1
    assert sum(1 for payload in payloads if payload["replayed"] is True) == 9
    assert len(provider_call_recorder) == 1

    rows = await _operation_rows(db_session, org_id=ADMIN_ORG_ID, idempotency_key=idempotency_key)
    assert len(rows) == 1
    assert str(rows[0].id) in operation_ids
    assert rows[0].result_reference in session_refs


@pytest.mark.asyncio
@pytest.mark.parametrize("iteration", range(20))
async def test_checkout_concurrent_conflicting_request_has_one_winner_one_409_and_one_provider_call(
    client: AsyncClient,
    admin_token_headers: dict[str, str],
    monkeypatch,
    platform_catalog,
    fake_customer,
    provider_call_recorder,
    db_session: AsyncSession,
    iteration,
):
    _enable_fake_checkout(monkeypatch)
    idempotency_key = f"concurrent-conflict-{iteration}-{uuid.uuid4().hex[:16]}"
    responses = await asyncio.gather(
        _post_checkout(client, admin_token_headers, idempotency_key=idempotency_key, plan_code=platform_catalog["plan_code"]),
        _post_checkout(client, admin_token_headers, idempotency_key=idempotency_key, plan_code=platform_catalog["plan2_code"]),
    )
    statuses = sorted(response.status_code for response in responses)
    assert statuses == [200, 409]
    assert all(response.status_code != 429 for response in responses)
    assert all(response.status_code != 500 for response in responses)
    conflicts = [response for response in responses if response.status_code == 409]
    assert conflicts[0].json()["detail"]["code"] == "IDEMPOTENCY_REQUEST_CONFLICT"
    assert len(provider_call_recorder) == 1

    rows = await _operation_rows(db_session, org_id=ADMIN_ORG_ID, idempotency_key=idempotency_key)
    assert len(rows) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("iteration", range(20))
async def test_checkout_concurrent_same_key_cross_tenant_creates_independent_operations_and_sessions(
    client: AsyncClient,
    admin_token_headers: dict[str, str],
    owner_token_headers: dict[str, str],
    monkeypatch,
    platform_catalog,
    fake_customer,
    provider_call_recorder,
    db_session: AsyncSession,
    iteration,
):
    _enable_fake_checkout(monkeypatch)
    idempotency_key = f"concurrent-cross-{iteration}-{uuid.uuid4().hex[:16]}"
    tasks = []
    for _ in range(5):
        tasks.append(_post_checkout(client, admin_token_headers, idempotency_key=idempotency_key, plan_code=platform_catalog["plan_code"]))
        tasks.append(_post_checkout(client, owner_token_headers, idempotency_key=idempotency_key, plan_code=platform_catalog["plan_code"]))
    responses = await asyncio.gather(*tasks)
    assert [response.status_code for response in responses] == [200] * 10

    admin_payloads = [responses[index].json() for index in range(0, 10, 2)]
    owner_payloads = [responses[index].json() for index in range(1, 10, 2)]
    admin_operation_ids = {payload["operation_id"] for payload in admin_payloads}
    owner_operation_ids = {payload["operation_id"] for payload in owner_payloads}
    admin_refs = {payload["checkout_session_reference"] for payload in admin_payloads}
    owner_refs = {payload["checkout_session_reference"] for payload in owner_payloads}
    assert len(admin_operation_ids) == 1
    assert len(owner_operation_ids) == 1
    assert admin_operation_ids != owner_operation_ids
    assert len(admin_refs) == 1
    assert len(owner_refs) == 1
    assert admin_refs != owner_refs
    assert len(provider_call_recorder) == 2

    admin_rows = await _operation_rows(db_session, org_id=ADMIN_ORG_ID, idempotency_key=idempotency_key)
    owner_rows = await _operation_rows(db_session, org_id=OWNER_ORG_ID, idempotency_key=idempotency_key)
    assert len(admin_rows) == 1
    assert len(owner_rows) == 1
    assert admin_rows[0].id != owner_rows[0].id
