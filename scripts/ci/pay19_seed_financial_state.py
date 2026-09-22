from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from tests.finance_core.test_phase5c_invoice_engine import seed_master_data
from tests.finance_core.test_phase6c_checkout_orchestration import command, orchestrate
from tests.finance_core.test_phase6d_razorpay_webhook_normalization import (
    confirm,
    razorpay_payload,
    signed_webhook,
)
from tests.finance_core.test_phase6e_payment_application_gate import (
    apply_gate,
    seed_ledger_accounts_only,
)
from tests import test_pay9_payment_application_entitlement_runtime as pay9
from tests.test_p4d_refund_obligation_resolution_runtime import (
    _INVOICE_A,
    _PAYMENT_A,
    _SOURCE_C,
    _SUBSCRIPTION_A,
    _allocate,
    _claim_source,
    _ensure_p4d2_base_state,
    _insert_source,
    _record_member_subscription_checkout_binding,
    _resolve,
)


async def _seed_checkout_payment() -> dict[str, str]:
    await seed_master_data()
    await seed_ledger_accounts_only()

    checkout, client = await orchestrate(command(idempotency_key="pay19-checkout"))
    if len(client.requests) != 1:
        raise RuntimeError("PAY-19 seed expected exactly one synthetic provider call")

    capture = await confirm(
        signed_webhook(
            razorpay_payload(
                event_id="evt_pay19_captured",
                event_type="payment.captured",
                payment_id="pay_pay19_captured",
                order_id=checkout.provider_order_id,
                status="captured",
            ),
            idempotency_key="pay19-capture",
        )
    )
    application = await apply_gate(
        checkout.finance_checkout_intent_id,
        checkout.finance_invoice_id,
        idempotency_key="pay19-application",
    )
    if application.invoice_status != "paid":
        raise RuntimeError("PAY-19 seed payment application did not settle invoice")

    return {
        "invoice_id": str(checkout.finance_invoice_id),
        "payment_id": str(checkout.finance_checkout_intent_id),
        "provider_order_id": str(checkout.provider_order_id),
        "payment_event_id": str(capture.payment_event_id),
        "allocation_id": str(application.allocation_id),
    }


def _seed_pay9_application_record() -> dict[str, str]:
    term_id = pay9._seed()
    applied = pay9._apply()
    if applied[8] != "applied_paid" or applied[9] is not False:
        raise RuntimeError(f"PAY-19 PAY-9 application seed failed: {applied!r}")
    return {
        "subscription_term_id": str(term_id),
        "payment_id": str(pay9.PROVIDER_PAYMENT),
        "payment_event_id": str(pay9.EVENT_A),
        "invoice_id": str(pay9.pay4.INVOICE),
        "application_record_id": str(applied[0]),
        "allocation_id": str(applied[3]),
    }


def _seed_subscription_refund() -> dict[str, str]:
    _ensure_p4d2_base_state()
    _allocate(amount="80.00")

    binding = _record_member_subscription_checkout_binding(
        _SUBSCRIPTION_A,
        _INVOICE_A,
        _PAYMENT_A,
    )
    if binding["inserted"] is not True:
        raise RuntimeError("PAY-19 checkout binding seed was not inserted")

    _insert_source(
        _SOURCE_C,
        payload={
            "reason": "pay19 synthetic recovery refund obligation",
            "amount": "9999.99",
            "currency": "USD",
        },
    )
    _claim_source(_SOURCE_C)
    resolved = _resolve()
    if resolved[0] != "command_materialized":
        raise RuntimeError(f"PAY-19 refund command seed failed: {resolved!r}")

    return {
        "subscription_id": str(_SUBSCRIPTION_A),
        "subscription_invoice_id": str(_INVOICE_A),
        "subscription_checkout_intent_id": str(_PAYMENT_A),
        "refund_id": str(resolved[1]),
        "refund_command_id": str(resolved[2]),
        "refund_source_id": str(_SOURCE_C),
    }


async def main() -> None:
    evidence_path = Path(
        os.environ.get("PAY19_SEED_EVIDENCE", "pay19-seed-evidence.json")
    )
    checkout = await _seed_checkout_payment()
    pay9_application = _seed_pay9_application_record()
    refund = _seed_subscription_refund()
    payload = {
        "synthetic_only": True,
        "live_provider": False,
        "checkout": checkout,
        "pay9_application": pay9_application,
        "subscription_refund": refund,
    }
    evidence_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print("PAY19_SYNTHETIC_FINANCIAL_STATE=PASS")
    print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
