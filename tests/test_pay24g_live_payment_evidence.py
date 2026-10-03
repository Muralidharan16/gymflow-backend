from __future__ import annotations

import asyncio
from decimal import Decimal
import uuid

import pytest

from app.finance_core.domain.razorpay_live import RazorpayLiveConfig
from app.finance_core.services.pay24g_live_payment_evidence import (
    ConfirmTerminalLivePaymentCommand,
    FinanceTerminalLivePaymentEvidenceService,
)
from app.finance_core.services.razorpay_live import (
    RazorpayLivePaymentsClient,
)


class _FetchTransport:
    def __init__(self, payload):
        self.payload = payload
        self.calls=[]

    async def get_json(self, **kwargs):
        self.calls.append(kwargs)
        return dict(self.payload)

    async def post_json(self, **kwargs):
        raise AssertionError("PAY-24-G payment evidence must not POST to provider")


class _Repo:
    def __init__(self):
        self.calls=[]

    async def confirm_provider_evidence_capability(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "payment_event_id": uuid.UUID("24a00000-0000-4000-8000-000000000001"),
            "payment_id": uuid.UUID("24a00000-0000-4000-8000-000000000002"),
            "provider_code": kwargs["provider_code"],
            "provider_event_id": kwargs["provider_event_id"],
            "event_type": kwargs["event_type"],
            "previous_payment_status": "created",
            "payment_status": "captured",
            "event_recorded": True,
            "state_changed": True,
            "state_ignored": False,
            "replayed": False,
        }


def _config():
    return RazorpayLiveConfig(
        mode="live",
        key_id="rzp_live_pay24g_public",
        key_secret="pay24g-secret-fixture",
        merchant_reference="doers-pay24g-canary",
    )


def _signature(order_id: str, payment_id: str) -> str:
    import hmac
    return hmac.digest(
        b"pay24g-secret-fixture",
        f"{order_id}|{payment_id}".encode(),
        "sha256",
    ).hex()


def test_terminal_live_evidence_requires_signature_and_captured_api_state():
    async def scenario():
        order_id="order_pay24gfixture"
        payment_id="pay_pay24gfixture"
        transport=_FetchTransport({
            "id": payment_id,
            "order_id": order_id,
            "amount": 100,
            "currency": "INR",
            "status": "captured",
            "captured": True,
        })
        client=RazorpayLivePaymentsClient(config=_config(),transport=transport)
        service=FinanceTerminalLivePaymentEvidenceService(
            object(),
            key_secret="pay24g-secret-fixture",
            payments_client=client,
        )
        repo=_Repo()
        service._repo=repo
    
        result=await service.confirm_captured_payment(
            ConfirmTerminalLivePaymentCommand(
                provider_order_ref=order_id,
                provider_payment_ref=payment_id,
                checkout_signature=_signature(order_id,payment_id),
                expected_amount_subunits=100,
                expected_currency="INR",
            )
        )
    
        assert result.payment_status == "captured"
        assert len(transport.calls) == 1
        assert transport.calls[0]["url"].endswith(f"/payments/{payment_id}")
        assert len(repo.calls) == 1
        assert repo.calls[0]["provider_code"] == "razorpay"
        assert repo.calls[0]["event_type"] == "payment.captured"
    

    asyncio.run(scenario())

def test_terminal_live_evidence_rejects_authorized_not_captured():
    async def scenario():
        order_id="order_pay24gauthorized"
        payment_id="pay_pay24gauthorized"
        client=RazorpayLivePaymentsClient(
            config=_config(),
            transport=_FetchTransport({
                "id": payment_id,
                "order_id": order_id,
                "amount": 100,
                "currency": "INR",
                "status": "authorized",
                "captured": False,
            }),
        )
        service=FinanceTerminalLivePaymentEvidenceService(
            object(),
            key_secret="pay24g-secret-fixture",
            payments_client=client,
        )
        service._repo=_Repo()
    
        with pytest.raises(ValueError, match="not captured"):
            await service.confirm_captured_payment(
                ConfirmTerminalLivePaymentCommand(
                    provider_order_ref=order_id,
                    provider_payment_ref=payment_id,
                    checkout_signature=_signature(order_id,payment_id),
                    expected_amount_subunits=100,
                    expected_currency="INR",
                )
            )
        assert service._repo.calls == []

    asyncio.run(scenario())
