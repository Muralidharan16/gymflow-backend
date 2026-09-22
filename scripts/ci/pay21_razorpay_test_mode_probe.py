#!/usr/bin/env python3
"""PAY-21 real Razorpay test-mode connectivity and secret-boundary proof."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path

from app.finance_core.domain.razorpay_sandbox import (
    RazorpayOrderCreateRequest,
    verify_razorpay_webhook_signature,
)
from app.finance_core.services.razorpay_local_smoke import (
    load_razorpay_test_mode_config_from_env,
    redact_razorpay_key_id,
)
from app.finance_core.services.razorpay_sandbox import (
    RazorpayTestModeHTTPTransport,
    RazorpayTestModeOrdersClient,
)


APPROVED_RAZORPAY_API = "https://api.razorpay.com/v1"


def _required(name: str) -> str:
    value = str(os.environ.get(name, "")).strip()
    if not value:
        raise SystemExit(f"PAY-21 secret/config missing: {name}")
    return value


async def main() -> int:
    candidate = _required("PAY21_CANDIDATE_SHA")
    run_id = _required("GITHUB_RUN_ID")
    output = Path(_required("PAY21_PROVIDER_EVIDENCE"))

    config = load_razorpay_test_mode_config_from_env(
        require_webhook_secret=True,
        merchant_reference="doers_pay21_preproduction",
    )
    if config.api_base_url != APPROVED_RAZORPAY_API:
        raise SystemExit("PAY-21 Razorpay API origin drift")
    if not config.key_id.startswith("rzp_test_"):
        raise SystemExit("PAY-21 provider key is not Razorpay test mode")
    if "rzp_live_" in config.key_id.lower():
        raise SystemExit("PAY-21 live Razorpay key rejected")

    # A short deterministic receipt is unique per exact-head workflow run.
    digest = hashlib.sha256(f"{candidate}:{run_id}".encode("utf-8")).hexdigest()
    receipt = f"p21_{digest[:28]}"
    request = RazorpayOrderCreateRequest(
        amount_subunits=100,
        currency_code="INR",
        receipt=receipt,
        notes={
            "certification": "PAY-21",
            "candidate_sha": candidate[:16],
        },
    )

    transport = RazorpayTestModeHTTPTransport()
    client = RazorpayTestModeOrdersClient(config=config, transport=transport)
    created = await client.create_order(request)

    if not created.order_id.startswith("order_"):
        raise SystemExit("PAY-21 Razorpay test order id invalid")
    if created.amount_subunits != 100 or created.currency_code.upper() != "INR":
        raise SystemExit("PAY-21 Razorpay test order amount/currency drift")
    if created.receipt != receipt:
        raise SystemExit("PAY-21 Razorpay test order receipt drift")

    auth = base64.b64encode(
        f"{config.key_id}:{config.key_secret}".encode("utf-8")
    ).decode("ascii")
    fetched = await transport.get_json(
        url=f"{config.api_base_url}/orders/{created.order_id}",
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/json",
        },
        timeout_seconds=float(config.timeout_seconds),
    )
    if str(fetched.get("id", "")) != created.order_id:
        raise SystemExit("PAY-21 Razorpay order readback mismatch")
    if int(fetched.get("amount", -1)) != 100:
        raise SystemExit("PAY-21 Razorpay order readback amount mismatch")

    # Prove the injected webhook secret is usable without emitting it.
    body = json.dumps(
        {
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_pay21_synthetic",
                        "order_id": created.order_id,
                    }
                }
            },
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    import hmac

    signature = hmac.digest(config.webhook_secret.encode("utf-8"), body, "sha256").hex()
    if not verify_razorpay_webhook_signature(
        raw_body=body,
        signature=signature,
        webhook_secret=config.webhook_secret,
    ):
        raise SystemExit("PAY-21 Razorpay webhook secret verification failed")

    record = {
        "schema_version": 1,
        "phase": "PAY-21",
        "candidate_sha": candidate,
        "provider": "razorpay",
        "provider_mode": config.mode,
        "provider_api": config.api_base_url,
        "key_id_redacted": redact_razorpay_key_id(config.key_id),
        "test_order_id": created.order_id,
        "test_order_status": created.status,
        "amount_subunits": created.amount_subunits,
        "currency_code": created.currency_code,
        "receipt": receipt,
        "readback_verified": True,
        "webhook_secret_verified": True,
        "production_customer_data": False,
        "real_money_movement": False,
        "live_key": False,
        "decision": "PASS",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(record, indent=2, sort_keys=True))
    print("PAY21_RAZORPAY_TEST_ACCOUNT=PASS")
    print("PAY21_RAZORPAY_REAL_HTTPS=PASS")
    print("PAY21_RAZORPAY_LIVE_KEY=REJECTED")
    print("PAY21_REAL_MONEY_MOVEMENT=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
