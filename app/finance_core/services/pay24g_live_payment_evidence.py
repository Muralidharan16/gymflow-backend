"""PAY-24-G terminal-only live payment evidence confirmation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.finance_core.domain.invoice_engine import canonical_hash
from app.finance_core.domain.provider_capture_confirmation import (
    ProviderPaymentEvidenceResult,
)
from app.finance_core.domain.razorpay_sandbox import (
    RazorpayCheckoutSignatureError,
    verify_razorpay_checkout_signature,
)
from app.finance_core.repositories.payments import FinancePaymentRepository
from app.finance_core.services.razorpay_live import RazorpayLivePaymentsClient


@dataclass(frozen=True, slots=True)
class ConfirmTerminalLivePaymentCommand:
    provider_order_ref: str
    provider_payment_ref: str
    checkout_signature: str
    expected_amount_subunits: int
    expected_currency: str


class FinanceTerminalLivePaymentEvidenceService:
    """Convert independently verified live Razorpay state into Finance evidence.

    The backing PostgreSQL capability is granted only to
    finance_reconciliation_runtime in PAY-24-G. This service performs no
    provider mutation and never captures a payment.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        key_secret: str,
        payments_client: RazorpayLivePaymentsClient,
    ) -> None:
        self._repo = FinancePaymentRepository(session)
        self._key_secret = key_secret
        self._payments_client = payments_client

    async def confirm_captured_payment(
        self,
        command: ConfirmTerminalLivePaymentCommand,
    ) -> ProviderPaymentEvidenceResult:
        verification = verify_razorpay_checkout_signature(
            razorpay_order_id=command.provider_order_ref,
            razorpay_payment_id=command.provider_payment_ref,
            razorpay_signature=command.checkout_signature,
            key_secret=self._key_secret,
        )
        if verification.verified is not True:
            raise RazorpayCheckoutSignatureError(
                "RAZORPAY_CHECKOUT_SIGNATURE_INVALID",
                "Razorpay checkout signature is invalid.",
            )

        payment = await self._payments_client.fetch_payment(
            command.provider_payment_ref
        )
        if payment.order_id != command.provider_order_ref:
            raise ValueError("PAY-24-G provider payment order mismatch")
        if payment.amount_subunits != command.expected_amount_subunits:
            raise ValueError("PAY-24-G provider payment amount mismatch")
        if payment.currency_code != command.expected_currency.upper():
            raise ValueError("PAY-24-G provider payment currency mismatch")
        if payment.status != "captured" or payment.captured is not True:
            raise ValueError(
                "PAY-24-G payment is not captured; no Finance application allowed"
            )

        evidence = {
            "provider_code": "razorpay",
            "provider_order_ref": payment.order_id,
            "provider_payment_ref": payment.payment_id,
            "provider_amount_subunits": payment.amount_subunits,
            "provider_currency": payment.currency_code,
            "provider_payment_status": payment.status,
            "provider_captured": payment.captured,
            "checkout_signature_verified": True,
            "provider_api_verified": True,
        }
        request_hash = canonical_hash(evidence)
        event_digest = hashlib.sha256(
            (
                payment.payment_id
                + "|"
                + payment.order_id
                + "|"
                + str(payment.amount_subunits)
                + "|"
                + payment.currency_code
            ).encode("utf-8")
        ).hexdigest()[:48]
        provider_event_id = f"pay24g_{event_digest}"
        idempotency_key = f"pay24g:live-evidence:{payment.payment_id}"

        row = await self._repo.confirm_provider_evidence_capability(
            provider_code="razorpay",
            provider_event_id=provider_event_id,
            event_type="payment.captured",
            provider_order_ref=payment.order_id,
            provider_payment_ref=payment.payment_id,
            provider_amount_subunits=payment.amount_subunits,
            provider_currency=payment.currency_code,
            provider_payment_status="captured",
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        return ProviderPaymentEvidenceResult(
            payment_event_id=row["payment_event_id"],
            payment_id=row["payment_id"],
            provider_code=row["provider_code"],
            provider_event_id=row["provider_event_id"],
            event_type=row["event_type"],
            previous_payment_status=row["previous_payment_status"],
            payment_status=row["payment_status"],
            event_recorded=row["event_recorded"],
            state_changed=row["state_changed"],
            state_ignored=row["state_ignored"],
            replayed=row["replayed"],
        )
