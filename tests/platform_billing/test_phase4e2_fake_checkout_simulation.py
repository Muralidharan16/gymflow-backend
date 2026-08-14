from __future__ import annotations

import asyncio
import inspect
import base64
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.engine import make_url

from app.core.config import settings
from app.main import app
from app.platform_billing.models.provider import PlatformProviderCustomer, PlatformProviderOperation
from app.platform_billing.models.webhook import PlatformWebhookInbox
from app.platform_billing.domain.capability import OperationClass
from app.platform_billing.domain.capability_decision import CapabilityDecision
from app.platform_billing.providers.fake_checkout_simulation import DeterministicFakeCheckoutOutcomeProducer
from app.platform_billing.services.checkout_simulation import CheckoutSimulationServices, default_simulation_services
from app.platform_billing.services import capability_authorization_service as auth_service
from app.platform_billing.services.webhooks import PlatformWebhookAcceptanceService
from app.platform_billing.webhooks.fake import DeterministicFakeWebhookVerifier, sign_fake_webhook
from app.platform_billing.webhooks.payload_store import InMemoryEncryptedWebhookPayloadStore, LocalEncryptedWebhookPayloadStore
from app.platform_billing.domain.webhooks import WebhookEnvelope, WebhookSignatureInvalid, WebhookTimestampInvalid, WebhookTransportHeaders
from tests.platform_billing.test_phase4e1_fake_checkout_api import (
    ADMIN_ORG_ID,
    OWNER_ORG_ID,
    _cleanup,
    _post_checkout,
    _seed_provider_customer,
    admin_token_headers,
    fake_customer,
    owner_token_headers,
    platform_catalog,
    setup_test_db_override,
)




class CountingFakeCheckoutOutcomeProducer(DeterministicFakeCheckoutOutcomeProducer):
    def __init__(self, *, clock=None, on_generate=None):
        super().__init__(clock=clock)
        self.generate_calls = 0
        self.events = []
        self.on_generate = on_generate

    def generate(self, **kwargs):
        self.generate_calls += 1
        if self.on_generate is not None:
            self.on_generate(kwargs)
        event = super().generate(**kwargs)
        self.events.append(event)
        return event


class CountingPayloadStore(InMemoryEncryptedWebhookPayloadStore):
    @property
    def write_count(self) -> int:
        return len(self.put_calls)


def _install_simulation_services(producer=None, store=None):
    producer = producer or CountingFakeCheckoutOutcomeProducer()
    store = store or CountingPayloadStore()
    services = CheckoutSimulationServices(event_producer=producer, payload_store=store)
    app.dependency_overrides[default_simulation_services] = lambda: services
    return services


def _clear_simulation_services_override() -> None:
    app.dependency_overrides.pop(default_simulation_services, None)


def _event_ids(producer: CountingFakeCheckoutOutcomeProducer) -> set[str]:
    return {event.provider_event_id for event in producer.events}


def _sync_admin_dsn() -> str:
    raw = os.environ.get("TEST_ADMIN_DATABASE_URL") or os.environ.get("DATABASE_URL") or settings.TEST_DATABASE_URL or settings.DATABASE_URL
    url = make_url(raw)
    if url.drivername.startswith("postgresql+"):
        url = url.set(drivername="postgresql")
    return url.render_as_string(hide_password=False)


def _enable_simulation(monkeypatch):
    monkeypatch.setattr(settings, "PLATFORM_BILLING_FAKE_CHECKOUT_ENABLED", True)
    monkeypatch.setattr(settings, "PLATFORM_BILLING_FAKE_CHECKOUT_SIMULATION_ENABLED", True)
    monkeypatch.setattr(settings, "PLATFORM_BILLING_PROVIDER_MODE", "fake")
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")


async def _checkout(client, headers, platform_catalog):
    response = await _post_checkout(
        client,
        headers,
        idempotency_key=f"checkout-e2-{uuid.uuid4().hex[:20]}",
        plan_code=platform_catalog["plan_code"],
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _simulate(client, headers, *, checkout_operation_id, outcome="succeeded", key=None, extra=None):
    payload = {"checkout_operation_id": str(checkout_operation_id), "requested_outcome": outcome}
    if extra:
        payload.update(extra)
    return await client.post(
        "/api/v1/platform-billing/fake-checkout-simulations",
        headers={**headers, "Idempotency-Key": key or f"simulate-e2-{uuid.uuid4().hex[:20]}"},
        json=payload,
    )




async def _seed_checkout_operation(
    db_session: AsyncSession,
    *,
    org_id: uuid.UUID = ADMIN_ORG_ID,
    provider_code: str = "fake",
    operation_type: str = "create_checkout",
    status: str = "succeeded",
    result_reference: str | None = None,
) -> PlatformProviderOperation:
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
        {
            "org_id": str(org_id),
            "name": f"Simulation Org {org_id.hex[:8]}",
            "slug": f"simulation-org-{org_id.hex[:8]}-{uuid.uuid4().hex[:8]}",
        },
    )
    operation = PlatformProviderOperation(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider_code=provider_code,
        operation_type=operation_type,
        idempotency_key=f"seed-checkout-{uuid.uuid4().hex[:20]}",
        canonical_request_sha256="0" * 64,
        status=status,
        attempt_count=1,
        result_reference=result_reference,
        completed_at=datetime.now(timezone.utc) if status in {"succeeded", "failed", "unknown"} else None,
        error_classification="provider_business_failure" if status == "failed" else None,
    )
    db_session.add(operation)
    await db_session.commit()
    return operation


async def _deny_capability(monkeypatch, *, capability_key: str, operation_class: str):
    async def deny(self, **kwargs):
        assert kwargs["capability_key"] == capability_key
        assert kwargs["operation_class"] == operation_class
        return auth_service.AuthorizationServiceResult(
            decision=CapabilityDecision(
                allowed=False,
                decision_code="PLATFORM_ACCESS_DENIED",
                safe_reason_code="access_mode_denied",
                capability_key=capability_key,
                operation_class=operation_class,
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

    monkeypatch.setattr(settings, "PLATFORM_BILLING_SHADOW_RESOLVER", True)
    monkeypatch.setattr(settings, "PLATFORM_BILLING_ENFORCEMENT", True)
    monkeypatch.setattr(auth_service.CapabilityAuthorizationService, "authorize", deny)


async def _set_tenant_context(db_session: AsyncSession, org_id: uuid.UUID) -> None:
    await db_session.execute(
        text("SELECT set_config('app.current_org_id', :org_id, true)"),
        {"org_id": str(org_id)},
    )


async def _operation_count(db_session: AsyncSession, operation_type="confirm_checkout", org_id: uuid.UUID = ADMIN_ORG_ID) -> int:
    await _set_tenant_context(db_session, org_id)
    return await db_session.scalar(select(func.count()).select_from(PlatformProviderOperation).where(PlatformProviderOperation.operation_type == operation_type))


async def _confirm_rows(db_session: AsyncSession, org_id: uuid.UUID = ADMIN_ORG_ID):
    await _set_tenant_context(db_session, org_id)
    result = await db_session.execute(
        select(PlatformProviderOperation).where(
            PlatformProviderOperation.organization_id == org_id,
            PlatformProviderOperation.operation_type == "confirm_checkout",
        )
    )
    return list(result.scalars().all())


async def _inbox_count(db_session: AsyncSession) -> int:
    return await db_session.scalar(select(func.count()).select_from(PlatformWebhookInbox))


def test_simulation_runtime_payload_store_is_durable_and_tests_can_override():
    runtime_services = default_simulation_services()

    assert isinstance(runtime_services.payload_store, LocalEncryptedWebhookPayloadStore)
    assert not isinstance(runtime_services.payload_store, InMemoryEncryptedWebhookPayloadStore)

    test_store = CountingPayloadStore()
    test_services = _install_simulation_services(store=test_store)
    try:
        assert test_services.payload_store is test_store
        assert isinstance(test_services.payload_store, InMemoryEncryptedWebhookPayloadStore)
    finally:
        _clear_simulation_services_override()




@pytest.mark.asyncio
async def test_simulation_bearer_token_boundary_uses_central_auth_not_route_decoding(
    client: AsyncClient, admin_token_headers, monkeypatch, platform_catalog, fake_customer
):
    from app.platform_billing.api import checkout_simulation as simulation_api

    _enable_simulation(monkeypatch)
    source = inspect.getsource(simulation_api)
    assert "decode_token" not in source
    assert "jwt.decode" not in source

    checkout = await _checkout(client, admin_token_headers, platform_catalog)
    valid_bearer = await _simulate(
        client,
        admin_token_headers,
        checkout_operation_id=checkout["operation_id"],
        outcome="pending",
        key="sim-bearer-valid-1234",
    )
    assert valid_bearer.status_code == 200

    body = {"checkout_operation_id": checkout["operation_id"], "requested_outcome": "pending"}
    missing = await client.post(
        "/api/v1/platform-billing/fake-checkout-simulations",
        headers={"Idempotency-Key": "sim-bearer-missing-1"},
        json=body,
    )
    assert missing.status_code == 401

    malformed_scheme = await client.post(
        "/api/v1/platform-billing/fake-checkout-simulations",
        headers={"Authorization": "Token 123", "Idempotency-Key": "sim-bearer-malformed-1"},
        json=body,
    )
    assert malformed_scheme.status_code == 401

    malformed_bearer = await client.post(
        "/api/v1/platform-billing/fake-checkout-simulations",
        headers={"Authorization": "Bearer not-a-jwt", "Idempotency-Key": "sim-bearer-malformed-2"},
        json=body,
    )
    assert malformed_bearer.status_code == 401

    client.cookies.set("access_token", admin_token_headers["Authorization"].replace("Bearer ", ""))
    cookie_only = await client.post(
        "/api/v1/platform-billing/fake-checkout-simulations",
        headers={"Idempotency-Key": "sim-bearer-cookie-1"},
        json=body,
    )
    assert cookie_only.status_code == 401


@pytest.mark.asyncio
async def test_simulation_post_capability_denial_precedes_work_and_role_label_cannot_bypass(
    client: AsyncClient, admin_token_headers, monkeypatch, platform_catalog, fake_customer, db_session: AsyncSession
):
    _enable_simulation(monkeypatch)
    checkout = await _checkout(client, admin_token_headers, platform_catalog)
    await _deny_capability(
        monkeypatch,
        capability_key="platform_billing.change_plan",
        operation_class=OperationClass.financial.value,
    )

    response = await _simulate(
        client,
        admin_token_headers,
        checkout_operation_id=checkout["operation_id"],
        outcome="succeeded",
        key="sim-cap-denied-1234",
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "PLATFORM_ACCESS_DENIED"
    assert await _operation_count(db_session) == 0
    assert await _inbox_count(db_session) == 0


@pytest.mark.asyncio
async def test_simulation_get_capability_denial_is_not_masked_by_not_found(
    client: AsyncClient, admin_token_headers, monkeypatch, platform_catalog, fake_customer
):
    _enable_simulation(monkeypatch)
    checkout = await _checkout(client, admin_token_headers, platform_catalog)
    created = await _simulate(
        client,
        admin_token_headers,
        checkout_operation_id=checkout["operation_id"],
        outcome="pending",
        key="sim-get-cap-create-1",
    )
    assert created.status_code == 200
    operation_id = created.json()["simulation_operation_id"]
    await _deny_capability(
        monkeypatch,
        capability_key="platform_billing.view",
        operation_class=OperationClass.safe_read.value,
    )

    response = await client.get(
        f"/api/v1/platform-billing/fake-checkout-simulations/{operation_id}",
        headers=admin_token_headers,
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "PLATFORM_ACCESS_DENIED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attr", "value"),
    [
        ("PLATFORM_BILLING_FAKE_CHECKOUT_SIMULATION_ENABLED", False),
        ("PLATFORM_BILLING_FAKE_CHECKOUT_ENABLED", False),
        ("ENVIRONMENT", "production"),
        ("ENVIRONMENT", "staging"),
        ("ENVIRONMENT", ""),
        ("ENVIRONMENT", "prodution"),
        ("PLATFORM_BILLING_PROVIDER_MODE", "disabled"),
    ],
)
async def test_simulation_gates_deny_before_any_write(
    client: AsyncClient,
    admin_token_headers,
    monkeypatch,
    platform_catalog,
    fake_customer,
    db_session: AsyncSession,
    attr,
    value,
):
    _enable_simulation(monkeypatch)
    checkout = await _checkout(client, admin_token_headers, platform_catalog)
    monkeypatch.setattr(settings, attr, value)

    response = await _simulate(client, admin_token_headers, checkout_operation_id=checkout["operation_id"])

    assert response.status_code == 404
    assert await _operation_count(db_session) == 0
    assert await _inbox_count(db_session) == 0


@pytest.mark.asyncio
async def test_simulation_success_uses_phase4c_and_preserves_checkout_meaning(
    client: AsyncClient,
    admin_token_headers,
    monkeypatch,
    platform_catalog,
    fake_customer,
    db_session: AsyncSession,
):
    _enable_simulation(monkeypatch)
    checkout = await _checkout(client, admin_token_headers, platform_catalog)

    response = await _simulate(client, admin_token_headers, checkout_operation_id=checkout["operation_id"], outcome="succeeded")

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["outcome_status"] == "outcome_succeeded"
    assert data["browser_authoritative"] is False
    assert data["subscription_activated"] is False
    rows = await _confirm_rows(db_session)
    assert len(rows) == 1
    assert rows[0].status == "succeeded"
    assert rows[0].operation_type == "confirm_checkout"
    assert rows[0].result_reference.startswith("webhook:fake:")
    assert await _inbox_count(db_session) == 1
    inbox = (await db_session.execute(select(PlatformWebhookInbox))).scalar_one()
    assert inbox.encrypted_payload_ref.startswith("file-encrypted://")
    raw_columns = await db_session.execute(
        text(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'platform_webhook_inbox'
              AND column_name IN ('raw_payload', 'raw_body', 'payload')
            """
        )
    )
    assert raw_columns.all() == []
    create = await db_session.get(PlatformProviderOperation, uuid.UUID(checkout["operation_id"]))
    assert create.status == "succeeded"
    assert create.operation_type == "create_checkout"
    assert "payment_id" not in data
    assert "invoice_id" not in data
    assert "refund_id" not in data


@pytest.mark.asyncio
async def test_simulation_pending_stays_nonterminal_and_generates_no_event(
    client, admin_token_headers, monkeypatch, platform_catalog, fake_customer, db_session: AsyncSession
):
    _enable_simulation(monkeypatch)
    checkout = await _checkout(client, admin_token_headers, platform_catalog)

    response = await _simulate(client, admin_token_headers, checkout_operation_id=checkout["operation_id"], outcome="pending")

    assert response.status_code == 200
    assert response.json()["outcome_status"] == "outcome_pending"
    rows = await _confirm_rows(db_session)
    assert len(rows) == 1
    assert rows[0].status == "in_progress"
    assert await _inbox_count(db_session) == 0


@pytest.mark.asyncio
async def test_simulation_failed_transitions_only_confirm_operation(
    client, admin_token_headers, monkeypatch, platform_catalog, fake_customer, db_session: AsyncSession
):
    _enable_simulation(monkeypatch)
    checkout = await _checkout(client, admin_token_headers, platform_catalog)

    response = await _simulate(client, admin_token_headers, checkout_operation_id=checkout["operation_id"], outcome="failed")

    assert response.status_code == 200
    assert response.json()["outcome_status"] == "outcome_failed"
    rows = await _confirm_rows(db_session)
    assert len(rows) == 1
    assert rows[0].status == "failed"
    assert rows[0].error_classification == "provider_webhook_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("field", [
    "organization_id", "tenant_id", "provider_code", "provider_customer_id", "checkout_session_reference",
    "amount", "amount_minor", "currency", "plan", "price", "event_id", "event_timestamp",
    "observed_at", "raw_payload", "signature", "provider_status", "payment_status",
    "subscription_status", "success", "paid", "entitlements", "idempotency_key",
])
async def test_simulation_rejects_browser_authority_fields(
    client, admin_token_headers, monkeypatch, platform_catalog, fake_customer, field
):
    _enable_simulation(monkeypatch)
    checkout = await _checkout(client, admin_token_headers, platform_catalog)

    response = await _simulate(
        client,
        admin_token_headers,
        checkout_operation_id=checkout["operation_id"],
        extra={field: True},
    )

    assert response.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("key,expected", [("", 422), ("a" * 15, 422), ("a" * 16, 200), ("a" * 160, 200), ("a" * 161, 422), ("bad key internal", 422), ("bad$key", 422)])
async def test_simulation_idempotency_key_boundaries(client, admin_token_headers, monkeypatch, platform_catalog, fake_customer, key, expected):
    _enable_simulation(monkeypatch)
    checkout = await _checkout(client, admin_token_headers, platform_catalog)
    headers = {**admin_token_headers}
    if key != "":
        headers["Idempotency-Key"] = key
    else:
        headers["Idempotency-Key"] = ""
    response = await client.post(
        "/api/v1/platform-billing/fake-checkout-simulations",
        headers=headers,
        json={"checkout_operation_id": checkout["operation_id"], "requested_outcome": "pending"},
    )
    assert response.status_code == expected


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_simulation_rejects_body_idempotency_key(client, admin_token_headers, monkeypatch, platform_catalog, fake_customer):
    _enable_simulation(monkeypatch)
    checkout = await _checkout(client, admin_token_headers, platform_catalog)

    response = await _simulate(
        client,
        admin_token_headers,
        checkout_operation_id=checkout["operation_id"],
        outcome="pending",
        key="sim-body-key-123456",
        extra={"idempotency_key": "body-key-must-not-work"},
    )

    assert response.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "provider_code", "operation_type", "status", "result_reference", "expected_status", "expected_code"),
    [
        ("non_create_checkout", "fake", "refund", "succeeded", "fake_session_x", 404, None),
        ("wrong_provider", "stripe", "create_checkout", "succeeded", "fake_session_x", 404, None),
        ("failed_checkout", "fake", "create_checkout", "failed", "fake_session_x", 422, "CHECKOUT_NOT_SUCCEEDED"),
        ("unknown_checkout", "fake", "create_checkout", "unknown", "fake_session_x", 422, "CHECKOUT_NOT_SUCCEEDED"),
        ("missing_session_reference", "fake", "create_checkout", "succeeded", None, 422, "CHECKOUT_SESSION_MISSING"),
    ],
)
async def test_simulation_validates_trusted_checkout_operation_state(
    client,
    admin_token_headers,
    monkeypatch,
    fake_customer,
    db_session: AsyncSession,
    case,
    provider_code,
    operation_type,
    status,
    result_reference,
    expected_status,
    expected_code,
):
    _enable_simulation(monkeypatch)
    checkout = await _seed_checkout_operation(
        db_session,
        provider_code=provider_code,
        operation_type=operation_type,
        status=status,
        result_reference=result_reference,
    )

    response = await _simulate(
        client,
        admin_token_headers,
        checkout_operation_id=checkout.id,
        outcome="succeeded",
        key=f"sim-checkout-state-{case}-{uuid.uuid4().hex[:8]}",
    )

    assert response.status_code == expected_status
    if expected_code is not None:
        assert response.json()["detail"]["code"] == expected_code


@pytest.mark.asyncio
async def test_simulation_unknown_checkout_is_hidden(client, admin_token_headers, monkeypatch, fake_customer):
    _enable_simulation(monkeypatch)

    response = await _simulate(
        client,
        admin_token_headers,
        checkout_operation_id=uuid.uuid4(),
        outcome="succeeded",
        key="sim-unknown-checkout-1",
    )

    assert response.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "org_id", "provider_code", "status"),
    [
        ("missing_customer", None, None, None),
        ("inactive_customer", ADMIN_ORG_ID, "fake", "inactive"),
        ("wrong_provider_customer", ADMIN_ORG_ID, "stripe", "active"),
        ("cross_tenant_customer", OWNER_ORG_ID, "fake", "active"),
    ],
)
async def test_simulation_requires_active_fake_customer_for_request_org(
    client,
    admin_token_headers,
    monkeypatch,
    db_session: AsyncSession,
    case,
    org_id,
    provider_code,
    status,
):
    _enable_simulation(monkeypatch)
    if org_id is not None:
        await _seed_provider_customer(db_session, org_id=org_id, provider_code=provider_code, status=status)
    checkout = await _seed_checkout_operation(
        db_session,
        status="succeeded",
        result_reference=f"fake_session_{uuid.uuid4().hex[:12]}",
    )

    response = await _simulate(
        client,
        admin_token_headers,
        checkout_operation_id=checkout.id,
        outcome="succeeded",
        key=f"sim-customer-{case}-{uuid.uuid4().hex[:8]}",
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "PROVIDER_CUSTOMER_MISSING"


async def test_simulation_same_request_replays_and_different_outcome_conflicts(
    client, admin_token_headers, monkeypatch, platform_catalog, fake_customer, db_session: AsyncSession
):
    _enable_simulation(monkeypatch)
    checkout = await _checkout(client, admin_token_headers, platform_catalog)
    key = "simulation-replay-key-12345"

    first = await _simulate(client, admin_token_headers, checkout_operation_id=checkout["operation_id"], outcome="succeeded", key=key)
    replay = await _simulate(client, admin_token_headers, checkout_operation_id=checkout["operation_id"], outcome="succeeded", key=key)
    conflict = await _simulate(client, admin_token_headers, checkout_operation_id=checkout["operation_id"], outcome="failed", key=key)

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json()["simulation_operation_id"] == first.json()["simulation_operation_id"]
    assert replay.json()["replayed"] is True
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "IDEMPOTENCY_REQUEST_CONFLICT"
    assert len(await _confirm_rows(db_session)) == 1
    assert await _inbox_count(db_session) == 1


@pytest.mark.asyncio
async def test_simulation_cross_tenant_hidden_and_same_key_independent(
    client, admin_token_headers, owner_token_headers, monkeypatch, platform_catalog, fake_customer, db_session: AsyncSession
):
    _enable_simulation(monkeypatch)
    services = _install_simulation_services()
    try:
        admin_checkout = await _checkout(client, admin_token_headers, platform_catalog)
        key = f"simulation-cross-key-{uuid.uuid4().hex[:20]}"

        hidden = await _simulate(client, owner_token_headers, checkout_operation_id=admin_checkout["operation_id"], outcome="succeeded", key=key)
        owner_checkout = await _checkout(client, owner_token_headers, platform_catalog)
        admin = await _simulate(client, admin_token_headers, checkout_operation_id=admin_checkout["operation_id"], outcome="succeeded", key=key)
        owner = await _simulate(client, owner_token_headers, checkout_operation_id=owner_checkout["operation_id"], outcome="succeeded", key=key)
    finally:
        _clear_simulation_services_override()

    assert hidden.status_code == 404
    assert admin.status_code == 200
    assert owner.status_code == 200
    assert admin.json()["simulation_operation_id"] != owner.json()["simulation_operation_id"]
    assert admin.json()["provider_event_reference"] != owner.json()["provider_event_reference"]
    assert len(await _confirm_rows(db_session, ADMIN_ORG_ID)) == 1
    assert len(await _confirm_rows(db_session, OWNER_ORG_ID)) == 1
    assert services.event_producer.generate_calls == 2
    assert len(_event_ids(services.event_producer)) == 2
    assert services.payload_store.write_count == 2
    assert await _inbox_count(db_session) == 2


@pytest.mark.asyncio
async def test_simulation_orchestration_commits_before_event_generation(
    client, admin_token_headers, monkeypatch, platform_catalog, fake_customer
):
    _enable_simulation(monkeypatch)
    sequence: list[str] = []
    boundary = {
        "visible_before_generation": False,
        "nowait_probe_succeeded": False,
        "producer_received_session": False,
    }

    def on_generate(kwargs: dict[str, Any]) -> None:
        sequence.append("generate")
        boundary["producer_received_session"] = any(
            isinstance(value, AsyncSession) or value.__class__.__name__.lower().endswith(("session", "connection"))
            for value in kwargs.values()
        )
        assert not boundary["producer_received_session"]
        with psycopg.connect(_sync_admin_dsn()) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT set_config('app.current_org_id', %s, true)", [str(ADMIN_ORG_ID)])
                cursor.execute(
                    "SELECT status FROM platform_provider_operations WHERE id = %s",
                    [str(kwargs["simulation_operation_id"])],
                )
                row = cursor.fetchone()
                boundary["visible_before_generation"] = row == ("in_progress",)
                cursor.execute(
                    "SELECT id FROM platform_provider_operations WHERE id = %s FOR UPDATE NOWAIT",
                    [str(kwargs["simulation_operation_id"])],
                )
                boundary["nowait_probe_succeeded"] = cursor.fetchone() is not None
                conn.rollback()

    producer = CountingFakeCheckoutOutcomeProducer(on_generate=on_generate)
    services = _install_simulation_services(producer=producer)
    original_accept = PlatformWebhookAcceptanceService.accept

    async def observed_accept(self, envelope):
        sequence.append("accept")
        assert sequence == ["generate", "accept"]
        return await original_accept(self, envelope)

    monkeypatch.setattr(PlatformWebhookAcceptanceService, "accept", observed_accept)
    try:
        checkout = await _checkout(client, admin_token_headers, platform_catalog)
        response = await _simulate(
            client,
            admin_token_headers,
            checkout_operation_id=checkout["operation_id"],
            outcome="succeeded",
            key=f"boundary-{uuid.uuid4().hex[:20]}",
        )
    finally:
        _clear_simulation_services_override()

    assert response.status_code == 200, response.text
    assert sequence == ["generate", "accept"]
    assert boundary["visible_before_generation"] is True
    assert boundary["nowait_probe_succeeded"] is True
    assert boundary["producer_received_session"] is False
    assert services.event_producer.generate_calls == 1
    assert services.payload_store.write_count == 1


@pytest.mark.asyncio
async def test_simulation_get_is_tenant_scoped_and_readable_when_creation_disabled(
    client, admin_token_headers, owner_token_headers, monkeypatch, platform_catalog, fake_customer
):
    _enable_simulation(monkeypatch)
    checkout = await _checkout(client, admin_token_headers, platform_catalog)
    created = await _simulate(client, admin_token_headers, checkout_operation_id=checkout["operation_id"], outcome="succeeded")
    operation_id = created.json()["simulation_operation_id"]
    monkeypatch.setattr(settings, "PLATFORM_BILLING_FAKE_CHECKOUT_SIMULATION_ENABLED", False)

    own = await client.get(f"/api/v1/platform-billing/fake-checkout-simulations/{operation_id}", headers=admin_token_headers)
    cross = await client.get(f"/api/v1/platform-billing/fake-checkout-simulations/{operation_id}", headers=owner_token_headers)

    assert own.status_code == 200
    assert own.json()["outcome_status"] == "outcome_succeeded"
    assert cross.status_code == 404


@pytest.mark.asyncio
async def test_fake_event_exact_byte_signature_validation(monkeypatch):
    producer = DeterministicFakeCheckoutOutcomeProducer(clock=lambda: datetime(2026, 6, 27, tzinfo=timezone.utc))
    raw_event = producer.generate(
        organization_id=ADMIN_ORG_ID,
        checkout_operation_id=uuid.uuid4(),
        checkout_session_reference="fake-session",
        simulation_operation_id=uuid.uuid4(),
        external_operation_ref="fake_confirm_exact",
        provider_customer_ref="cus_fake123",
        requested_outcome="succeeded",
    )
    envelope = WebhookEnvelope(
        provider_code="fake",
        raw_body=raw_event.raw_body,
        headers=WebhookTransportHeaders({"x-fake-timestamp": str(raw_event.event_timestamp), "x-fake-signature": raw_event.signature}),
    )
    verifier = DeterministicFakeWebhookVerifier(now=datetime.fromtimestamp(raw_event.event_timestamp, tz=timezone.utc))
    verified = await verifier.verify(envelope)
    assert verified.provider_event_id == raw_event.provider_event_id

    mutated = raw_event.raw_body[:-1] + (b"}" if raw_event.raw_body[-1:] != b"}" else b" ")
    with pytest.raises(WebhookSignatureInvalid):
        await verifier.verify(WebhookEnvelope(provider_code="fake", raw_body=mutated, headers=envelope.headers))
    with pytest.raises(WebhookSignatureInvalid):
        await verifier.verify(WebhookEnvelope(provider_code="fake", raw_body=raw_event.raw_body, headers=WebhookTransportHeaders({"x-fake-timestamp": str(raw_event.event_timestamp), "x-fake-signature": "v1=bad"})))
    with pytest.raises(WebhookTimestampInvalid):
        await DeterministicFakeWebhookVerifier(now=datetime.fromtimestamp(raw_event.event_timestamp + 999, tz=timezone.utc)).verify(envelope)
    with pytest.raises(WebhookTimestampInvalid):
        await DeterministicFakeWebhookVerifier(now=datetime.fromtimestamp(raw_event.event_timestamp - 999, tz=timezone.utc)).verify(envelope)


@pytest.mark.asyncio
async def test_simulation_concurrent_same_request_one_operation_one_event(client, admin_token_headers, monkeypatch, platform_catalog, fake_customer, db_session: AsyncSession):
    _enable_simulation(monkeypatch)
    services = _install_simulation_services()
    try:
        checkout = await _checkout(client, admin_token_headers, platform_catalog)
        key = f"sim-concurrent-same-{uuid.uuid4().hex[:20]}"
        responses = await asyncio.gather(*[
            _simulate(client, admin_token_headers, checkout_operation_id=checkout["operation_id"], outcome="succeeded", key=key)
            for _ in range(5)
        ])
    finally:
        _clear_simulation_services_override()

    assert [r.status_code for r in responses] == [200] * 5
    bodies = [r.json() for r in responses]
    assert len({body["simulation_operation_id"] for body in bodies}) == 1
    assert len({body["provider_event_reference"] for body in bodies}) == 1
    assert len({body["outcome_status"] for body in bodies}) == 1
    assert all(r.status_code not in {429, 500} for r in responses)
    assert len(await _confirm_rows(db_session)) == 1
    assert services.event_producer.generate_calls == 1
    assert len(_event_ids(services.event_producer)) == 1
    assert services.payload_store.write_count == 1
    assert await _inbox_count(db_session) == 1


@pytest.mark.asyncio
async def test_simulation_concurrent_conflicting_outcomes_one_winner(client, admin_token_headers, monkeypatch, platform_catalog, fake_customer, db_session: AsyncSession):
    _enable_simulation(monkeypatch)
    services = _install_simulation_services()
    try:
        checkout = await _checkout(client, admin_token_headers, platform_catalog)
        key = f"sim-concurrent-conflict-{uuid.uuid4().hex[:20]}"
        responses = await asyncio.gather(
            _simulate(client, admin_token_headers, checkout_operation_id=checkout["operation_id"], outcome="succeeded", key=key),
            _simulate(client, admin_token_headers, checkout_operation_id=checkout["operation_id"], outcome="failed", key=key),
        )
    finally:
        _clear_simulation_services_override()

    assert sorted(r.status_code for r in responses) == [200, 409]
    assert any(r.status_code == 409 and r.json()["detail"]["code"] == "IDEMPOTENCY_REQUEST_CONFLICT" for r in responses)
    assert len(await _confirm_rows(db_session)) == 1
    assert services.event_producer.generate_calls == 1
    assert services.payload_store.write_count == 1
    assert await _inbox_count(db_session) == 1


@pytest.mark.asyncio
async def test_simulation_concurrent_competing_terminal_outcomes_preserve_one(client, admin_token_headers, monkeypatch, platform_catalog, fake_customer, db_session: AsyncSession):
    _enable_simulation(monkeypatch)
    services = _install_simulation_services()
    try:
        checkout = await _checkout(client, admin_token_headers, platform_catalog)
        responses = await asyncio.gather(
            _simulate(client, admin_token_headers, checkout_operation_id=checkout["operation_id"], outcome="succeeded", key=f"terminal-a-{uuid.uuid4().hex[:20]}"),
            _simulate(client, admin_token_headers, checkout_operation_id=checkout["operation_id"], outcome="failed", key=f"terminal-b-{uuid.uuid4().hex[:20]}"),
        )
    finally:
        _clear_simulation_services_override()

    statuses = sorted(r.status_code for r in responses)
    assert statuses == [200, 409]
    rows = await _confirm_rows(db_session)
    assert len(rows) == 1
    assert rows[0].status in {"succeeded", "failed"}
    winning_status = "outcome_succeeded" if rows[0].status == "succeeded" else "outcome_failed"
    assert [r.json()["outcome_status"] for r in responses if r.status_code == 200] == [winning_status]
    assert rows[0].result_reference.startswith("webhook:fake:")
    assert services.event_producer.generate_calls == 1
    assert services.payload_store.write_count == 1
    assert await _inbox_count(db_session) == 1
