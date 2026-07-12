from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.finance_core.domain.invoice_engine import FinanceInvoiceStateError
from app.finance_core.domain.operational_guards import FinanceOperationalGuardError, FinanceOperationalPosture
from app.finance_core.domain.payment_application_gate import (
    ApplyConfirmedPaymentCommand,
    FinancePaymentApplicationAuthorityError,
)
from app.finance_core.domain.payment_ledger import FinancePaymentConflictError, FinancePaymentStateError
from app.finance_core.services.operational_guards import FinanceOperationalGuardService
from app.finance_core.services.payment_application_gate import FinancePaymentApplicationGateService
from tests.finance_core.test_phase5c_invoice_engine import LEGAL_ENTITY_ID, create_draft, draft_command, fetch_scalar
from tests.finance_core.test_phase5d_payment_ledger import LEDGER_ACCOUNTS, issued_invoice, payment_command, record_payment, seed_finance_foundation
from tests.finance_core.test_phase6d_razorpay_webhook_normalization import (
    confirm,
    razorpay_payload,
    seed_checkout,
    signed_webhook,
)


async def apply_gate(
    payment_id,
    invoice_id,
    *,
    amount: str = "1180.00",
    currency_code: str = "INR",
    idempotency_key: str = "gate-apply-1",
    internal_actor: str = "finance_core",
    reason: str = "explicit internal payment application",
    guard=None,
):
    async with AsyncSessionLocal() as session:
        service = FinancePaymentApplicationGateService(session, guard_service=guard)
        result = await service.apply_confirmed_payment(
            ApplyConfirmedPaymentCommand(
                payment_id=payment_id,
                invoice_id=invoice_id,
                amount=Decimal(amount),
                currency_code=currency_code,
                idempotency_key=idempotency_key,
                internal_actor=internal_actor,
                reason=reason,
            )
        )
        await session.commit()
        return result


async def seed_ledger_accounts_only():
    async with AsyncSessionLocal() as session:
        for code, (name, account_type) in LEDGER_ACCOUNTS.items():
            await session.execute(
                text(
                    """
                    INSERT INTO finance.ledger_accounts (
                        legal_entity_id, code, name, account_type, status
                    )
                    VALUES (:legal_entity_id, :code, :name, :account_type, 'active')
                    ON CONFLICT DO NOTHING
                    """
                ),
                {
                    "legal_entity_id": LEGAL_ENTITY_ID,
                    "code": code,
                    "name": name,
                    "account_type": account_type,
                },
            )
        await session.commit()


@pytest.mark.asyncio
async def test_captured_payment_can_be_explicitly_applied_to_issued_invoice():
    await seed_finance_foundation()
    invoice = await issued_invoice(idempotency_key="invoice-6e-captured")
    payment = await record_payment(payment_command(provider_payment_ref="pay_6e_captured", idempotency_key="pay-6e-captured"))

    result = await apply_gate(payment.payment_id, invoice.invoice_id, idempotency_key="gate-6e-captured")

    assert result.invoice_status == "paid"
    assert result.allocated_amount == Decimal("1180.00")
    assert await fetch_scalar("SELECT status FROM finance.invoices WHERE id = :id", {"id": invoice.invoice_id}) == "paid"
    assert await fetch_scalar("SELECT count(*) FROM finance.payment_allocations") == 1
    assert await fetch_scalar("SELECT count(*) FROM finance.ledger_entries WHERE source_type = 'payment_allocation'") == 1
    assert await fetch_scalar("SELECT count(*) FROM finance.outbox_events WHERE event_type = 'finance.payment.applied'") == 1


@pytest.mark.asyncio
async def test_settled_payment_can_be_explicitly_applied_to_issued_invoice():
    await seed_finance_foundation()
    invoice = await issued_invoice(idempotency_key="invoice-6e-settled")
    payment = await record_payment(
        payment_command(provider_payment_ref="pay_6e_settled", status="settled", idempotency_key="pay-6e-settled")
    )

    result = await apply_gate(payment.payment_id, invoice.invoice_id, idempotency_key="gate-6e-settled")

    assert result.invoice_status == "paid"
    assert await fetch_scalar("SELECT status FROM finance.invoices WHERE id = :id", {"id": invoice.invoice_id}) == "paid"


@pytest.mark.asyncio
async def test_partial_and_full_application_update_invoice_status_through_existing_ledger_service():
    await seed_finance_foundation()
    invoice = await issued_invoice(idempotency_key="invoice-6e-partial")
    first = await record_payment(payment_command(provider_payment_ref="pay_6e_part", amount="500.00", idempotency_key="pay-6e-part"))
    second = await record_payment(
        payment_command(provider_payment_ref="pay_6e_rest", amount="680.00", idempotency_key="pay-6e-rest")
    )

    partial = await apply_gate(first.payment_id, invoice.invoice_id, amount="500.00", idempotency_key="gate-6e-part")
    final = await apply_gate(second.payment_id, invoice.invoice_id, amount="680.00", idempotency_key="gate-6e-rest")

    assert partial.invoice_status == "partially_paid"
    assert final.invoice_status == "paid"
    assert await fetch_scalar("SELECT count(*) FROM finance.payment_allocations") == 2
    assert await fetch_scalar("SELECT count(*) FROM finance.ledger_entries WHERE source_type = 'payment_allocation'") == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["created", "pending", "authorized", "failed", "cancelled", "refunded"])
async def test_ineligible_payment_states_cannot_be_applied(status: str):
    await seed_finance_foundation()
    invoice = await issued_invoice(idempotency_key=f"invoice-6e-block-{status}")
    payment = await record_payment(
        payment_command(provider_payment_ref=f"pay_6e_{status}", status=status, idempotency_key=f"pay-6e-{status}")
    )

    with pytest.raises(FinancePaymentStateError):
        await apply_gate(payment.payment_id, invoice.invoice_id, amount="100.00", idempotency_key=f"gate-6e-{status}")

    assert await fetch_scalar("SELECT count(*) FROM finance.payment_allocations") == 0


@pytest.mark.asyncio
async def test_draft_and_paid_invoice_cannot_be_applied_except_idempotent_replay():
    await seed_finance_foundation()
    draft = await create_draft(draft_command(idempotency_key="draft-6e-blocked"))
    draft_payment = await record_payment(
        payment_command(provider_payment_ref="pay_6e_draft", amount="100.00", idempotency_key="pay-6e-draft")
    )
    with pytest.raises(FinanceInvoiceStateError):
        await apply_gate(draft_payment.payment_id, draft.invoice_id, amount="100.00", idempotency_key="gate-6e-draft")

    invoice = await issued_invoice(idempotency_key="invoice-6e-paid")
    payment = await record_payment(payment_command(provider_payment_ref="pay_6e_paid", idempotency_key="pay-6e-paid"))
    first = await apply_gate(payment.payment_id, invoice.invoice_id, idempotency_key="gate-6e-paid")
    replay = await apply_gate(payment.payment_id, invoice.invoice_id, idempotency_key="gate-6e-paid")

    assert replay.allocation_id == first.allocation_id
    assert replay.replayed is True
    assert await fetch_scalar("SELECT count(*) FROM finance.payment_allocations") == 1

    extra = await record_payment(payment_command(provider_payment_ref="pay_6e_extra", amount="1.00", idempotency_key="pay-6e-extra"))
    with pytest.raises(FinanceInvoiceStateError):
        await apply_gate(extra.payment_id, invoice.invoice_id, amount="1.00", idempotency_key="gate-6e-extra")


@pytest.mark.asyncio
async def test_over_allocation_amount_currency_and_payment_invoice_mismatch_are_rejected():
    await seed_finance_foundation()
    invoice = await issued_invoice(idempotency_key="invoice-6e-validation")
    payment = await record_payment(payment_command(provider_payment_ref="pay_6e_validation", idempotency_key="pay-6e-validation"))

    with pytest.raises(FinancePaymentStateError):
        await apply_gate(payment.payment_id, invoice.invoice_id, amount="1180.01", idempotency_key="gate-6e-too-much")
    with pytest.raises(FinancePaymentStateError):
        await apply_gate(payment.payment_id, invoice.invoice_id, currency_code="USD", idempotency_key="gate-6e-currency")

    mismatched_invoice = await issued_invoice(amount="100.00", idempotency_key="invoice-6e-mismatch")
    with pytest.raises(FinancePaymentStateError):
        await apply_gate(payment.payment_id, mismatched_invoice.invoice_id, amount="100.00", idempotency_key="gate-6e-mismatch")


@pytest.mark.asyncio
async def test_explicit_internal_authority_and_safe_operational_posture_are_required():
    await seed_finance_foundation()
    invoice = await issued_invoice(idempotency_key="invoice-6e-authority")
    payment = await record_payment(payment_command(provider_payment_ref="pay_6e_authority", idempotency_key="pay-6e-authority"))

    with pytest.raises(FinancePaymentApplicationAuthorityError):
        await apply_gate(payment.payment_id, invoice.invoice_id, idempotency_key="gate-6e-browser", internal_actor="browser")
    with pytest.raises(FinancePaymentApplicationAuthorityError):
        await apply_gate(payment.payment_id, invoice.invoice_id, idempotency_key="gate-6e-no-reason", reason=" ")

    guard = FinanceOperationalGuardService(FinanceOperationalPosture(live_provider_enabled=True))
    with pytest.raises(FinanceOperationalGuardError):
        await apply_gate(payment.payment_id, invoice.invoice_id, idempotency_key="gate-6e-guard", guard=guard)

    assert await fetch_scalar("SELECT count(*) FROM finance.payment_allocations") == 0


@pytest.mark.asyncio
async def test_webhook_confirmation_alone_still_does_not_apply_payment_mark_paid_or_post_ledger():
    checkout = await seed_checkout()
    await seed_ledger_accounts_only()
    await confirm(
        signed_webhook(
            razorpay_payload(
                event_id="evt_6e_captured_no_apply",
                event_type="payment.captured",
                payment_id="pay_6e_webhook_only",
                order_id=checkout.provider_order_id,
                status="captured",
            ),
            idempotency_key="evt-6e-captured-no-apply",
        )
    )

    assert await fetch_scalar("SELECT status FROM finance.payments WHERE id = :id", {"id": checkout.finance_checkout_intent_id}) == "captured"
    assert await fetch_scalar("SELECT status FROM finance.invoices WHERE id = :id", {"id": checkout.finance_invoice_id}) == "issued"
    assert await fetch_scalar("SELECT count(*) FROM finance.payment_allocations") == 0
    assert await fetch_scalar("SELECT count(*) FROM finance.ledger_entries WHERE source_type = 'payment_allocation'") == 0
    assert await fetch_scalar("SELECT count(*) FROM finance.outbox_events WHERE event_type = 'finance.invoice.paid'") == 0


@pytest.mark.asyncio
async def test_explicit_gate_can_apply_confirmed_checkout_payment_without_subscription_or_entitlement_side_effects():
    await seed_finance_foundation()
    checkout = await seed_checkout()
    await seed_ledger_accounts_only()
    await confirm(
        signed_webhook(
            razorpay_payload(
                event_id="evt_6e_checkout_captured",
                event_type="payment.captured",
                payment_id="pay_6e_checkout",
                order_id=checkout.provider_order_id,
                status="captured",
            ),
            idempotency_key="evt-6e-checkout-captured",
        )
    )

    result = await apply_gate(
        checkout.finance_checkout_intent_id,
        checkout.finance_invoice_id,
        idempotency_key="gate-6e-checkout",
    )

    assert result.invoice_status == "paid"
    assert await fetch_scalar("SELECT count(*) FROM finance.payment_allocations") == 1
    assert await fetch_scalar("SELECT count(*) FROM finance.ledger_entries WHERE source_type = 'payment_allocation'") == 1
    assert await fetch_scalar("SELECT count(*) FROM finance.outbox_events WHERE event_type = 'finance.invoice.paid'") == 1
    assert await fetch_scalar("SELECT count(*) FROM finance.outbox_events WHERE event_type LIKE 'platform.%'") == 0


def test_phase6e_has_no_public_api_frontend_provider_network_or_subscription_behavior():
    repo_root = Path(__file__).resolve().parents[2]
    finance_root = repo_root / "app" / "finance_core"
    combined = "\n".join(path.read_text(encoding="utf-8").lower() for path in finance_root.rglob("*.py"))

    assert "finance_payment_api_enabled = false" in combined
    assert "require_finance_payment_api_enabled" in combined
    assert "requests" not in combined
    assert "httpx" not in combined
    assert "aiohttp" not in combined
    assert "urllib" not in combined
    assert "rzp_live_" not in combined
    assert "activate_subscription" not in combined
    assert "deactivate_subscription" not in combined
    assert "entitlement_projection" not in combined
    assert not (repo_root / "frontend").exists()
