from __future__ import annotations

import asyncio

from tests.finance_core.test_phase5c_invoice_engine import (
    create_draft,
    draft_command,
    fetch_scalar,
    issue_invoice,
)
from tests.finance_core.test_phase6c_checkout_orchestration import (
    FakeRazorpayClient,
    command,
    orchestrate,
)
from tests.finance_core.test_phase6e_payment_application_gate import apply_gate
from tests import test_pay9_payment_application_entitlement_runtime as pay9
from tests.test_p4d_refund_authority_runtime import _connect
from tests.test_p4d_refund_obligation_resolution_runtime import _SOURCE_C


async def _replay_finance() -> None:
    provider_before = await fetch_scalar(
        "SELECT count(*) FROM finance.provider_operations "
        "WHERE operation_type='create_checkout'"
    )
    invoice_before = await fetch_scalar("SELECT count(*) FROM finance.invoices")
    application_before = await fetch_scalar(
        "SELECT count(*) FROM finance.payment_application_records"
    )
    allocation_before = await fetch_scalar(
        "SELECT count(*) FROM finance.payment_allocations"
    )
    ledger_before = await fetch_scalar(
        "SELECT count(*) FROM finance.ledger_entries "
        "WHERE source_type='payment_allocation'"
    )
    outbox_before = await fetch_scalar("SELECT count(*) FROM finance.outbox_events")

    payment_id = await fetch_scalar(
        "SELECT id FROM finance.payments "
        "WHERE provider_code='razorpay_sandbox' "
        "AND provider_order_ref='order_test_1'"
    )
    invoice_id = await fetch_scalar(
        "SELECT invoice_id FROM finance.payment_allocations "
        "WHERE payment_id=:payment_id",
        {"payment_id": payment_id},
    )
    provider_status_before = await fetch_scalar(
        "SELECT status FROM finance.provider_operations "
        "WHERE payment_id=:payment_id AND operation_type='create_checkout'",
        {"payment_id": payment_id},
    )
    provider_object_before = await fetch_scalar(
        "SELECT provider_object_id FROM finance.provider_operations "
        "WHERE payment_id=:payment_id AND operation_type='create_checkout'",
        {"payment_id": payment_id},
    )
    if provider_status_before != "succeeded" or provider_object_before != "order_test_1":
        raise RuntimeError("PAY-19 recovered provider operation is not canonical")

    client = FakeRazorpayClient()
    try:
        checkout, _ = await orchestrate(
            command(idempotency_key="pay19-checkout"),
            client=client,
        )
    except ValueError as exc:
        if str(exc) != "Checkout orchestration requires an issued invoice":
            raise
        invoice_status = await fetch_scalar(
            "SELECT status FROM finance.invoices WHERE id=:invoice_id",
            {"invoice_id": invoice_id},
        )
        if invoice_status != "paid":
            raise RuntimeError(
                "PAY-19 recovered checkout replay failed for a non-paid invoice"
            ) from exc
    else:
        if not checkout.replayed:
            raise RuntimeError("PAY-19 recovered checkout did not idempotently replay")
        if checkout.finance_checkout_intent_id != payment_id:
            raise RuntimeError("PAY-19 recovered checkout payment identity drift")
        if checkout.finance_invoice_id != invoice_id:
            raise RuntimeError("PAY-19 recovered checkout invoice identity drift")

    if client.requests:
        raise RuntimeError("PAY-19 replay made a duplicate provider operation")
    application = await apply_gate(
        payment_id,
        invoice_id,
        idempotency_key="pay19-application",
    )
    if not application.replayed:
        raise RuntimeError("PAY-19 recovered checkout payment application did not replay")

    pay9_replay = pay9._apply()
    if pay9_replay[9] is not True:
        raise RuntimeError(
            f"PAY-19 recovered PAY-9 application record did not replay: {pay9_replay!r}"
        )

    if await fetch_scalar(
        "SELECT count(*) FROM finance.provider_operations "
        "WHERE operation_type='create_checkout'"
    ) != provider_before:
        raise RuntimeError("PAY-19 duplicate provider operation after recovery")
    if await fetch_scalar(
        "SELECT status FROM finance.provider_operations "
        "WHERE payment_id=:payment_id AND operation_type='create_checkout'",
        {"payment_id": payment_id},
    ) != provider_status_before:
        raise RuntimeError("PAY-19 provider operation status drift after replay")
    if await fetch_scalar(
        "SELECT provider_object_id FROM finance.provider_operations "
        "WHERE payment_id=:payment_id AND operation_type='create_checkout'",
        {"payment_id": payment_id},
    ) != provider_object_before:
        raise RuntimeError("PAY-19 provider object drift after replay")
    if await fetch_scalar("SELECT count(*) FROM finance.invoices") != invoice_before:
        raise RuntimeError("PAY-19 checkout replay created duplicate invoice")
    if await fetch_scalar(
        "SELECT count(*) FROM finance.payment_application_records"
    ) != application_before:
        raise RuntimeError("PAY-19 repeated payment application record")
    if await fetch_scalar(
        "SELECT count(*) FROM finance.payment_allocations"
    ) != allocation_before:
        raise RuntimeError("PAY-19 repeated payment allocation")
    if await fetch_scalar(
        "SELECT count(*) FROM finance.ledger_entries "
        "WHERE source_type='payment_allocation'"
    ) != ledger_before:
        raise RuntimeError("PAY-19 repeated payment application ledger effect")
    if await fetch_scalar("SELECT count(*) FROM finance.outbox_events") != outbox_before:
        raise RuntimeError("PAY-19 replay created duplicate outbox effect")

    next_draft = await create_draft(
        draft_command(idempotency_key="pay19-recovery-next-draft")
    )
    next_invoice = await issue_invoice(
        next_draft.invoice_id,
        idempotency_key="pay19-recovery-next-issue",
    )
    if next_invoice.official_invoice_number != "VS/2425/00002":
        raise RuntimeError(
            "PAY-19 invoice sequence reused or skipped unexpectedly: "
            f"{next_invoice.official_invoice_number!r}"
        )
    if await fetch_scalar(
        "SELECT count(*) FROM finance.invoices "
        "WHERE official_invoice_number='VS/2425/00001'"
    ) != 1:
        raise RuntimeError("PAY-19 restored invoice number was reused")


def _replay_refund_command() -> None:
    with _connect("migration_owner", "MIGRATION_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT refund_id,command_id
                FROM finance.refund_execution_commands
                WHERE source_id=%s
                """,
                (_SOURCE_C,),
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError("PAY-19 recovered refund command missing")
            refund_id, command_id = row
        conn.commit()

    with _connect("worker_test_runtime", "WORKER_RUNTIME_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT command_id,reused
                FROM app_secure.materialize_refund_execution_command(
                    %s,'branch.refund_required',%s,'pay19-recovery-refund-replay'
                )
                """,
                (refund_id, _SOURCE_C),
            )
            replay = cur.fetchone()
            if replay != (command_id, True):
                raise RuntimeError(
                    f"PAY-19 refund command replay drift: {replay!r}"
                )
        conn.commit()

    with _connect("migration_owner", "MIGRATION_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM finance.refund_execution_commands "
                "WHERE source_id=%s",
                (_SOURCE_C,),
            )
            if cur.fetchone()[0] != 1:
                raise RuntimeError("PAY-19 repeated refund execution command")
        conn.commit()


async def main() -> None:
    await _replay_finance()
    _replay_refund_command()
    print("PAY19_NO_DUPLICATE_PROVIDER_OPERATION=PASS")
    print("PAY19_NO_REUSED_INVOICE_NUMBER=PASS")
    print("PAY19_NO_REPEATED_REFUND=PASS")
    print("PAY19_NO_REPEATED_PAYMENT_APPLICATION=PASS")


if __name__ == "__main__":
    asyncio.run(main())
