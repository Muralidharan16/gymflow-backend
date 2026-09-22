from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.core.database import AsyncSessionLocal, update_session_context
from app.finance_core.domain.payment_ledger import (
    CreateRefundIntentCommand,
    ReconcilePaymentSettlementCommand,
)
from app.finance_core.domain.provider_capture_confirmation import (
    FinanceProviderEvidenceDeferredError,
)
from app.finance_core.services.checkout_orchestration import (
    FinanceCheckoutOrchestrationService,
)
from app.finance_core.services.payment_ledger import FinancePaymentLedgerService
from app.finance_core.services.razorpay_sandbox import RazorpaySandboxAdapter
from app.finance_core.services.razorpay_webhooks import (
    RazorpayWebhookConfirmationService,
)
from tests.finance_core.admin_database import finance_admin_session
from tests.finance_core.test_phase5c_invoice_engine import (
    ORG_ID,
    fetch_one,
    fetch_scalar,
    seed_master_data,
    set_current_org_context,
)
from tests.finance_core.test_phase5j_refund_credit_note_reversal import (
    paid_invoice_with_payment,
)
from tests.finance_core.test_phase6b_razorpay_sandbox_adapter import sandbox_config
from tests.finance_core.test_phase6c_checkout_orchestration import (
    FakePlanResolver,
    FakeRazorpayClient,
    command,
)
from tests.finance_core.test_phase6d_razorpay_webhook_normalization import (
    PROVIDER_CONFIG,
    razorpay_payload,
    signed_webhook,
)


def _webhook_service(session) -> RazorpayWebhookConfirmationService:
    return RazorpayWebhookConfirmationService(
        session,
        razorpay_config=sandbox_config(
            webhook_secret="rzp_webhook_secret"
        ),
        provider_config=PROVIDER_CONFIG,
    )


@pytest.mark.asyncio
async def test_100_concurrent_identical_callbacks_converge_to_one_financial_effect():
    checkout = await _seed_finished_checkout(
        idempotency_key="pay17-callback-100"
    )
    raw = razorpay_payload(
        event_id="evt_pay17_callback_100",
        event_type="payment.captured",
        payment_id="pay_pay17_callback_100",
        order_id=checkout.provider_order_id,
        status="captured",
    )
    webhook = signed_webhook(
        raw,
        idempotency_key="pay17-callback-100",
    )

    async def record_one():
        async with AsyncSessionLocal() as session:
            receipt = await _webhook_service(
                session
            ).record_verified_webhook(webhook)
            await session.commit()
            return receipt

    receipts = await asyncio.gather(
        *(record_one() for _ in range(100))
    )
    inbox_ids = {receipt.inbox_id for receipt in receipts}
    assert len(inbox_ids) == 1
    assert sum(not receipt.replayed for receipt in receipts) == 1
    assert sum(receipt.replayed for receipt in receipts) == 99
    inbox_id = next(iter(inbox_ids))

    async def claim_one(worker_id: uuid.UUID):
        async with AsyncSessionLocal() as session:
            claimed = await _webhook_service(
                session
            ).claim_recorded_webhook(
                inbox_id=inbox_id,
                lease_owner=worker_id,
            )
            await session.commit()
            return worker_id, claimed

    claims = await asyncio.gather(
        *(claim_one(uuid.uuid4()) for _ in range(100))
    )
    winners = [
        (worker_id, claimed)
        for worker_id, claimed in claims
        if claimed.claimed
    ]
    assert len(winners) == 1
    winner_id, claimed = winners[0]

    async with AsyncSessionLocal() as session:
        service = _webhook_service(session)
        result = await service.process_claimed_webhook(claimed)
        await service.complete_claimed_webhook(
            claimed=claimed,
            lease_owner=winner_id,
            payment_event_id=result.payment_event_id,
        )
        await session.commit()

    assert await fetch_scalar(
        "SELECT count(*) FROM finance.provider_webhook_inbox "
        "WHERE provider_event_id='evt_pay17_callback_100'"
    ) == 1
    assert await fetch_scalar(
        "SELECT count(*) FROM finance.payment_events "
        "WHERE provider_event_id='evt_pay17_callback_100'"
    ) == 1
    assert await fetch_scalar(
        "SELECT count(*) FROM finance.payment_allocations "
        "WHERE payment_id=:payment_id",
        {"payment_id": checkout.finance_checkout_intent_id},
    ) == 1
    assert await fetch_scalar(
        "SELECT count(*) FROM finance.ledger_entries "
        "WHERE source_type='payment_allocation'"
    ) == 1
    assert await fetch_scalar(
        "SELECT status FROM finance.invoices WHERE id=:invoice_id",
        {"invoice_id": checkout.finance_invoice_id},
    ) == "paid"


@pytest.mark.asyncio
async def test_process_rollback_before_commit_is_reclaimable_without_duplicate_money():
    checkout = await _seed_finished_checkout(
        idempotency_key="pay17-rollback-reclaim"
    )
    raw = razorpay_payload(
        event_id="evt_pay17_rollback_reclaim",
        event_type="payment.captured",
        payment_id="pay_pay17_rollback",
        order_id=checkout.provider_order_id,
        status="captured",
    )
    webhook = signed_webhook(
        raw,
        idempotency_key="pay17-rollback-reclaim",
    )

    async with AsyncSessionLocal() as session:
        service = _webhook_service(session)
        receipt = await service.record_verified_webhook(webhook)
        await session.commit()
        first_owner = uuid.uuid4()
        first_claim = await service.claim_recorded_webhook(
            inbox_id=receipt.inbox_id,
            lease_owner=first_owner,
        )
        await session.commit()
        assert first_claim.claimed is True

        # Inject death before the transaction containing payment mutation and
        # inbox completion commits.
        await service.process_claimed_webhook(first_claim)
        await session.rollback()

    assert await fetch_scalar(
        "SELECT count(*) FROM finance.payment_events "
        "WHERE provider_event_id='evt_pay17_rollback_reclaim'"
    ) == 0
    assert await fetch_scalar(
        "SELECT count(*) FROM finance.payment_allocations "
        "WHERE payment_id=:payment_id",
        {"payment_id": checkout.finance_checkout_intent_id},
    ) == 0

    async with finance_admin_session() as admin:
        await admin.execute(
            text(
                """
                UPDATE finance.provider_webhook_inbox
                   SET lease_until=pg_catalog.clock_timestamp()-interval '1 second'
                 WHERE id=:inbox_id
                """
            ),
            {"inbox_id": receipt.inbox_id},
        )
        await admin.commit()

    second_owner = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        service = _webhook_service(session)
        reclaimed = await service.claim_recorded_webhook(
            inbox_id=receipt.inbox_id,
            lease_owner=second_owner,
        )
        await session.commit()
        assert reclaimed.claimed is True
        assert reclaimed.lease_fence == first_claim.lease_fence + 1

        result = await service.process_claimed_webhook(reclaimed)
        await service.complete_claimed_webhook(
            claimed=reclaimed,
            lease_owner=second_owner,
            payment_event_id=result.payment_event_id,
        )
        await session.commit()

    assert await fetch_scalar(
        "SELECT count(*) FROM finance.payment_events "
        "WHERE provider_event_id='evt_pay17_rollback_reclaim'"
    ) == 1
    assert await fetch_scalar(
        "SELECT count(*) FROM finance.payment_allocations "
        "WHERE payment_id=:payment_id",
        {"payment_id": checkout.finance_checkout_intent_id},
    ) == 1
    assert await fetch_scalar(
        "SELECT status FROM finance.provider_webhook_inbox "
        "WHERE id=:inbox_id",
        {"inbox_id": receipt.inbox_id},
    ) == "processed"


@pytest.mark.asyncio
async def test_webhook_before_checkout_response_is_deferred_then_recovers_payment():
    await seed_master_data()
    provider_client = FakeRazorpayClient()
    lease_owner = uuid.uuid4()

    async with AsyncSessionLocal() as checkout_session:
        await update_session_context(
            checkout_session,
            org_id=str(ORG_ID),
        )
        checkout_service = FinanceCheckoutOrchestrationService(
            checkout_session,
            plan_resolver=FakePlanResolver(),
            razorpay_adapter=RazorpaySandboxAdapter(
                config=sandbox_config(),
                client=provider_client,
            ),
        )
        prepared = await checkout_service.prepare_checkout_session(
            command(idempotency_key="pay17-early-webhook")
        )
        await checkout_session.commit()

        assert prepared.provider_operation is not None
        claim = await checkout_service.claim_provider_operation(
            prepared,
            lease_owner=lease_owner,
        )
        await checkout_session.commit()
        provider_response = await checkout_service.call_provider(prepared)
        assert provider_response.provider_order_ref == "order_test_1"

        # Provider has succeeded, but local provider_order_ref acknowledgement
        # has not committed and the HTTP checkout response cannot yet exist.
        assert await fetch_scalar(
            "SELECT provider_order_ref FROM finance.payments WHERE id=:id",
            {"id": prepared.finance_checkout_intent_id},
        ) != provider_response.provider_order_ref

        raw = razorpay_payload(
            event_id="evt_pay17_early_webhook",
            event_type="payment.captured",
            payment_id="pay_pay17_early_webhook",
            order_id=provider_response.provider_order_ref,
            status="captured",
        )
        webhook = signed_webhook(
            raw,
            idempotency_key="pay17-early-webhook-event",
        )

        first_webhook_owner = uuid.uuid4()
        async with AsyncSessionLocal() as webhook_session:
            webhook_service = _webhook_service(webhook_session)
            receipt = await webhook_service.record_verified_webhook(webhook)
            await webhook_session.commit()
            first_claim = await webhook_service.claim_recorded_webhook(
                inbox_id=receipt.inbox_id,
                lease_owner=first_webhook_owner,
            )
            await webhook_session.commit()

            with pytest.raises(FinanceProviderEvidenceDeferredError):
                await webhook_service.process_claimed_webhook(first_claim)
            await webhook_session.rollback()
            retry_status = await webhook_service.fail_claimed_webhook(
                claimed=first_claim,
                lease_owner=first_webhook_owner,
                error_code="provider_binding_pending",
                retryable=True,
            )
            await webhook_session.commit()
            assert retry_status == "retry"

        provider_order_id = await checkout_service.finish_provider_success(
            prepared,
            claim,
            lease_owner=lease_owner,
            response=provider_response,
        )
        await checkout_session.commit()
        assert provider_order_id == provider_response.provider_order_ref

    second_webhook_owner = uuid.uuid4()
    async with AsyncSessionLocal() as webhook_session:
        webhook_service = _webhook_service(webhook_session)
        reclaimed = await webhook_service.claim_recorded_webhook(
            inbox_id=receipt.inbox_id,
            lease_owner=second_webhook_owner,
        )
        await webhook_session.commit()
        assert reclaimed.claimed is True
        result = await webhook_service.process_claimed_webhook(reclaimed)
        await webhook_service.complete_claimed_webhook(
            claimed=reclaimed,
            lease_owner=second_webhook_owner,
            payment_event_id=result.payment_event_id,
        )
        await webhook_session.commit()

    assert await fetch_scalar(
        "SELECT status FROM finance.provider_webhook_inbox WHERE id=:id",
        {"id": receipt.inbox_id},
    ) == "processed"
    assert await fetch_scalar(
        "SELECT count(*) FROM finance.payment_events "
        "WHERE provider_event_id='evt_pay17_early_webhook'"
    ) == 1
    assert await fetch_scalar(
        "SELECT count(*) FROM finance.payment_allocations "
        "WHERE payment_id=:payment_id",
        {"payment_id": prepared.finance_checkout_intent_id},
    ) == 1
    assert await fetch_scalar(
        "SELECT status FROM finance.invoices WHERE id=:invoice_id",
        {"invoice_id": prepared.finance_invoice_id},
    ) == "paid"


@pytest.mark.asyncio
async def test_settlement_and_refund_race_serializes_without_lost_obligation():
    _invoice, payment = await paid_invoice_with_payment(
        invoice_key="pay17-settlement-refund-invoice",
        payment_key="pay17-settlement-refund-payment",
        payment_ref="pay_pay17_settlement_refund",
    )

    async def settlement():
        async with AsyncSessionLocal() as session:
            await set_current_org_context(session)
            result = await FinancePaymentLedgerService(
                session
            ).reconcile_payment_settlement(
                ReconcilePaymentSettlementCommand(
                    payment_id=payment.payment_id,
                    settlement_ref="SETTLE-PAY17-RACE",
                    settlement_amount=Decimal("600.00"),
                    gateway_fee_amount=Decimal("10.00"),
                    idempotency_key="pay17-settlement-race",
                )
            )
            await session.commit()
            return result

    async def refund():
        async with AsyncSessionLocal() as session:
            await set_current_org_context(session)
            result = await FinancePaymentLedgerService(
                session
            ).create_refund_intent(
                CreateRefundIntentCommand(
                    payment_id=payment.payment_id,
                    refund_ref="RF-PAY17-RACE",
                    amount=Decimal("700.00"),
                    reason="PAY-17 concurrent settlement/refund",
                    credit_note_id=None,
                    idempotency_key="pay17-refund-race",
                )
            )
            await session.commit()
            return result

    settlement_result, refund_result = await asyncio.gather(
        settlement(),
        refund(),
    )

    assert settlement_result.settlement_amount == Decimal("600.00")
    assert refund_result.amount == Decimal("700.00")
    assert await fetch_scalar(
        "SELECT count(*) FROM finance.ledger_entries "
        "WHERE source_type='settlement'"
    ) == 1
    assert await fetch_scalar(
        "SELECT count(*) FROM finance.refunds WHERE payment_id=:payment_id",
        {"payment_id": payment.payment_id},
    ) == 1
    assert await fetch_scalar(
        "SELECT coalesce(sum(amount),0) FROM finance.refunds "
        "WHERE payment_id=:payment_id AND status<>'cancelled'",
        {"payment_id": payment.payment_id},
    ) == Decimal("700.00")
    assert await fetch_scalar(
        "SELECT count(*) FROM finance.refund_execution_commands "
        "WHERE payment_id=:payment_id",
        {"payment_id": payment.payment_id},
    ) >= 1


async def _seed_finished_checkout(*, idempotency_key: str):
    await seed_master_data()
    provider_client = FakeRazorpayClient()
    async with AsyncSessionLocal() as session:
        await update_session_context(session, org_id=str(ORG_ID))
        service = FinanceCheckoutOrchestrationService(
            session,
            plan_resolver=FakePlanResolver(),
            razorpay_adapter=RazorpaySandboxAdapter(
                config=sandbox_config(),
                client=provider_client,
            ),
        )
        prepared = await service.prepare_checkout_session(
            command(idempotency_key=idempotency_key)
        )
        await session.commit()
        assert prepared.provider_operation is not None
        lease_owner = uuid.uuid4()
        claim = await service.claim_provider_operation(
            prepared,
            lease_owner=lease_owner,
        )
        await session.commit()
        response = await service.call_provider(prepared)
        provider_order_id = await service.finish_provider_success(
            prepared,
            claim,
            lease_owner=lease_owner,
            response=response,
        )
        await session.commit()
        return service.build_result(
            prepared,
            provider_order_id=provider_order_id,
        )
