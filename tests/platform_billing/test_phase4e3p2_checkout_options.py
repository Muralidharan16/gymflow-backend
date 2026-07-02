from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from fastapi import Request
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as app_database
from app.core.config import settings
from app.core.security import create_access_token
from app.main import app
from app.platform_billing.domain.capability import OperationClass
from app.platform_billing.domain.capability_decision import CapabilityDecision
from app.platform_billing.models.billing_account import PlatformBillingAccount
from app.platform_billing.models.catalog import PlatformPlanVersion, PlatformPolicyVersion, PlatformPrice, PlatformProduct
from app.platform_billing.models.provider import PlatformProviderCustomer
from app.platform_billing.models.subscription import PlatformSubscription
from app.platform_billing.services import capability_authorization_service as auth_service
from tests.platform_billing.test_phase1_schema import cleanup_phase1_tables


ADMIN_ORG_ID = uuid.UUID("82000000-0000-0000-0000-00000000e3a1")
OWNER_ORG_ID = uuid.UUID("82000000-0000-0000-0000-00000000e3a2")


@pytest_asyncio.fixture(autouse=True)
async def _cleanup():
    await cleanup_phase1_tables()
    yield
    await cleanup_phase1_tables()


@pytest_asyncio.fixture(autouse=True)
async def setup_test_db_override():
    original_override = app.dependency_overrides.get(app_database.get_db)

    async def custom_override_get_db(request: Request = None):
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
                    trace_id="phase4e3p2-test",
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


@pytest_asyncio.fixture
async def clean_catalog(db_session: AsyncSession) -> dict[str, uuid.UUID | str]:
    return await _seed_catalog(db_session)


@pytest_asyncio.fixture
async def fake_customer(db_session: AsyncSession) -> PlatformProviderCustomer:
    return await _seed_provider_customer(db_session, org_id=ADMIN_ORG_ID)


@pytest.fixture
def provider_call_recorder(monkeypatch):
    from app.platform_billing.providers import fake as fake_provider

    calls = []
    original_execute = fake_provider.DeterministicFakeProvider.execute

    async def wrapped_execute(self, request):
        calls.append(request)
        return await original_execute(self, request)

    monkeypatch.setattr(fake_provider.DeterministicFakeProvider, "execute", wrapped_execute)
    return calls


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


async def _seed_catalog(
    db_session: AsyncSession,
    *,
    plan_code: str | None = None,
    status: str = "published",
    price_status: str = "active",
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
    amount_minor: int = 1000,
    currency_code: str = "USD",
    provider_price_hint: str | None = "internal-provider-hint",
) -> dict[str, uuid.UUID | str]:
    product = PlatformProduct(
        id=uuid.uuid4(),
        code=f"PLATFORM-PRODUCT-{uuid.uuid4().hex[:8].upper()}",
        name="Platform Product",
        description="Doers platform product",
        status="active",
    )
    db_session.add(product)

    dummy_sha = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    policy_version = uuid.uuid4().int % 1000000
    policies = [
        PlatformPolicyVersion(
            id=uuid.uuid4(),
            code=f"{kind.upper()}-{uuid.uuid4().hex[:8].upper()}",
            policy_type=kind,
            version=policy_version,
            status="published",
            published_at=datetime.now(timezone.utc),
            payload_sha256=dummy_sha,
        )
        for kind in ("dunning", "cancellation", "downgrade")
    ]
    db_session.add_all(policies)

    plan = PlatformPlanVersion(
        id=uuid.uuid4(),
        product_id=product.id,
        version=1,
        code=plan_code or f"DOERS-PLAN-{uuid.uuid4().hex[:8].upper()}",
        display_name="Doers Platform Plan",
        description="A display-safe platform plan.",
        status=status,
        published_at=datetime.now(timezone.utc) if status == "published" else None,
        dunning_policy_version_id=policies[0].id,
        cancellation_policy_version_id=policies[1].id,
        downgrade_policy_version_id=policies[2].id,
        metadata_json={"internal": "not exposed"},
    )
    db_session.add(plan)

    price = PlatformPrice(
        id=uuid.uuid4(),
        plan_version_id=plan.id,
        code=f"PLATFORM-PRICE-{uuid.uuid4().hex[:8].upper()}",
        country_code=None,
        currency_code=currency_code,
        amount_minor=amount_minor,
        billing_interval="month",
        interval_count=1,
        tax_behavior="exclusive",
        status=price_status,
        valid_from=valid_from if valid_from is not None else datetime.now(timezone.utc) - timedelta(minutes=1),
        valid_until=valid_until,
        published_at=datetime.now(timezone.utc) if price_status in {"active", "retired"} else None,
        provider_price_hint=provider_price_hint,
    )
    db_session.add(price)
    await db_session.commit()
    return {"plan_id": plan.id, "plan_code": plan.code, "price_id": price.id, "price_code": price.code}


async def _seed_ambiguous_price(db_session: AsyncSession, catalog: dict[str, uuid.UUID | str]) -> None:
    price = PlatformPrice(
        id=uuid.uuid4(),
        plan_version_id=catalog["plan_id"],
        code=f"PLATFORM-PRICE-AMB-{uuid.uuid4().hex[:8].upper()}",
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
    db_session.add(price)
    await db_session.commit()


async def _seed_provider_customer(
    db_session: AsyncSession,
    *,
    org_id: uuid.UUID = ADMIN_ORG_ID,
    status: str = "active",
) -> PlatformProviderCustomer:
    await _seed_organization(db_session, org_id, f"Org {org_id.hex[:8]}")
    customer = PlatformProviderCustomer(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider_code="fake",
        status=status,
        external_customer_ref=f"cus_fake_{uuid.uuid4().hex[:8]}",
    )
    db_session.add(customer)
    await db_session.commit()
    return customer


async def _seed_current_subscription(
    db_session: AsyncSession,
    *,
    status: str,
    catalog: dict[str, uuid.UUID | str],
) -> PlatformSubscription:
    await _seed_organization(db_session, ADMIN_ORG_ID, "Subscription Org")
    account = PlatformBillingAccount(
        id=uuid.uuid4(),
        organization_id=ADMIN_ORG_ID,
        status="active",
        legal_name="Subscription Org",
        billing_email="billing@example.test",
        country_code="US",
        default_currency_code="USD",
        city="Test City",
    )
    db_session.add(account)
    now = datetime.now(timezone.utc)
    sub = PlatformSubscription(
        id=uuid.uuid4(),
        organization_id=ADMIN_ORG_ID,
        billing_account_id=account.id,
        status=status,
        current_plan_version_id=catalog["plan_id"],
        current_price_id=catalog["price_id"],
        policy_snapshot_json={},
        started_at=now - timedelta(days=1),
        current_period_start=now - timedelta(days=1),
        current_period_end=now + timedelta(days=29),
        cancel_at_period_end=status == "cancel_scheduled",
        cancellation_requested_at=now if status == "cancel_scheduled" else None,
        cancellation_effective_at=now + timedelta(days=29) if status == "cancel_scheduled" else None,
        canceled_at=now if status == "canceled" else None,
        ended_at=now if status in {"canceled", "expired"} else None,
    )
    db_session.add(sub)
    await db_session.commit()
    return sub


def _enable_checkout(monkeypatch, *, environment: str = "test") -> None:
    monkeypatch.setattr(settings, "PLATFORM_BILLING_FAKE_CHECKOUT_ENABLED", True)
    monkeypatch.setattr(settings, "PLATFORM_BILLING_PROVIDER_MODE", "fake")
    monkeypatch.setattr(settings, "ENVIRONMENT", environment)


def _enable_simulation(monkeypatch, tmp_path, *, environment: str = "test") -> None:
    _enable_checkout(monkeypatch, environment=environment)
    monkeypatch.setattr(settings, "PLATFORM_BILLING_FAKE_CHECKOUT_SIMULATION_ENABLED", True)
    monkeypatch.setattr(settings, "PLATFORM_BILLING_FAKE_PROVIDER_EVIDENCE_DIR", str(tmp_path / "fake-provider-evidence"))


async def _options(client: AsyncClient, headers: dict[str, str]):
    return await client.get("/api/v1/platform-billing/checkout-options", headers=headers)


async def _post_checkout(client: AsyncClient, headers: dict[str, str], *, plan_code: str, key: str | None = None):
    return await client.post(
        "/api/v1/platform-billing/checkout-sessions",
        headers={**headers, "Idempotency-Key": key or f"phase4e3p2-{uuid.uuid4().hex[:20]}"},
        json={"plan_code": plan_code, "billing_interval": "month"},
    )


async def _deny_capability(monkeypatch, *, capability_key: str):
    async def deny(self, **kwargs):
        allowed = kwargs["capability_key"] != capability_key
        return auth_service.AuthorizationServiceResult(
            decision=CapabilityDecision(
                allowed=allowed,
                decision_code="ALLOWED" if allowed else "PLATFORM_ACCESS_DENIED",
                safe_reason_code="allowed" if allowed else "access_mode_denied",
                capability_key=kwargs["capability_key"],
                operation_class=kwargs["operation_class"],
                access_mode="full" if allowed else "blocked",
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

    monkeypatch.setattr(settings, "PLATFORM_BILLING_SHADOW_RESOLVER", True)
    monkeypatch.setattr(settings, "PLATFORM_BILLING_ENFORCEMENT", True)
    monkeypatch.setattr(auth_service.CapabilityAuthorizationService, "authorize", deny)


@pytest.mark.asyncio
async def test_checkout_options_view_succeeds_and_is_no_store(client, admin_token_headers, clean_catalog, fake_customer, monkeypatch):
    _enable_checkout(monkeypatch)
    response = await _options(client, admin_token_headers)

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["schema_version"] == "platform-billing-checkout-options-v1"
    assert body["checkout_availability"]["available"] is True
    assert body["plans"][0]["plan_code"] == clean_catalog["plan_code"]


@pytest.mark.asyncio
async def test_missing_view_capability_is_denied(client, admin_token_headers, monkeypatch):
    await _deny_capability(monkeypatch, capability_key="platform_billing.view")

    response = await _options(client, admin_token_headers)

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "PLATFORM_ACCESS_DENIED"


@pytest.mark.asyncio
async def test_missing_authentication_is_denied(client):
    response = await client.get("/api/v1/platform-billing/checkout-options")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_view_only_user_sees_catalog_but_action_is_unavailable(client, admin_token_headers, clean_catalog, fake_customer, monkeypatch):
    _enable_checkout(monkeypatch)
    await _deny_capability(monkeypatch, capability_key="platform_billing.change_plan")

    response = await _options(client, admin_token_headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["plans"]
    assert body["checkout_availability"]["reason_code"] == "ACTION_NOT_PERMITTED"
    assert {action["unavailable_reason_code"] for action in body["actions"]} == {"ACTION_NOT_PERMITTED"}


@pytest.mark.asyncio
async def test_role_label_alone_grants_nothing_when_capability_denies(client, admin_token_headers, clean_catalog, fake_customer, monkeypatch):
    _enable_checkout(monkeypatch)
    await _deny_capability(monkeypatch, capability_key="platform_billing.change_plan")

    response = await _options(client, admin_token_headers)

    assert response.status_code == 200
    assert response.json()["checkout_availability"]["available"] is False


@pytest.mark.asyncio
async def test_catalog_response_uses_only_safe_platform_billing_fields(client, admin_token_headers, clean_catalog, fake_customer, monkeypatch):
    _enable_checkout(monkeypatch)

    response = await _options(client, admin_token_headers)

    assert response.status_code == 200, response.text
    payload = json.dumps(response.json(), sort_keys=True)
    assert clean_catalog["plan_code"] in payload
    assert "amount_minor" in payload
    assert "USD" in payload
    forbidden = [
        "provider_price_hint",
        "internal-provider-hint",
        "external_customer_ref",
        "provider secret",
        "webhook secret",
        "raw_event",
        "raw_payload",
        "evidence",
        "reconciliation",
        "member_subscription",
        str(clean_catalog["plan_id"]),
        str(clean_catalog["price_id"]),
    ]
    for token in forbidden:
        assert token not in payload


@pytest.mark.asyncio
async def test_only_published_effective_terms_are_returned(client, admin_token_headers, db_session, fake_customer, monkeypatch):
    _enable_checkout(monkeypatch)
    active = await _seed_catalog(db_session, plan_code="VISIBLE-PLAN")
    await _seed_catalog(db_session, plan_code="DRAFT-PLAN", status="draft")
    await _seed_catalog(db_session, plan_code="RETIRED-PRICE-PLAN", price_status="retired")
    await _seed_catalog(
        db_session,
        plan_code="FUTURE-PRICE-PLAN",
        valid_from=datetime.now(timezone.utc) + timedelta(days=1),
    )

    response = await _options(client, admin_token_headers)

    assert response.status_code == 200
    codes = {plan["plan_code"] for plan in response.json()["plans"]}
    assert codes == {active["plan_code"]}


@pytest.mark.asyncio
async def test_current_plan_is_marked(client, admin_token_headers, clean_catalog, db_session, fake_customer, monkeypatch):
    _enable_checkout(monkeypatch)
    await _seed_current_subscription(db_session, status="active", catalog=clean_catalog)

    response = await _options(client, admin_token_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["current_subscription"]["current_plan_code"] == clean_catalog["plan_code"]
    assert body["plans"][0]["is_current"] is True
    assert body["checkout_availability"]["reason_code"] == "ACTIVE_SUBSCRIPTION_EXISTS"


@pytest.mark.asyncio
async def test_ambiguous_prices_fail_closed(client, admin_token_headers, clean_catalog, db_session, fake_customer, monkeypatch):
    _enable_checkout(monkeypatch)
    await _seed_ambiguous_price(db_session, clean_catalog)

    response = await _options(client, admin_token_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["checkout_availability"]["available"] is False
    assert body["checkout_availability"]["reason_code"] == "CATALOG_PRICE_AMBIGUOUS"


@pytest.mark.asyncio
async def test_catalog_version_is_deterministic(client, admin_token_headers, clean_catalog, fake_customer, monkeypatch):
    _enable_checkout(monkeypatch)

    first = await _options(client, admin_token_headers)
    second = await _options(client, admin_token_headers)

    assert first.status_code == 200
    assert first.json()["catalog_version"] == second.json()["catalog_version"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_value", "reason"),
    [
        ("active", "ACTIVE_SUBSCRIPTION_EXISTS"),
        ("trialing", "TRIAL_SUBSCRIPTION_EXISTS"),
        ("cancel_scheduled", "CANCELLATION_SCHEDULED"),
    ],
)
async def test_subscription_states_block_checkout(
    client, admin_token_headers, clean_catalog, db_session, fake_customer, monkeypatch, status_value, reason
):
    _enable_checkout(monkeypatch)
    await _seed_current_subscription(db_session, status=status_value, catalog=clean_catalog)

    response = await _options(client, admin_token_headers)
    post = await _post_checkout(client, admin_token_headers, plan_code=clean_catalog["plan_code"])

    assert response.status_code == 200
    assert response.json()["checkout_availability"]["reason_code"] == reason
    assert post.status_code == 409


@pytest.mark.asyncio
async def test_terminal_historical_subscription_does_not_block_checkout(client, admin_token_headers, clean_catalog, db_session, fake_customer, monkeypatch):
    _enable_checkout(monkeypatch)
    await _seed_current_subscription(db_session, status="canceled", catalog=clean_catalog)

    response = await _options(client, admin_token_headers)

    assert response.status_code == 200
    assert response.json()["checkout_availability"]["available"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("setup_name", "reason"),
    [
        ("checkout_disabled", "CHECKOUT_FEATURE_DISABLED"),
        ("provider_disabled", "PROVIDER_MODE_UNAVAILABLE"),
        ("production", "ENVIRONMENT_DENIED"),
        ("no_customer", "PROVIDER_CUSTOMER_MISSING"),
        ("no_available_plan", "NO_AVAILABLE_PLANS"),
    ],
)
async def test_eligibility_reason_codes(client, admin_token_headers, db_session, clean_catalog, monkeypatch, setup_name, reason):
    if setup_name != "no_customer":
        await _seed_provider_customer(db_session, org_id=ADMIN_ORG_ID)
    _enable_checkout(monkeypatch)
    if setup_name == "checkout_disabled":
        monkeypatch.setattr(settings, "PLATFORM_BILLING_FAKE_CHECKOUT_ENABLED", False)
    elif setup_name == "provider_disabled":
        monkeypatch.setattr(settings, "PLATFORM_BILLING_PROVIDER_MODE", "disabled")
    elif setup_name == "production":
        monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    elif setup_name == "no_available_plan":
        result = await db_session.execute(select(PlatformPrice))
        for price in result.scalars().all():
            price.status = "retired"
        await db_session.commit()

    response = await _options(client, admin_token_headers)

    assert response.status_code == 200
    assert response.json()["checkout_availability"]["reason_code"] == reason


@pytest.mark.asyncio
async def test_advertised_action_is_accepted_by_checkout_post(
    client, admin_token_headers, clean_catalog, fake_customer, monkeypatch, provider_call_recorder
):
    _enable_checkout(monkeypatch)

    response = await _options(client, admin_token_headers)
    body = response.json()
    action = next(action for action in body["actions"] if action["is_available"])
    post = await _post_checkout(client, admin_token_headers, plan_code=action["target_plan_code"])

    assert response.status_code == 200
    assert post.status_code == 200, post.text
    assert provider_call_recorder[-1].amount_minor == 1000
    assert provider_call_recorder[-1].currency_code == "USD"


@pytest.mark.asyncio
async def test_catalog_change_after_get_does_not_make_displayed_amount_authoritative(
    client, admin_token_headers, clean_catalog, db_session, fake_customer, monkeypatch, provider_call_recorder
):
    _enable_checkout(monkeypatch)

    response = await _options(client, admin_token_headers)
    assert response.json()["plans"][0]["prices"][0]["amount_minor"] == 1000

    price = await db_session.get(PlatformPrice, clean_catalog["price_id"])
    price.status = "retired"
    replacement = PlatformPrice(
        id=uuid.uuid4(),
        plan_version_id=clean_catalog["plan_id"],
        code=f"PLATFORM-PRICE-NEW-{uuid.uuid4().hex[:8].upper()}",
        country_code=None,
        currency_code="USD",
        amount_minor=4321,
        billing_interval="month",
        interval_count=1,
        tax_behavior="exclusive",
        status="active",
        valid_from=datetime.now(timezone.utc) - timedelta(minutes=1),
        published_at=datetime.now(timezone.utc),
    )
    db_session.add(replacement)
    await db_session.commit()

    post = await _post_checkout(client, admin_token_headers, plan_code=clean_catalog["plan_code"])
    assert post.status_code == 200, post.text
    assert provider_call_recorder[-1].amount_minor == 4321


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attr", "value"),
    [
        ("PLATFORM_BILLING_FAKE_CHECKOUT_ENABLED", False),
        ("PLATFORM_BILLING_FAKE_CHECKOUT_SIMULATION_ENABLED", False),
        ("ENVIRONMENT", "production"),
        ("ENVIRONMENT", "staging"),
        ("ENVIRONMENT", ""),
        ("ENVIRONMENT", "prodution"),
        ("PLATFORM_BILLING_PROVIDER_MODE", "disabled"),
    ],
)
async def test_fake_diagnostics_disabled_gates(client, admin_token_headers, clean_catalog, fake_customer, monkeypatch, tmp_path, attr, value):
    _enable_simulation(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, attr, value)

    response = await _options(client, admin_token_headers)

    diagnostic = response.json()["diagnostics"]["fake_checkout_simulation"]
    assert diagnostic["available"] is False
    assert diagnostic["allowed_outcomes"] == []
    assert diagnostic["warning"] == "Development test simulation. No real payment is performed. No subscription is activated."


@pytest.mark.asyncio
async def test_fake_diagnostics_denied_when_evidence_directory_missing(client, admin_token_headers, clean_catalog, fake_customer, monkeypatch):
    _enable_checkout(monkeypatch)
    monkeypatch.setattr(settings, "PLATFORM_BILLING_FAKE_CHECKOUT_SIMULATION_ENABLED", True)
    monkeypatch.setattr(settings, "PLATFORM_BILLING_FAKE_PROVIDER_EVIDENCE_DIR", "")

    response = await _options(client, admin_token_headers)

    assert response.json()["diagnostics"]["fake_checkout_simulation"]["available"] is False


@pytest.mark.asyncio
async def test_fake_diagnostics_denied_when_evidence_directory_unusable(client, admin_token_headers, clean_catalog, fake_customer, monkeypatch, tmp_path):
    blocked = tmp_path / "blocked-file"
    blocked.write_text("not a directory")
    _enable_simulation(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "PLATFORM_BILLING_FAKE_PROVIDER_EVIDENCE_DIR", str(blocked))

    response = await _options(client, admin_token_headers)

    assert response.json()["diagnostics"]["fake_checkout_simulation"]["available"] is False


@pytest.mark.asyncio
async def test_fake_diagnostics_available_in_development_and_test(client, admin_token_headers, clean_catalog, fake_customer, monkeypatch, tmp_path):
    for environment in ("development", "test"):
        _enable_simulation(monkeypatch, tmp_path / environment, environment=environment)
        response = await _options(client, admin_token_headers)
        diagnostic = response.json()["diagnostics"]["fake_checkout_simulation"]
        assert diagnostic["available"] is True
        assert diagnostic["allowed_outcomes"] == ["pending", "succeeded", "failed"]


@pytest.mark.asyncio
async def test_fake_availability_agrees_with_simulation_post(
    client, admin_token_headers, clean_catalog, fake_customer, monkeypatch, tmp_path
):
    _enable_simulation(monkeypatch, tmp_path)
    options = await _options(client, admin_token_headers)
    checkout = await _post_checkout(client, admin_token_headers, plan_code=clean_catalog["plan_code"])

    simulation = await client.post(
        "/api/v1/platform-billing/fake-checkout-simulations",
        headers={**admin_token_headers, "Idempotency-Key": f"phase4e3p2-sim-{uuid.uuid4().hex[:20]}"},
        json={"checkout_operation_id": checkout.json()["operation_id"], "requested_outcome": "succeeded"},
    )

    assert options.json()["diagnostics"]["fake_checkout_simulation"]["available"] is True
    assert simulation.status_code == 200, simulation.text


def test_route_inventory_declares_checkout_options_as_safe_read():
    import yaml
    from pathlib import Path

    inventory = yaml.safe_load(Path("tests/platform_billing/fixtures/phase3_route_inventory.yaml").read_text())
    routes = {
        (route["method"], route["normalized_route_path"]): route
        for route in inventory["migrated_routes"]
    }
    route = routes[("GET", "/api/v1/platform-billing/checkout-options")]
    assert route["proposed_capability"] == "platform_billing.view"
    assert route["operation_class"] == OperationClass.safe_read.value
    assert route["required_entitlement"] is None
    assert route["usage_metric"] is None
