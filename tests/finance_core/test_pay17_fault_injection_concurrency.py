from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.database import AsyncSessionLocal, update_session_context
from app.finance_core.domain.payment_ledger import (
    CreateRefundIntentCommand,
    ReconcilePaymentSettlementCommand,
)
from app.finance_core.domain.provider_capture_confirmation import (
    FinanceProviderEvidenceDeferredError,
)
from app.finance_core.domain.provider_boundary import (
    FinanceWebhookNormalizationError,
)
from app.finance_core.services.checkout_orchestration import (
    FinanceCheckoutOrchestrationService,
)
from app.finance_core.services.payment_ledger import FinancePaymentLedgerService
from app.finance_core.services.provider_operations import (
    FinanceProviderOperationService,
    provider_checkout_success_hash,
)
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


@asynccontextmanager
async def _reconciliation_session():
    raw = os.environ.get("PAY17_RECON_DATABASE_URL")
    if not raw:
        pytest.skip(
            "PAY17_RECON_DATABASE_URL is required for reduced reconciliation identity proof"
        )
    runtime_raw = os.environ.get("FINANCE_CORE_TEST_DATABASE_URL")
    if not runtime_raw:
        raise RuntimeError("FINANCE_CORE_TEST_DATABASE_URL is required")
    recon = make_url(raw)
    runtime = make_url(runtime_raw)
    if recon.database != runtime.database or "test" not in str(recon.database or "").lower():
        raise RuntimeError("PAY-17 reconciliation URL must target the Finance disposable test database")
    if recon.username != "pay17_recon_test":
        raise RuntimeError("PAY-17 reconciliation proof requires pay17_recon_test")
    engine = create_async_engine(
        raw,
        poolclass=NullPool,
        pool_pre_ping=True,
        echo=False,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await session.execute(
                text(
                    "SELECT pg_catalog.set_config("
                    "'app.current_org_id',:organization_id,true)"
                ),
                {"organization_id": str(ORG_ID)},
            )
            yield session
    finally:
        await engine.dispose()


async def _expire_provider_operation(operation_id: uuid.UUID) -> None:
    async with finance_admin_session() as admin:
        await admin.execute(
            text(
                """
                UPDATE finance.provider_operations
                   SET lease_until=pg_catalog.clock_timestamp()-interval '1 second'
                 WHERE id=:operation_id
                """
            ),
            {"operation_id": operation_id},
        )
        await admin.commit()


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

    db_slots = asyncio.Semaphore(20)

    async def record_one():
        async with db_slots:
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
        async with db_slots:
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


@pytest.mark.asyncio
async def test_death_after_claim_before_provider_call_becomes_unknown_then_reconciles_not_found():
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
            command(idempotency_key="pay17-before-provider-call")
        )
        await session.commit()
        assert prepared.provider_operation is not None
        first_owner = uuid.uuid4()
        first_claim = await service.claim_provider_operation(
            prepared,
            lease_owner=first_owner,
        )
        await session.commit()
        assert first_claim.claimed is True

    # Process dies here: the provider was never called, but the database cannot
    # safely infer that fact from an abandoned in-flight lease.
    assert provider_client.requests == []
    await _expire_provider_operation(prepared.provider_operation.operation_id)

    async with AsyncSessionLocal() as session:
        await update_session_context(session, org_id=str(ORG_ID))
        operation_service = FinanceProviderOperationService(session)
        second_claim = await operation_service.claim(
            operation_id=prepared.provider_operation.operation_id,
            lease_owner=uuid.uuid4(),
        )
        await session.commit()

    assert second_claim.claimed is False
    assert second_claim.status == "unknown"
    assert provider_client.requests == []

    async with _reconciliation_session() as session:
        operation_id, status_value, provider_object_id = (
            await FinanceProviderOperationService(session).reconcile_unknown(
                operation_id=prepared.provider_operation.operation_id,
                outcome="failed_final",
                provider_object_id=None,
                error_code="provider_object_not_found",
                evidence_sha256="a" * 64,
            )
        )
        await session.commit()

    assert operation_id == prepared.provider_operation.operation_id
    assert status_value == "failed_final"
    assert provider_object_id is None
    assert await fetch_scalar(
        "SELECT count(*) FROM finance.provider_operations "
        "WHERE id=:id AND status='failed_final'",
        {"id": prepared.provider_operation.operation_id},
    ) == 1
    assert provider_client.requests == []


@pytest.mark.asyncio
async def test_provider_success_then_process_death_reconciles_without_second_provider_call():
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
            command(idempotency_key="pay17-provider-success-crash")
        )
        await session.commit()
        assert prepared.provider_operation is not None
        first_owner = uuid.uuid4()
        first_claim = await service.claim_provider_operation(
            prepared,
            lease_owner=first_owner,
        )
        await session.commit()
        assert first_claim.claimed is True
        response = await service.call_provider(prepared)

    # Provider succeeded, then the process dies before local acknowledgement.
    assert len(provider_client.requests) == 1
    assert await fetch_scalar(
        "SELECT provider_order_ref FROM finance.payments WHERE id=:id",
        {"id": prepared.finance_checkout_intent_id},
    ) != response.provider_order_ref

    await _expire_provider_operation(prepared.provider_operation.operation_id)
    async with AsyncSessionLocal() as session:
        await update_session_context(session, org_id=str(ORG_ID))
        second_claim = await FinanceProviderOperationService(session).claim(
            operation_id=prepared.provider_operation.operation_id,
            lease_owner=uuid.uuid4(),
        )
        await session.commit()

    assert second_claim.claimed is False
    assert second_claim.status == "unknown"
    assert len(provider_client.requests) == 1

    async with _reconciliation_session() as session:
        operation_id, status_value, provider_object_id = (
            await FinanceProviderOperationService(session).reconcile_unknown(
                operation_id=prepared.provider_operation.operation_id,
                outcome="succeeded",
                provider_object_id=response.provider_order_ref,
                error_code=None,
                evidence_sha256=provider_checkout_success_hash(response),
            )
        )
        await session.commit()

    assert operation_id == prepared.provider_operation.operation_id
    assert status_value == "succeeded"
    assert provider_object_id == response.provider_order_ref
    assert await fetch_scalar(
        "SELECT provider_order_ref FROM finance.payments WHERE id=:id",
        {"id": prepared.finance_checkout_intent_id},
    ) == response.provider_order_ref

    # Replaying checkout returns the reconciled provider object and performs no
    # second external create.
    async with AsyncSessionLocal() as session:
        await update_session_context(session, org_id=str(ORG_ID))
        replay_service = FinanceCheckoutOrchestrationService(
            session,
            plan_resolver=FakePlanResolver(),
            razorpay_adapter=RazorpaySandboxAdapter(
                config=sandbox_config(),
                client=provider_client,
            ),
        )
        replay = await replay_service.prepare_checkout_session(
            command(idempotency_key="pay17-provider-success-crash")
        )
        await session.commit()

    assert replay.provider_order_id == response.provider_order_ref
    assert len(provider_client.requests) == 1


@pytest.mark.asyncio
async def test_provider_clock_skew_outside_window_is_rejected_before_durable_inbox_write():
    checkout = await _seed_finished_checkout(
        idempotency_key="pay17-provider-clock-skew"
    )
    payload = json.loads(
        razorpay_payload(
            event_id="evt_pay17_clock_skew",
            event_type="payment.captured",
            payment_id="pay_pay17_clock_skew",
            order_id=checkout.provider_order_id,
            status="captured",
        ).decode("utf-8")
    )
    payload["created_at"] = int(time.time()) + 301
    raw = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    async with AsyncSessionLocal() as session:
        service = _webhook_service(session)
        with pytest.raises(FinanceWebhookNormalizationError):
            await service.record_verified_webhook(
                signed_webhook(
                    raw,
                    idempotency_key="pay17-provider-clock-skew",
                )
            )
        await session.rollback()

    assert await fetch_scalar(
        "SELECT count(*) FROM finance.provider_webhook_inbox "
        "WHERE provider_event_id='evt_pay17_clock_skew'"
    ) == 0


def test_finance_lease_and_timeout_authority_uses_database_clock_not_process_clock():
    repo_root = __import__("pathlib").Path(__file__).resolve().parents[2]
    migration = (
        repo_root
        / "alembic"
        / "versions"
        / "zr07d8e9f0a52_pay8_durable_checkout_webhooks.py"
    ).read_text(encoding="utf-8")
    assert "pg_catalog.clock_timestamp()+interval '30 seconds'" in migration
    assert "lease_until<=pg_catalog.clock_timestamp()" in migration
    assert "datetime.now(" not in migration


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
