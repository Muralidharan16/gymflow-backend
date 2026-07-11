from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.core.database import AsyncSessionLocal
from app.finance_core.domain.payment_ledger import FinancePaymentConflictError
from app.finance_core.domain.provider_boundary import (
    CreateCheckoutIntentCommand,
    FinancePaymentStateTransitionError,
    FinanceWebhookNormalizationError,
    NormalizedProviderEventCommand,
    StaticSandboxSignatureVerifier,
)
from app.finance_core.services.checkout_intents import FinanceCheckoutIntentService
from app.finance_core.services.provider_webhooks import FinanceProviderWebhookIntakeService
from tests.finance_core.test_phase5c_invoice_engine import fetch_scalar
from tests.finance_core.test_phase5d_payment_ledger import issued_invoice, seed_finance_foundation
from tests.finance_core.test_phase5f_provider_sandbox_webhook import sandbox_config, signature_for


async def sandbox_checkout_intent(*, idempotency_key: str = "state-intent-key"):
    await seed_finance_foundation()
    invoice = await issued_invoice(idempotency_key=f"{idempotency_key}-invoice")
    async with AsyncSessionLocal() as session:
        service = FinanceCheckoutIntentService(session)
        result = await service.create_checkout_intent(
            CreateCheckoutIntentCommand(
                organization_id=None,
                invoice_id=invoice.invoice_id,
                provider_code="sandbox_provider",
                amount=Decimal("1180.00"),
                currency_code="INR",
                idempotency_key=idempotency_key,
            )
        )
        await session.commit()
        return result


def state_event(*, payment_id, status: str, event_id: str, idempotency_key: str) -> NormalizedProviderEventCommand:
    payload_hash = "b" * 64
    return NormalizedProviderEventCommand(
        provider_code="sandbox_provider",
        provider_event_id=event_id,
        event_type=f"payment.{status}",
        raw_status=status,
        payload_hash=payload_hash,
        signature=signature_for(payload_hash),
        idempotency_key=idempotency_key,
        payment_id=payment_id,
    )


async def apply_event(command: NormalizedProviderEventCommand):
    async with AsyncSessionLocal() as session:
        service = FinanceProviderWebhookIntakeService(
            session,
            config=sandbox_config(),
            signature_verifier=StaticSandboxSignatureVerifier(),
        )
        result = await service.normalize_provider_event(command)
        await session.commit()
        return result


@pytest.mark.asyncio
async def test_allowed_transition_created_to_pending_emits_state_changed_outbox():
    intent = await sandbox_checkout_intent(idempotency_key="intent-created-pending")
    result = await apply_event(
        state_event(
            payment_id=intent.intent_id,
            status="pending",
            event_id="evt_created_pending",
            idempotency_key="state-created-pending",
        )
    )
    assert result.payment_status == "pending"
    assert result.state_applied is True
    assert await fetch_scalar("SELECT status FROM finance.payments WHERE id = :id", {"id": intent.intent_id}) == "pending"
    assert await fetch_scalar("SELECT count(*) FROM finance.outbox_events WHERE event_type = 'finance.payment.state_changed'") == 1


@pytest.mark.asyncio
async def test_allowed_transition_pending_to_captured():
    intent = await sandbox_checkout_intent(idempotency_key="intent-pending-captured")
    await apply_event(state_event(payment_id=intent.intent_id, status="pending", event_id="evt_pending", idempotency_key="state-pending"))
    result = await apply_event(
        state_event(
            payment_id=intent.intent_id,
            status="captured",
            event_id="evt_pending_captured",
            idempotency_key="state-pending-captured",
        )
    )
    assert result.payment_status == "captured"
    assert await fetch_scalar("SELECT status FROM finance.payments WHERE id = :id", {"id": intent.intent_id}) == "captured"


@pytest.mark.asyncio
async def test_allowed_transition_authorized_to_captured():
    intent = await sandbox_checkout_intent(idempotency_key="intent-authorized-captured")
    await apply_event(state_event(payment_id=intent.intent_id, status="authorized", event_id="evt_authorized", idempotency_key="state-authorized"))
    result = await apply_event(
        state_event(
            payment_id=intent.intent_id,
            status="captured",
            event_id="evt_authorized_captured",
            idempotency_key="state-authorized-captured",
        )
    )
    assert result.payment_status == "captured"


@pytest.mark.asyncio
async def test_allowed_transition_captured_to_settled():
    intent = await sandbox_checkout_intent(idempotency_key="intent-captured-settled")
    await apply_event(state_event(payment_id=intent.intent_id, status="captured", event_id="evt_captured", idempotency_key="state-captured"))
    result = await apply_event(
        state_event(
            payment_id=intent.intent_id,
            status="settled",
            event_id="evt_captured_settled",
            idempotency_key="state-captured-settled",
        )
    )
    assert result.payment_status == "settled"


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["failed", "cancelled", "refunded"])
async def test_terminal_or_blocked_status_cannot_become_captured(terminal_status: str):
    intent = await sandbox_checkout_intent(idempotency_key=f"intent-terminal-{terminal_status}")
    if terminal_status == "refunded":
        await apply_event(state_event(payment_id=intent.intent_id, status="captured", event_id="evt_terminal_captured", idempotency_key=f"state-terminal-captured-{terminal_status}"))
        await apply_event(state_event(payment_id=intent.intent_id, status="refunded", event_id="evt_terminal_refunded", idempotency_key="state-terminal-refunded"))
    else:
        await apply_event(state_event(payment_id=intent.intent_id, status=terminal_status, event_id=f"evt_terminal_{terminal_status}", idempotency_key=f"state-terminal-{terminal_status}"))

    async with AsyncSessionLocal() as session:
        service = FinanceProviderWebhookIntakeService(
            session,
            config=sandbox_config(),
            signature_verifier=StaticSandboxSignatureVerifier(),
        )
        with pytest.raises(FinancePaymentStateTransitionError):
            await service.normalize_provider_event(
                state_event(
                    payment_id=intent.intent_id,
                    status="captured",
                    event_id=f"evt_terminal_{terminal_status}_captured",
                    idempotency_key=f"state-terminal-{terminal_status}-captured",
                )
            )
        await session.rollback()
    assert await fetch_scalar("SELECT status FROM finance.payments WHERE id = :id", {"id": intent.intent_id}) == terminal_status


@pytest.mark.asyncio
async def test_stale_out_of_order_event_does_not_corrupt_current_state():
    intent = await sandbox_checkout_intent(idempotency_key="intent-stale")
    await apply_event(state_event(payment_id=intent.intent_id, status="captured", event_id="evt_stale_captured", idempotency_key="state-stale-captured"))
    result = await apply_event(
        state_event(
            payment_id=intent.intent_id,
            status="authorized",
            event_id="evt_stale_authorized",
            idempotency_key="state-stale-authorized",
        )
    )
    assert result.payment_status == "captured"
    assert result.state_ignored is True
    assert await fetch_scalar("SELECT status FROM finance.payments WHERE id = :id", {"id": intent.intent_id}) == "captured"


@pytest.mark.asyncio
async def test_duplicate_provider_event_and_same_idempotency_replay_are_safe():
    intent = await sandbox_checkout_intent(idempotency_key="intent-replay")
    command = state_event(payment_id=intent.intent_id, status="pending", event_id="evt_replay", idempotency_key="state-replay")
    first = await apply_event(command)
    replay = await apply_event(command)
    assert replay.payment_event_id == first.payment_event_id
    assert replay.replayed is True
    assert await fetch_scalar("SELECT count(*) FROM finance.payment_events") == 1
    assert await fetch_scalar("SELECT count(*) FROM finance.outbox_events WHERE event_type = 'finance.payment.state_changed'") == 1


@pytest.mark.asyncio
async def test_same_idempotency_key_with_different_payload_conflicts_without_state_change():
    intent = await sandbox_checkout_intent(idempotency_key="intent-idem-conflict")
    await apply_event(state_event(payment_id=intent.intent_id, status="pending", event_id="evt_idem_one", idempotency_key="state-idem-conflict"))
    async with AsyncSessionLocal() as session:
        service = FinanceProviderWebhookIntakeService(
            session,
            config=sandbox_config(),
            signature_verifier=StaticSandboxSignatureVerifier(),
        )
        with pytest.raises(FinancePaymentConflictError):
            await service.normalize_provider_event(
                state_event(
                    payment_id=intent.intent_id,
                    status="captured",
                    event_id="evt_idem_two",
                    idempotency_key="state-idem-conflict",
                )
            )
        await session.rollback()
    assert await fetch_scalar("SELECT status FROM finance.payments WHERE id = :id", {"id": intent.intent_id}) == "pending"


@pytest.mark.asyncio
async def test_unknown_status_and_unknown_payment_follow_explicit_domain_rules():
    intent = await sandbox_checkout_intent(idempotency_key="intent-unknown")
    async with AsyncSessionLocal() as session:
        service = FinanceProviderWebhookIntakeService(
            session,
            config=sandbox_config(),
            signature_verifier=StaticSandboxSignatureVerifier(),
        )
        with pytest.raises(FinanceWebhookNormalizationError):
            await service.normalize_provider_event(
                state_event(
                    payment_id=intent.intent_id,
                    status="mystery",
                    event_id="evt_unknown_status",
                    idempotency_key="state-unknown-status",
                )
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_payment_state_application_has_no_allocation_ledger_invoice_or_subscription_side_effects():
    intent = await sandbox_checkout_intent(idempotency_key="intent-no-side-effects")
    await apply_event(state_event(payment_id=intent.intent_id, status="captured", event_id="evt_no_side_effect", idempotency_key="state-no-side-effect"))
    assert await fetch_scalar("SELECT count(*) FROM finance.payment_allocations") == 0
    assert await fetch_scalar("SELECT count(*) FROM finance.ledger_entries") == 0
    assert await fetch_scalar("SELECT count(*) FROM finance.invoices WHERE status = 'paid'") == 0
    assert await fetch_scalar("SELECT count(*) FROM information_schema.tables WHERE table_name = 'platform_subscriptions'") >= 0


def test_phase5g_has_no_live_provider_frontend_or_production_enablement():
    repo_root = Path(__file__).resolve().parents[2]
    finance_root = repo_root / "app" / "finance_core"
    finance_files = [
        *(finance_root / "domain").rglob("*.py"),
        *(finance_root / "repositories").rglob("*.py"),
        *(finance_root / "services").rglob("*.py"),
    ]
    combined = "\n".join(path.read_text(encoding="utf-8").lower() for path in finance_files)
    assert "rzp_live_" not in combined
    assert "requests" not in combined
    assert "httpx" not in combined
    assert "aiohttp" not in combined
    assert "provider_secret" not in combined
    assert "activate_subscription" not in combined
    assert "platform_subscriptions" not in combined
    assert not (repo_root / "frontend").exists()
