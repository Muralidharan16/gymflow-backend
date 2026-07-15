from __future__ import annotations

import asyncio
import hmac
import json
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.finance_core.domain.payment_ledger import FinancePaymentConflictError
from app.finance_core.domain.provider_boundary import FinancePaymentStateTransitionError
from app.finance_core.domain.provider_capture_confirmation import (
    ConfirmProviderPaymentEvidenceCommand,
    FinanceProviderEvidenceError,
)
from app.finance_core.services.provider_capture_confirmation import (
    FinanceProviderCaptureConfirmationService,
)
from tests.finance_core.test_phase5c_invoice_engine import fetch_one, fetch_scalar, seed_master_data
from tests.finance_core.test_phase6aj_checkout_callback_recording import (
    callback_command,
    record as record_callback,
    seed_checkout as seed_callback_checkout,
)
from tests.finance_core.test_phase6c_checkout_orchestration import (
    FakeRazorpayClient,
    command as checkout_command,
    orchestrate,
)
from tests.finance_core.test_phase6d_razorpay_webhook_normalization import seed_checkout
from tests.finance_core.test_phase6p_sandbox_webhook_route_enablement import (
    clear_webhook_dependency_overrides,
    override_webhook_dependencies,
    sandbox_webhook_posture,
)


PROVIDER_CODE = "razorpay_sandbox"
ORDER_REF = "order_test_1"
PAYMENT_REF = "pay_phase6al_1"
AMOUNT_SUBUNITS = 118000
CURRENCY = "INR"
WEBHOOK_SECRET = b"rzp_webhook_secret"


def evidence(
    *,
    event_id: str = "evt_phase6al_1",
    event_type: str = "payment.captured",
    order_ref: str = ORDER_REF,
    payment_ref: str = PAYMENT_REF,
    amount_subunits: int = AMOUNT_SUBUNITS,
    currency: str = CURRENCY,
    payment_status: str | None = None,
    captured: bool | None = None,
    idempotency_key: str = "phase6al-request-1",
    provider_code: str = PROVIDER_CODE,
    signature_verified: bool = True,
    payment_order_ref: str | None = None,
    order_entity_ref: str | None = None,
    order_status: str | None = None,
    timestamp: int | None = 1784100000,
) -> ConfirmProviderPaymentEvidenceCommand:
    target_status = {
        "payment.authorized": "authorized",
        "payment.captured": "captured",
        "payment.failed": "failed",
        "order.paid": "captured",
    }.get(event_type, payment_status or "unknown")
    if captured is None and event_type in {"payment.captured", "order.paid"}:
        captured = True
    if captured is None and event_type in {"payment.authorized", "payment.failed"}:
        captured = False
    if event_type == "order.paid":
        order_entity_ref = order_entity_ref if order_entity_ref is not None else order_ref
        order_status = order_status if order_status is not None else "paid"
    return ConfirmProviderPaymentEvidenceCommand(
        provider_code=provider_code,
        provider_event_id=event_id,
        event_type=event_type,
        provider_order_ref=order_ref,
        provider_payment_ref=payment_ref,
        provider_amount_subunits=amount_subunits,
        provider_currency=currency,
        provider_payment_status=payment_status or target_status,
        provider_captured=captured,
        provider_payment_order_ref=payment_order_ref or order_ref,
        provider_order_entity_ref=order_entity_ref,
        provider_order_status=order_status,
        provider_event_timestamp=timestamp,
        idempotency_key=idempotency_key,
        webhook_signature_verified=signature_verified,
    )


async def seed_payment(*, status: str = "created", bind_payment_ref: bool = True):
    checkout = await seed_checkout()
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                """
                UPDATE finance.payments
                SET status = :payment_status,
                    raw_status = :raw_status,
                    provider_payment_ref = :payment_ref
                WHERE id = :payment_id
                """
            ),
            {
                "payment_status": status,
                "raw_status": status,
                "payment_ref": PAYMENT_REF if bind_payment_ref else None,
                "payment_id": checkout.finance_checkout_intent_id,
            },
        )
        await session.commit()
    return checkout


async def confirm(command_: ConfirmProviderPaymentEvidenceCommand):
    async with AsyncSessionLocal() as session:
        service = FinanceProviderCaptureConfirmationService(session, provider_code=PROVIDER_CODE)
        result = await service.confirm_provider_evidence(command_)
        await session.commit()
        return result


async def mutation_counts() -> dict:
    return await fetch_one(
        """
        SELECT
            (SELECT count(*) FROM finance.payment_events) AS events,
            (SELECT count(*) FROM finance.idempotency_keys
             WHERE scope = 'finance.provider.capture.confirm') AS capture_keys,
            (SELECT count(*) FROM finance.outbox_events
             WHERE event_type = 'finance.payment.state_changed') AS state_events,
            (SELECT count(*) FROM finance.payment_allocations) AS allocations,
            (SELECT count(*) FROM finance.ledger_entries) AS ledger_entries,
            (SELECT count(*) FROM finance.invoices
             WHERE status IN ('paid', 'partially_paid')) AS paid_invoices,
            (SELECT count(*) FROM finance.refunds) AS refunds,
            (SELECT count(*) FROM finance.credit_notes) AS credit_notes
        """
    )


def _route_capture_payload() -> bytes:
    return json.dumps(
        {
            "id": "body_event_id_must_be_ignored",
            "event": "payment.captured",
            "created_at": 1784100000,
            "payload": {
                "payment": {
                    "entity": {
                        "id": PAYMENT_REF,
                        "order_id": ORDER_REF,
                        "amount": AMOUNT_SUBUNITS,
                        "currency": CURRENCY,
                        "status": "captured",
                        "captured": True,
                    }
                }
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _route_order_paid_payload(*, event_id: str = "evt_phase6al_order_paid_route", payload_overrides: dict | None = None) -> bytes:
    payload = {
        "payment": {
            "entity": {
                "id": PAYMENT_REF,
                "order_id": ORDER_REF,
                "amount": AMOUNT_SUBUNITS,
                "currency": CURRENCY,
                "status": "captured",
                "captured": True,
            }
        },
        "order": {
            "entity": {
                "id": ORDER_REF,
                "status": "paid",
            }
        },
    }
    if payload_overrides:
        payload.update(payload_overrides)
    return json.dumps(
        {
            "id": event_id,
            "event": "order.paid",
            "created_at": 1784100000,
            "payload": payload,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _signed_route_headers(raw: bytes, *, event_id: str, idempotency_key: str) -> dict[str, str]:
    return {
        "X-Razorpay-Signature": hmac.digest(WEBHOOK_SECRET, raw, "sha256").hex(),
        "X-Razorpay-Event-Id": event_id,
        "X-Idempotency-Key": idempotency_key,
    }


async def _post_order_paid_route(client, raw: bytes, *, event_id: str, idempotency_key: str):
    override_webhook_dependencies(sandbox_webhook_posture())
    try:
        return await client.post(
            "/api/v1/finance/payments/webhooks/razorpay",
            content=raw,
            headers=_signed_route_headers(raw, event_id=event_id, idempotency_key=idempotency_key),
        )
    finally:
        clear_webhook_dependency_overrides()


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_status", ["created", "pending"])
async def test_authorized_evidence_advances_only_created_or_pending(initial_status: str):
    checkout = await seed_payment(status=initial_status, bind_payment_ref=False)
    result = await confirm(
        evidence(
            event_id=f"evt_phase6al_authorized_{initial_status}",
            event_type="payment.authorized",
            idempotency_key=f"phase6al-authorized-{initial_status}",
        )
    )

    assert result.previous_payment_status == initial_status
    assert result.payment_status == "authorized"
    assert result.state_changed is True
    assert await fetch_scalar(
        "SELECT provider_payment_ref FROM finance.payments WHERE id = :id",
        {"id": checkout.finance_checkout_intent_id},
    ) == PAYMENT_REF


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial_status", "expected_ignored"),
    [("authorized", False), ("captured", True), ("settled", True), ("partially_refunded", True)],
)
async def test_authorized_evidence_never_downgrades_later_states(
    initial_status: str,
    expected_ignored: bool,
):
    await seed_payment(status=initial_status)

    result = await confirm(
        evidence(
            event_id=f"evt_phase6al_authorized_no_downgrade_{initial_status}",
            event_type="payment.authorized",
            idempotency_key=f"phase6al-authorized-no-downgrade-{initial_status}",
        )
    )

    assert result.payment_status == initial_status
    assert result.state_changed is False
    assert result.state_ignored is expected_ignored
    assert await fetch_scalar(
        "SELECT count(*) FROM finance.outbox_events WHERE event_type = 'finance.payment.state_changed'"
    ) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["failed", "cancelled", "refunded"])
async def test_terminal_payment_rejects_authorized_evidence_without_mutation(terminal_status: str):
    await seed_payment(status=terminal_status)
    before = await mutation_counts()

    with pytest.raises(FinancePaymentStateTransitionError):
        await confirm(
            evidence(
                event_id=f"evt_phase6al_authorized_terminal_{terminal_status}",
                event_type="payment.authorized",
                idempotency_key=f"phase6al-authorized-terminal-{terminal_status}",
            )
        )

    assert await mutation_counts() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_status", ["created", "pending", "authorized"])
async def test_complete_captured_evidence_advances_eligible_states(initial_status: str):
    await seed_payment(status=initial_status, bind_payment_ref=initial_status == "authorized")
    result = await confirm(
        evidence(
            event_id=f"evt_phase6al_capture_{initial_status}",
            idempotency_key=f"phase6al-capture-{initial_status}",
        )
    )

    assert result.previous_payment_status == initial_status
    assert result.payment_status == "captured"
    assert result.event_recorded is True
    assert result.state_changed is True
    assert await fetch_scalar(
        "SELECT count(*) FROM finance.outbox_events WHERE event_type = 'finance.payment.state_changed'"
    ) == 1


@pytest.mark.asyncio
async def test_captured_replay_is_noop_and_alternate_request_key_does_not_duplicate():
    await seed_payment(status="captured")
    command_ = evidence(event_id="evt_phase6al_replay", idempotency_key="phase6al-replay-a")

    first = await confirm(command_)
    same_key = await confirm(command_)
    other_key = await confirm(replace(command_, idempotency_key="phase6al-replay-b"))

    assert first.event_recorded is True
    assert first.state_changed is False
    assert same_key.replayed is True
    assert other_key.replayed is True
    assert await fetch_scalar(
        "SELECT count(*) FROM finance.payment_events WHERE provider_event_id = 'evt_phase6al_replay'"
    ) == 1
    assert await fetch_scalar(
        "SELECT count(*) FROM finance.outbox_events WHERE event_type = 'finance.payment.state_changed'"
    ) == 0
    assert await fetch_scalar(
        "SELECT count(*) FROM finance.idempotency_keys WHERE scope = 'finance.provider.capture.confirm'"
    ) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_status", ["settled", "partially_refunded"])
async def test_captured_evidence_is_recorded_as_stale_without_downgrade(initial_status: str):
    await seed_payment(status=initial_status)
    result = await confirm(
        evidence(
            event_id=f"evt_phase6al_stale_{initial_status}",
            idempotency_key=f"phase6al-stale-{initial_status}",
        )
    )

    assert result.payment_status == initial_status
    assert result.event_recorded is True
    assert result.state_changed is False
    assert result.state_ignored is True
    assert await fetch_scalar(
        "SELECT count(*) FROM finance.outbox_events WHERE event_type = 'finance.payment.state_changed'"
    ) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["failed", "cancelled", "refunded"])
async def test_terminal_payment_rejects_captured_evidence_without_mutation(terminal_status: str):
    await seed_payment(status=terminal_status)
    before = await mutation_counts()

    with pytest.raises(FinancePaymentStateTransitionError):
        await confirm(
            evidence(
                event_id=f"evt_phase6al_terminal_{terminal_status}",
                idempotency_key=f"phase6al-terminal-{terminal_status}",
            )
        )

    assert await mutation_counts() == before
    assert await fetch_scalar("SELECT status FROM finance.payments") == terminal_status


@pytest.mark.asyncio
async def test_payment_failed_uses_existing_state_machine_for_eligible_payment():
    await seed_payment(status="created")

    result = await confirm(
        evidence(
            event_id="evt_phase6al_failed",
            event_type="payment.failed",
            idempotency_key="phase6al-failed",
        )
    )

    assert result.payment_status == "failed"
    assert result.state_changed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("protected_status", ["captured", "settled", "partially_refunded", "refunded"])
async def test_payment_failed_does_not_overwrite_protected_state(protected_status: str):
    await seed_payment(status=protected_status)
    before = await mutation_counts()

    with pytest.raises(FinancePaymentStateTransitionError):
        await confirm(
            evidence(
                event_id=f"evt_phase6al_failed_after_{protected_status}",
                event_type="payment.failed",
                idempotency_key=f"phase6al-failed-after-{protected_status}",
            )
        )

    assert await mutation_counts() == before
    assert await fetch_scalar("SELECT status FROM finance.payments") == protected_status


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changes", "error_code"),
    [
        ({"provider_amount_subunits": None}, "PROVIDER_AMOUNT_INVALID"),
        ({"provider_amount_subunits": -1}, "PROVIDER_AMOUNT_INVALID"),
        ({"provider_amount_subunits": 117999}, "PROVIDER_AMOUNT_MISMATCH"),
        ({"provider_currency": ""}, "PROVIDER_CURRENCY_INVALID"),
        ({"provider_currency": "USD"}, "PROVIDER_CURRENCY_MISMATCH"),
        ({"provider_currency": "inr"}, "PROVIDER_CURRENCY_INVALID"),
    ],
)
async def test_amount_and_currency_fail_before_any_database_mutation(changes: dict, error_code: str):
    await seed_payment(status="authorized")
    before = await mutation_counts()
    command_ = replace(evidence(), **changes)

    with pytest.raises(FinanceProviderEvidenceError) as exc:
        await confirm(command_)

    assert exc.value.code == error_code
    assert await mutation_counts() == before
    assert await fetch_scalar("SELECT status FROM finance.payments") == "authorized"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changes", "error_code"),
    [
        ({"provider_event_id": ""}, "PROVIDER_EVENT_ID_INVALID"),
        ({"provider_event_id": "bad event id"}, "PROVIDER_EVENT_ID_INVALID"),
        ({"provider_event_id": "x" * 201}, "PROVIDER_EVENT_ID_INVALID"),
        ({"webhook_signature_verified": False}, "PROVIDER_EVIDENCE_UNVERIFIED"),
        ({"source": "browser"}, "PROVIDER_EVIDENCE_SOURCE_INVALID"),
        ({"idempotency_key": "   "}, "PROVIDER_EVIDENCE_IDEMPOTENCY_INVALID"),
        ({"event_type": "payment.unknown"}, "PROVIDER_EVENT_TYPE_UNSUPPORTED"),
        ({"provider_payment_status": "authorized"}, "PROVIDER_PAYMENT_STATUS_MISMATCH"),
        ({"provider_captured": False}, "PROVIDER_CAPTURED_FLAG_MISMATCH"),
        (
            {
                "event_type": "payment.authorized",
                "provider_payment_status": "authorized",
                "provider_captured": True,
            },
            "PROVIDER_CAPTURED_FLAG_MISMATCH",
        ),
        (
            {
                "event_type": "payment.failed",
                "provider_payment_status": "failed",
                "provider_captured": True,
            },
            "PROVIDER_CAPTURED_FLAG_MISMATCH",
        ),
    ],
)
async def test_untrusted_or_malformed_evidence_is_rejected_without_mutation(changes: dict, error_code: str):
    await seed_payment(status="authorized")
    before = await mutation_counts()

    with pytest.raises(FinanceProviderEvidenceError) as exc:
        await confirm(replace(evidence(), **changes))

    assert exc.value.code == error_code
    assert await mutation_counts() == before


@pytest.mark.asyncio
async def test_unknown_wrong_provider_and_reference_mismatches_fail_closed():
    await seed_payment(status="authorized")
    before = await mutation_counts()
    cases = [
        (
            evidence(
                event_id="evt_phase6al_unknown",
                order_ref="order_unknown",
                payment_ref="pay_unknown",
                payment_order_ref="order_unknown",
                idempotency_key="phase6al-unknown",
            ),
            "PROVIDER_EVIDENCE_PAYMENT_NOT_FOUND",
        ),
        (
            evidence(
                event_id="evt_phase6al_wrong_provider",
                provider_code="other_provider",
                idempotency_key="phase6al-wrong-provider",
            ),
            "PROVIDER_EVIDENCE_PROVIDER_MISMATCH",
        ),
        (
            evidence(
                event_id="evt_phase6al_wrong_order",
                order_ref="order_other",
                payment_order_ref="order_other",
                idempotency_key="phase6al-wrong-order",
            ),
            "PROVIDER_EVIDENCE_ORDER_MISMATCH",
        ),
        (
            evidence(
                event_id="evt_phase6al_wrong_payment",
                payment_ref="pay_other",
                idempotency_key="phase6al-wrong-payment",
            ),
            "PROVIDER_EVIDENCE_PAYMENT_MISMATCH",
        ),
    ]
    for command_, code in cases:
        with pytest.raises(FinanceProviderEvidenceError) as exc:
            await confirm(command_)
        assert exc.value.code == code

    assert await mutation_counts() == before


@pytest.mark.asyncio
async def test_order_and_payment_references_must_identify_the_same_row():
    await seed_master_data()
    client = FakeRazorpayClient()
    first, _ = await orchestrate(checkout_command(idempotency_key="phase6al-ref-first"), client=client)
    second, _ = await orchestrate(checkout_command(idempotency_key="phase6al-ref-second"), client=client)
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                """
                UPDATE finance.payments
                SET status = 'authorized',
                    raw_status = 'authorized',
                    provider_payment_ref = CASE id
                        WHEN :first_id THEN 'pay_phase6al_first'
                        WHEN :second_id THEN 'pay_phase6al_second'
                    END
                WHERE id IN (:first_id, :second_id)
                """
            ),
            {
                "first_id": first.finance_checkout_intent_id,
                "second_id": second.finance_checkout_intent_id,
            },
        )
        await session.commit()

    with pytest.raises(FinanceProviderEvidenceError) as exc:
        await confirm(
            evidence(
                event_id="evt_phase6al_cross_ref",
                order_ref="order_test_1",
                payment_ref="pay_phase6al_second",
                payment_order_ref="order_test_1",
                idempotency_key="phase6al-cross-ref",
            )
        )
    assert exc.value.code == "PROVIDER_EVIDENCE_REFERENCE_MISMATCH"
    assert await fetch_scalar("SELECT count(*) FROM finance.payment_events") == 0


@pytest.mark.asyncio
async def test_duplicate_provider_order_reference_is_rejected_as_ambiguous():
    await seed_master_data()
    client = FakeRazorpayClient()
    first, _ = await orchestrate(checkout_command(idempotency_key="phase6al-ambiguous-first"), client=client)
    second, _ = await orchestrate(checkout_command(idempotency_key="phase6al-ambiguous-second"), client=client)
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                """
                UPDATE finance.payments
                SET status = 'authorized',
                    raw_status = 'authorized',
                    provider_order_ref = :order_ref,
                    provider_payment_ref = CASE id
                        WHEN :first_id THEN 'pay_phase6al_ambiguous_first'
                        WHEN :second_id THEN 'pay_phase6al_ambiguous_second'
                    END
                WHERE id IN (:first_id, :second_id)
                """
            ),
            {
                "order_ref": ORDER_REF,
                "first_id": first.finance_checkout_intent_id,
                "second_id": second.finance_checkout_intent_id,
            },
        )
        await session.commit()
    before = await mutation_counts()

    with pytest.raises(FinanceProviderEvidenceError) as exc:
        await confirm(
            evidence(
                event_id="evt_phase6al_ambiguous_order",
                payment_ref="pay_phase6al_ambiguous_first",
                idempotency_key="phase6al-ambiguous-order",
            )
        )

    assert exc.value.code == "PROVIDER_EVIDENCE_ORDER_AMBIGUOUS"
    assert await mutation_counts() == before


@pytest.mark.asyncio
async def test_order_paid_requires_strict_order_and_payment_entities():
    await seed_payment(status="authorized")
    valid = await confirm(
        evidence(
            event_id="evt_phase6al_order_paid",
            event_type="order.paid",
            idempotency_key="phase6al-order-paid",
        )
    )
    assert valid.payment_status == "captured"

    invalid_cases = [
        replace(
            evidence(
                event_id="evt_phase6al_order_missing_payment",
                event_type="order.paid",
                idempotency_key="phase6al-order-missing-payment",
            ),
            provider_payment_ref="",
        ),
        replace(
            evidence(
                event_id="evt_phase6al_order_payment_order_mismatch",
                event_type="order.paid",
                idempotency_key="phase6al-order-payment-order-mismatch",
            ),
            provider_payment_order_ref="order_other",
        ),
        replace(
            evidence(
                event_id="evt_phase6al_order_missing",
                event_type="order.paid",
                idempotency_key="phase6al-order-missing",
            ),
            provider_order_entity_ref=None,
        ),
        replace(
            evidence(
                event_id="evt_phase6al_order_mismatch",
                event_type="order.paid",
                idempotency_key="phase6al-order-mismatch",
            ),
            provider_order_entity_ref="order_other",
        ),
        replace(
            evidence(
                event_id="evt_phase6al_order_status",
                event_type="order.paid",
                idempotency_key="phase6al-order-status",
            ),
            provider_order_status="attempted",
        ),
    ]
    for command_ in invalid_cases:
        with pytest.raises(FinanceProviderEvidenceError):
            await confirm(command_)

    assert await fetch_scalar("SELECT count(*) FROM finance.payment_events") == 1


@pytest.mark.asyncio
async def test_provider_event_identity_is_primary_and_changed_payload_conflicts():
    await seed_payment(status="authorized")
    first_command = evidence(
        event_id="evt_phase6al_identity",
        idempotency_key="phase6al-identity-a",
    )
    first = await confirm(first_command)
    replay = await confirm(replace(first_command, idempotency_key="phase6al-identity-b"))
    assert first.event_recorded is True
    assert replay.replayed is True

    with pytest.raises(FinancePaymentConflictError):
        await confirm(
            replace(
                first_command,
                provider_event_timestamp=1784100001,
                idempotency_key="phase6al-identity-c",
            )
        )
    with pytest.raises(FinancePaymentConflictError):
        await confirm(
            replace(
                first_command,
                provider_event_id="evt_phase6al_different",
                idempotency_key="phase6al-identity-a",
            )
        )

    assert await fetch_scalar("SELECT count(*) FROM finance.payment_events") == 1
    assert await fetch_scalar(
        "SELECT count(*) FROM finance.outbox_events WHERE event_type = 'finance.payment.state_changed'"
    ) == 1


@pytest.mark.asyncio
async def test_concurrent_duplicate_delivery_serializes_to_one_event_and_state_change():
    await seed_payment(status="authorized")
    base = evidence(event_id="evt_phase6al_concurrent")

    first, second = await asyncio.gather(
        confirm(replace(base, idempotency_key="phase6al-concurrent-a")),
        confirm(replace(base, idempotency_key="phase6al-concurrent-b")),
    )

    assert sorted([first.event_recorded, second.event_recorded]) == [False, True]
    assert sorted([first.replayed, second.replayed]) == [False, True]
    assert await fetch_scalar("SELECT count(*) FROM finance.payment_events") == 1
    assert await fetch_scalar(
        "SELECT count(*) FROM finance.outbox_events WHERE event_type = 'finance.payment.state_changed'"
    ) == 1


@pytest.mark.asyncio
async def test_callback_authorized_then_provider_capture_advances_to_captured():
    await seed_callback_checkout(idempotency_key="phase6al-callback-first")
    await record_callback(callback_command(idempotency_key="phase6al-callback-first-record"))

    captured = await confirm(
        evidence(
            payment_ref="pay_phase6aj_1",
            event_id="evt_phase6al_after_callback",
            idempotency_key="phase6al-after-callback",
        )
    )

    assert captured.previous_payment_status == "authorized"
    assert captured.payment_status == "captured"


@pytest.mark.asyncio
async def test_provider_capture_then_checkout_callback_does_not_downgrade():
    await seed_payment(status="created", bind_payment_ref=False)
    await confirm(
        evidence(
            event_id="evt_phase6al_before_callback",
            idempotency_key="phase6al-before-callback",
        )
    )

    callback = await record_callback(
        callback_command(
            payment_id=PAYMENT_REF,
            idempotency_key="phase6al-callback-after-capture",
        )
    )

    assert callback.payment_status == "captured"
    assert await fetch_scalar("SELECT status FROM finance.payments") == "captured"


@pytest.mark.asyncio
async def test_late_failure_rolls_back_reference_event_state_outbox_and_idempotency():
    checkout = await seed_payment(status="created", bind_payment_ref=False)
    async with AsyncSessionLocal() as session:
        service = FinanceProviderCaptureConfirmationService(session, provider_code=PROVIDER_CODE)

        async def fail_outbox(**_kwargs):
            raise RuntimeError("simulated sanitized outbox failure")

        service._repo.create_outbox_event = fail_outbox
        with pytest.raises(RuntimeError, match="simulated sanitized outbox failure"):
            await service.confirm_provider_evidence(
                evidence(
                    event_id="evt_phase6al_rollback",
                    idempotency_key="phase6al-rollback",
                )
            )
        await session.commit()

    payment = await fetch_one(
        "SELECT status, provider_payment_ref FROM finance.payments WHERE id = :id",
        {"id": checkout.finance_checkout_intent_id},
    )
    assert payment["status"] == "created"
    assert payment["provider_payment_ref"] is None
    assert await fetch_scalar("SELECT count(*) FROM finance.payment_events") == 0
    assert await fetch_scalar(
        "SELECT count(*) FROM finance.idempotency_keys WHERE scope = 'finance.provider.capture.confirm'"
    ) == 0
    assert await fetch_scalar(
        "SELECT count(*) FROM finance.outbox_events WHERE event_type = 'finance.payment.state_changed'"
    ) == 0


@pytest.mark.asyncio
async def test_order_paid_raw_route_accepts_canonical_payment_entity_only(client):
    await seed_payment(status="authorized")
    raw = _route_order_paid_payload(event_id="evt_phase6al_order_paid_canonical")

    response = await _post_order_paid_route(
        client,
        raw,
        event_id="evt_phase6al_order_paid_canonical",
        idempotency_key="phase6al-order-paid-canonical",
    )

    assert response.status_code == 202
    assert await fetch_scalar("SELECT status FROM finance.payments") == "captured"
    assert await fetch_scalar(
        "SELECT count(*) FROM finance.payment_events WHERE provider_event_id = 'evt_phase6al_order_paid_canonical'"
    ) == 1


@pytest.mark.asyncio
async def test_order_paid_raw_route_accepts_repeated_same_payment_identity(client):
    await seed_payment(status="authorized")
    raw = _route_order_paid_payload(
        event_id="evt_phase6al_order_paid_repeated",
        payload_overrides={
            "payment_id": PAYMENT_REF,
            "payment_ids": [PAYMENT_REF, PAYMENT_REF],
            "payments": {
                "entity": {"id": PAYMENT_REF, "order_id": ORDER_REF},
                "items": [
                    {"id": PAYMENT_REF, "order_id": ORDER_REF},
                    {"id": PAYMENT_REF, "order_id": ORDER_REF},
                ],
                "entities": [{"id": PAYMENT_REF, "order_id": ORDER_REF}],
            }
        },
    )

    response = await _post_order_paid_route(
        client,
        raw,
        event_id="evt_phase6al_order_paid_repeated",
        idempotency_key="phase6al-order-paid-repeated",
    )

    assert response.status_code == 202
    assert await fetch_scalar("SELECT status FROM finance.payments") == "captured"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_id", "payments"),
    [
        (
            "evt_phase6al_order_paid_competing_array",
            {
                "items": [
                    {"id": PAYMENT_REF, "order_id": ORDER_REF},
                    {"id": "pay_phase6al_competing", "order_id": ORDER_REF},
                ]
            },
        ),
        (
            "evt_phase6al_order_paid_noncanonical_array",
            {
                "items": [
                    {"id": "pay_phase6al_other_a", "order_id": ORDER_REF},
                    {"id": "pay_phase6al_other_b", "order_id": ORDER_REF},
                ]
            },
        ),
        (
            "evt_phase6al_order_paid_conflicting_entity",
            {"entity": {"id": "pay_phase6al_entity_conflict", "order_id": ORDER_REF}},
        ),
    ],
)
async def test_order_paid_raw_route_rejects_competing_payment_evidence_without_mutation(
    client,
    event_id: str,
    payments: dict,
):
    await seed_payment(status="authorized")
    before = await mutation_counts()
    raw = _route_order_paid_payload(event_id=event_id, payload_overrides={"payments": payments})

    response = await _post_order_paid_route(
        client,
        raw,
        event_id=event_id,
        idempotency_key=f"{event_id}-request",
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "FINANCE_WEBHOOK_PAYLOAD_INVALID"
    assert PAYMENT_REF not in response.text
    assert "pay_phase6al" not in response.text
    assert WEBHOOK_SECRET.decode("utf-8") not in response.text
    assert await mutation_counts() == before
    assert await fetch_scalar("SELECT status FROM finance.payments") == "authorized"


@pytest.mark.asyncio
async def test_order_paid_raw_route_rejects_conflicting_top_level_payment_reference_without_mutation(client):
    await seed_payment(status="authorized")
    before = await mutation_counts()
    raw = _route_order_paid_payload(
        event_id="evt_phase6al_order_paid_top_ref_conflict",
        payload_overrides={"payment_id": "pay_phase6al_top_ref_conflict"},
    )

    response = await _post_order_paid_route(
        client,
        raw,
        event_id="evt_phase6al_order_paid_top_ref_conflict",
        idempotency_key="phase6al-order-paid-top-ref-conflict",
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "FINANCE_WEBHOOK_PAYLOAD_INVALID"
    assert "pay_phase6al" not in response.text
    assert await mutation_counts() == before


@pytest.mark.asyncio
async def test_order_paid_raw_route_rejects_payment_order_mismatch_without_mutation(client):
    await seed_payment(status="authorized")
    before = await mutation_counts()
    raw = _route_order_paid_payload(
        event_id="evt_phase6al_order_paid_order_mismatch",
        payload_overrides={
            "payment": {
                "entity": {
                    "id": PAYMENT_REF,
                    "order_id": "order_phase6al_conflict",
                    "amount": AMOUNT_SUBUNITS,
                    "currency": CURRENCY,
                    "status": "captured",
                    "captured": True,
                }
            }
        },
    )

    response = await _post_order_paid_route(
        client,
        raw,
        event_id="evt_phase6al_order_paid_order_mismatch",
        idempotency_key="phase6al-order-paid-order-mismatch",
    )

    assert response.status_code == 400
    assert await mutation_counts() == before


@pytest.mark.asyncio
async def test_order_paid_raw_route_rejects_array_order_ambiguity_deterministically(client):
    await seed_payment(status="authorized")
    before = await mutation_counts()
    arrays = [
        [
            {"id": PAYMENT_REF, "order_id": ORDER_REF},
            {"id": "pay_phase6al_array_other", "order_id": ORDER_REF},
        ],
        [
            {"id": "pay_phase6al_array_other", "order_id": ORDER_REF},
            {"id": PAYMENT_REF, "order_id": ORDER_REF},
        ],
    ]

    for index, items in enumerate(arrays):
        event_id = f"evt_phase6al_order_paid_array_order_{index}"
        raw = _route_order_paid_payload(event_id=event_id, payload_overrides={"payments": {"items": items}})
        response = await _post_order_paid_route(
            client,
            raw,
            event_id=event_id,
            idempotency_key=f"phase6al-array-order-{index}",
        )
        assert response.status_code == 400

    assert await mutation_counts() == before
    assert await fetch_scalar("SELECT status FROM finance.payments") == "authorized"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_id", "payload_overrides"),
    [
        ("evt_phase6al_order_paid_missing_payment", {"payment": {}}),
        ("evt_phase6al_order_paid_malformed_array", {"payments": {"items": "not-a-list"}}),
        ("evt_phase6al_order_paid_malformed_object", {"payments": {"items": [{"id": PAYMENT_REF}]}}),
        ("evt_phase6al_order_paid_nonstring_id", {"payments": {"items": [{"id": 123, "order_id": ORDER_REF}]}}),
        (
            "evt_phase6al_order_paid_oversized_id",
            {"payments": {"items": [{"id": "pay_" + "x" * 220, "order_id": ORDER_REF}]}},
        ),
    ],
)
async def test_order_paid_raw_route_rejects_missing_or_malformed_payment_evidence_without_mutation(
    client,
    event_id: str,
    payload_overrides: dict,
):
    await seed_payment(status="authorized")
    before = await mutation_counts()
    raw = _route_order_paid_payload(event_id=event_id, payload_overrides=payload_overrides)

    response = await _post_order_paid_route(
        client,
        raw,
        event_id=event_id,
        idempotency_key=f"{event_id}-request",
    )

    assert response.status_code == 400
    assert "secret" not in response.text.lower()
    assert PAYMENT_REF not in response.text
    assert await mutation_counts() == before
    assert await fetch_scalar("SELECT status FROM finance.payments") == "authorized"


@pytest.mark.asyncio
async def test_sandbox_route_uses_header_event_identity_instead_of_body_id(client):
    await seed_payment(status="authorized")
    raw = _route_capture_payload()
    signature = hmac.digest(WEBHOOK_SECRET, raw, "sha256").hex()
    override_webhook_dependencies(sandbox_webhook_posture())
    try:
        accepted = await client.post(
            "/api/v1/finance/payments/webhooks/razorpay",
            content=raw,
            headers={
                "X-Razorpay-Signature": signature,
                "X-Razorpay-Event-Id": "evt_phase6al_header",
                "X-Idempotency-Key": "phase6al-header",
            },
        )
    finally:
        clear_webhook_dependency_overrides()

    assert accepted.status_code == 202
    assert await fetch_scalar(
        "SELECT provider_event_id FROM finance.payment_events"
    ) == "evt_phase6al_header"


@pytest.mark.asyncio
async def test_sandbox_route_rejects_missing_authoritative_event_header_without_mutation(client):
    await seed_payment(status="authorized")
    raw = _route_capture_payload()
    signature = hmac.digest(WEBHOOK_SECRET, raw, "sha256").hex()
    before = await mutation_counts()
    override_webhook_dependencies(sandbox_webhook_posture())
    try:
        rejected = await client.post(
            "/api/v1/finance/payments/webhooks/razorpay",
            content=raw,
            headers={
                "X-Razorpay-Signature": signature,
                "X-Idempotency-Key": "phase6al-missing-header",
            },
        )
    finally:
        clear_webhook_dependency_overrides()

    assert rejected.status_code == 400
    assert await mutation_counts() == before


@pytest.mark.asyncio
async def test_capture_confirmation_has_no_accounting_product_or_secret_side_effects():
    checkout = await seed_payment(status="authorized")
    await confirm(evidence(event_id="evt_phase6al_boundaries"))
    counts = await mutation_counts()

    assert counts["events"] == 1
    assert counts["state_events"] == 1
    assert counts["allocations"] == 0
    assert counts["ledger_entries"] == 0
    assert counts["paid_invoices"] == 0
    assert counts["refunds"] == 0
    assert counts["credit_notes"] == 0
    assert await fetch_scalar(
        "SELECT status FROM finance.invoices WHERE id = :id",
        {"id": checkout.finance_invoice_id},
    ) == "issued"
    stored = await fetch_one(
        """
        SELECT event_type, event_payload_sha256,
               (SELECT provider_signature_hash FROM finance.payments WHERE id = :payment_id) AS signature_hash
        FROM finance.payment_events
        """,
        {"payment_id": checkout.finance_checkout_intent_id},
    )
    assert stored["event_type"] == "payment.captured"
    assert len(stored["event_payload_sha256"]) == 64
    assert stored["signature_hash"] is None


def test_phase6al_adds_no_network_fetch_capture_api_frontend_or_product_mutation():
    repo_root = Path(__file__).resolve().parents[2]
    service_source = (
        repo_root / "app" / "finance_core" / "services" / "provider_capture_confirmation.py"
    ).read_text(encoding="utf-8").lower()
    domain_source = (
        repo_root / "app" / "finance_core" / "domain" / "provider_capture_confirmation.py"
    ).read_text(encoding="utf-8").lower()
    combined = service_source + domain_source

    for forbidden in (
        "http.client",
        "requests",
        "httpx",
        "aiohttp",
        "urllib",
        "authorization",
        "key_secret",
        "webhook_secret",
        "internal_apply",
        "allocate_payment",
        "ledger_entries",
        "mark_invoice_paid",
        "activate_subscription",
        "entitlement",
        "platform_billing",
    ):
        assert forbidden not in combined
    assert not (repo_root / "frontend").exists()
