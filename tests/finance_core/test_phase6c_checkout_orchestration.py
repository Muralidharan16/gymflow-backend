from __future__ import annotations

import uuid
from dataclasses import fields
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.database import AsyncSessionLocal
from app.finance_core.domain.checkout_orchestration import (
    CheckoutPlanSelector,
    CreateCheckoutSessionCommand,
    FinanceCheckoutBillingIntervalError,
    FinanceCheckoutPlanNotFoundError,
    ResolvedCheckoutPlan,
)
from app.finance_core.domain.invoice_engine import InvoiceLineInput
from app.finance_core.domain.operational_guards import FinanceOperationalGuardError, FinanceOperationalPosture
from app.finance_core.domain.razorpay_sandbox import RazorpayOrderCreateRequest, RazorpayOrderCreateResponse
from app.finance_core.services.checkout_orchestration import FinanceCheckoutOrchestrationService
from app.finance_core.services.operational_guards import FinanceOperationalGuardService
from app.finance_core.services.razorpay_sandbox import RazorpaySandboxAdapter
from tests.finance_core.test_phase5c_invoice_engine import (
    BILLING_PARTY_ID,
    BRAND_ID,
    DIVISION_ID,
    GST_REGISTRATION_ID,
    LEGAL_ENTITY_ID,
    fetch_one,
    fetch_scalar,
    seed_master_data,
)
from tests.finance_core.test_phase6b_razorpay_sandbox_adapter import sandbox_config


class FakePlanResolver:
    async def resolve_plan(self, selector: CheckoutPlanSelector) -> ResolvedCheckoutPlan:
        if selector.plan_code != "DOERS_PRO_MONTHLY":
            raise FinanceCheckoutPlanNotFoundError("Unknown checkout plan")
        if selector.billing_interval != "monthly":
            raise FinanceCheckoutBillingIntervalError("Unsupported billing interval")
        return ResolvedCheckoutPlan(
            plan_code=selector.plan_code,
            billing_interval=selector.billing_interval,
            legal_entity_id=LEGAL_ENTITY_ID,
            gst_registration_id=GST_REGISTRATION_ID,
            division_id=DIVISION_ID,
            brand_id=BRAND_ID,
            currency_code="INR",
            supply_date=date(2024, 4, 1),
            line_items=(
                InvoiceLineInput(
                    description="Doers Pro monthly subscription",
                    quantity=Decimal("1"),
                    unit_price=Decimal("1000.00"),
                    hsn_sac="998313",
                    gst_rate_basis_points=1800,
                    pricing_mode="tax_exclusive",
                ),
            ),
        )


class FakeRazorpayClient:
    def __init__(self):
        self.requests: list[RazorpayOrderCreateRequest] = []

    async def create_order(self, request: RazorpayOrderCreateRequest) -> RazorpayOrderCreateResponse:
        self.requests.append(request)
        return RazorpayOrderCreateResponse(
            order_id=f"order_test_{len(self.requests)}",
            amount_subunits=request.amount_subunits,
            currency_code=request.currency_code,
            receipt=request.receipt,
            status="created",
        )


def command(*, idempotency_key: str = "phase6c-checkout-key") -> CreateCheckoutSessionCommand:
    return CreateCheckoutSessionCommand(
        organization_id=None,
        billing_party_id=BILLING_PARTY_ID,
        selector=CheckoutPlanSelector(plan_code="DOERS_PRO_MONTHLY", billing_interval="monthly"),
        idempotency_key=idempotency_key,
    )


async def orchestrate(command_: CreateCheckoutSessionCommand, *, client: FakeRazorpayClient | None = None, guard=None):
    client = client or FakeRazorpayClient()
    async with AsyncSessionLocal() as session:
        service = FinanceCheckoutOrchestrationService(
            session,
            plan_resolver=FakePlanResolver(),
            razorpay_adapter=RazorpaySandboxAdapter(
                config=sandbox_config(),
                client=client,
                guard_service=guard,
            ),
        )
        result = await service.create_checkout_session(command_)
        await session.commit()
        return result, client


@pytest.mark.asyncio
async def test_safe_plan_selector_creates_issued_invoice_checkout_intent_and_sandbox_order():
    await seed_master_data()
    result, client = await orchestrate(command())

    assert result.display_amount == Decimal("1180.00")
    assert result.display_currency == "INR"
    assert result.provider_order_id == "order_test_1"
    assert result.checkout_fields == {"key": "rzp_test_key_id", "order_id": "order_test_1"}
    assert len(client.requests) == 1

    invoice = await fetch_one("SELECT status, official_invoice_number, grand_total_amount FROM finance.invoices WHERE id = :id", {"id": result.finance_invoice_id})
    assert invoice["status"] == "issued"
    assert invoice["official_invoice_number"] is not None
    assert invoice["grand_total_amount"] == Decimal("1180.00")

    payment = await fetch_one(
        """
        SELECT status, provider_code, provider_order_ref, amount, currency_code
        FROM finance.payments
        WHERE id = :id
        """,
        {"id": result.finance_checkout_intent_id},
    )
    assert payment["status"] == "created"
    assert payment["provider_code"] == "razorpay_sandbox"
    assert payment["provider_order_ref"] == "order_test_1"
    assert payment["amount"] == Decimal("1180.00")
    assert payment["currency_code"] == "INR"


def test_browser_client_cannot_supply_financial_or_provider_authority_by_dto_design():
    command_fields = {field.name for field in fields(CreateCheckoutSessionCommand)}
    selector_fields = {field.name for field in fields(CheckoutPlanSelector)}

    forbidden = {
        "amount",
        "currency",
        "tax_amount",
        "gst_split",
        "provider_customer_id",
        "provider_payment_id",
        "provider_order_id",
        "invoice_number",
        "legal_entity_id",
        "division_id",
        "brand_id",
        "activate_subscription",
        "mark_invoice_paid",
    }
    assert forbidden.isdisjoint(command_fields)
    assert selector_fields == {"plan_code", "billing_interval"}
    with pytest.raises(TypeError):
        CreateCheckoutSessionCommand(  # type: ignore[call-arg]
            organization_id=None,
            billing_party_id=BILLING_PARTY_ID,
            selector=CheckoutPlanSelector(plan_code="DOERS_PRO_MONTHLY", billing_interval="monthly"),
            idempotency_key="unsafe",
            amount=Decimal("1.00"),
        )


@pytest.mark.asyncio
async def test_unknown_plan_and_unsupported_interval_fail_safely():
    await seed_master_data()

    with pytest.raises(FinanceCheckoutPlanNotFoundError):
        await orchestrate(
            CreateCheckoutSessionCommand(
                organization_id=None,
                billing_party_id=BILLING_PARTY_ID,
                selector=CheckoutPlanSelector(plan_code="UNKNOWN", billing_interval="monthly"),
                idempotency_key="unknown-plan",
            )
        )
    with pytest.raises(FinanceCheckoutBillingIntervalError):
        await orchestrate(
            CreateCheckoutSessionCommand(
                organization_id=None,
                billing_party_id=BILLING_PARTY_ID,
                selector=CheckoutPlanSelector(plan_code="DOERS_PRO_MONTHLY", billing_interval="yearly"),
                idempotency_key="bad-interval",
            )
        )


@pytest.mark.asyncio
async def test_live_provider_posture_blocks_before_adapter_client_call():
    await seed_master_data()
    client = FakeRazorpayClient()
    guard = FinanceOperationalGuardService(FinanceOperationalPosture(live_provider_enabled=True))

    with pytest.raises(FinanceOperationalGuardError):
        await orchestrate(command(idempotency_key="live-blocked"), client=client, guard=guard)
    assert client.requests == []


@pytest.mark.asyncio
async def test_idempotent_replay_does_not_create_duplicate_invoices_checkout_intents_or_orders():
    await seed_master_data()
    client = FakeRazorpayClient()

    first, _ = await orchestrate(command(idempotency_key="orchestration-replay"), client=client)
    replay, _ = await orchestrate(command(idempotency_key="orchestration-replay"), client=client)

    assert replay.replayed is True
    assert replay.finance_invoice_id == first.finance_invoice_id
    assert replay.finance_checkout_intent_id == first.finance_checkout_intent_id
    assert replay.provider_order_id == first.provider_order_id
    assert len(client.requests) == 1
    assert await fetch_scalar("SELECT count(*) FROM finance.invoices") == 1
    assert await fetch_scalar("SELECT count(*) FROM finance.payments") == 1


@pytest.mark.asyncio
async def test_checkout_orchestration_has_no_capture_allocation_ledger_paid_invoice_or_subscription_side_effects():
    await seed_master_data()
    await orchestrate(command(idempotency_key="no-side-effects"))

    assert await fetch_scalar("SELECT count(*) FROM finance.payments WHERE status = 'captured'") == 0
    assert await fetch_scalar("SELECT count(*) FROM finance.payment_allocations") == 0
    assert await fetch_scalar("SELECT count(*) FROM finance.ledger_entries") == 0
    assert await fetch_scalar("SELECT count(*) FROM finance.invoices WHERE status = 'paid'") == 0
    assert await fetch_scalar("SELECT count(*) FROM information_schema.tables WHERE table_name = 'platform_subscriptions'") >= 0


def test_phase6c_has_no_public_api_frontend_webhook_network_or_subscription_behavior():
    repo_root = Path(__file__).resolve().parents[2]
    finance_root = repo_root / "app" / "finance_core"
    combined = "\n".join(path.read_text(encoding="utf-8").lower() for path in finance_root.rglob("*.py"))

    assert "finance_payment_api_enabled = false" in combined
    assert "require_finance_payment_api_enabled" in combined
    assert "webhook" not in (repo_root / "app" / "finance_core" / "services" / "checkout_orchestration.py").read_text(encoding="utf-8").lower()
    assert "requests" not in combined
    assert "httpx" not in combined
    assert "aiohttp" not in combined
    assert "urllib" not in combined
    assert "rzp_live_" not in combined
    assert "provider_secret" not in combined
    assert "activate_subscription" not in combined
    assert "deactivate_subscription" not in combined
    assert "platform_subscriptions" not in combined
    assert not (repo_root / "frontend").exists()
