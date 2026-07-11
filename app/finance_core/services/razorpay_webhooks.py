from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.finance_core.domain.provider_boundary import (
    FinancePaymentStateTransitionError,
    FinanceWebhookNormalizationError,
    FinanceWebhookSignatureError,
    NormalizedProviderEventCommand,
    NormalizedProviderEventResult,
    ProviderSandboxConfig,
    StaticSandboxSignatureVerifier,
)
from app.finance_core.domain.razorpay_sandbox import RazorpaySandboxConfig, verify_razorpay_webhook_signature
from app.finance_core.domain.razorpay_webhooks import RazorpayWebhookInput, RazorpayWebhookPaymentReference
from app.finance_core.repositories.payments import FinancePaymentRepository
from app.finance_core.services.operational_guards import FinanceOperationalGuardService
from app.finance_core.services.provider_webhooks import FinanceProviderWebhookIntakeService


RAZORPAY_EVENT_STATUS_MAP = {
    "payment.authorized": "authorized",
    "payment.captured": "captured",
    "payment.failed": "failed",
    "order.paid": "captured",
}


class RazorpayWebhookConfirmationService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        razorpay_config: RazorpaySandboxConfig,
        provider_config: ProviderSandboxConfig,
        guard_service: FinanceOperationalGuardService | None = None,
    ):
        self._session = session
        self._razorpay_config = razorpay_config
        self._provider_config = provider_config
        self._guard_service = guard_service or FinanceOperationalGuardService()
        self._payments = FinancePaymentRepository(session)
        self._intake = FinanceProviderWebhookIntakeService(
            session,
            config=provider_config,
            signature_verifier=StaticSandboxSignatureVerifier(),
        )

    async def confirm_payment_event(self, webhook: RazorpayWebhookInput) -> NormalizedProviderEventResult:
        reference = self.normalize(webhook)
        self._guard_service.require_safe_preflight()
        payment = await self._payments.get_payment_by_provider_order_ref(
            provider_code=self._provider_config.provider_code,
            provider_order_ref=reference.provider_order_id,
            for_update=True,
        )
        if payment is None and reference.provider_payment_id:
            payment = await self._payments.get_payment_by_provider_ref(
                provider_code=self._provider_config.provider_code,
                provider_payment_ref=reference.provider_payment_id,
                for_update=True,
            )
        if payment is None:
            raise FinanceWebhookNormalizationError("Razorpay event references an unknown provider order/payment")
        if reference.provider_payment_id:
            if payment.provider_payment_ref and payment.provider_payment_ref != reference.provider_payment_id:
                raise FinanceWebhookNormalizationError("Razorpay event payment/provider reference mismatch")
            if payment.provider_payment_ref is None:
                await self._payments.set_provider_payment_ref(payment, provider_payment_ref=reference.provider_payment_id)

        try:
            return await self._intake.normalize_provider_event(
                NormalizedProviderEventCommand(
                    provider_code=self._provider_config.provider_code,
                    provider_event_id=reference.provider_event_id,
                    event_type=reference.event_type,
                    raw_status=reference.mapped_status,
                    payload_hash=reference.payload_hash,
                    signature=_provider_signature(reference.payload_hash, self._provider_config.signing_secret),
                    idempotency_key=webhook.idempotency_key or f"razorpay:{reference.provider_event_id}",
                    payment_id=payment.id,
                )
            )
        except FinancePaymentStateTransitionError:
            raise

    def normalize(self, webhook: RazorpayWebhookInput) -> RazorpayWebhookPaymentReference:
        if not webhook.signature:
            raise FinanceWebhookSignatureError("Missing Razorpay webhook signature")
        if not webhook.raw_body:
            raise FinanceWebhookNormalizationError("Missing Razorpay webhook raw body")
        if not verify_razorpay_webhook_signature(
            raw_body=webhook.raw_body,
            signature=webhook.signature,
            webhook_secret=self._razorpay_config.webhook_secret,
        ):
            raise FinanceWebhookSignatureError("Invalid Razorpay webhook signature")

        payload_hash = hashlib.sha256(webhook.raw_body).hexdigest()
        try:
            payload = json.loads(webhook.raw_body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise FinanceWebhookNormalizationError("Invalid Razorpay webhook JSON") from exc

        event_type = _required_text(payload, "event")
        if event_type not in RAZORPAY_EVENT_STATUS_MAP:
            raise FinanceWebhookNormalizationError("Unsupported Razorpay webhook event type")
        payment_entity = _payment_entity(payload)
        provider_order_id = _required_text(payment_entity, "order_id")
        provider_payment_id = payment_entity.get("id")
        if provider_payment_id is not None and not isinstance(provider_payment_id, str):
            raise FinanceWebhookNormalizationError("Invalid Razorpay payment id")

        return RazorpayWebhookPaymentReference(
            provider_event_id=_event_id(payload),
            event_type=event_type,
            provider_order_id=provider_order_id,
            provider_payment_id=provider_payment_id,
            mapped_status=RAZORPAY_EVENT_STATUS_MAP[event_type],
            payload_hash=payload_hash,
        )


def _payment_entity(payload: dict[str, Any]) -> dict[str, Any]:
    payment = payload.get("payload", {}).get("payment", {}).get("entity")
    if not isinstance(payment, dict):
        raise FinanceWebhookNormalizationError("Razorpay event is missing payment entity")
    return payment


def _event_id(payload: dict[str, Any]) -> str:
    value = payload.get("id") or payload.get("event_id")
    if not isinstance(value, str) or not value.strip():
        raise FinanceWebhookNormalizationError("Razorpay event id is required")
    return value


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise FinanceWebhookNormalizationError(f"Razorpay {key} is required")
    return value


def _provider_signature(payload_hash: str, signing_secret: str) -> str:
    return hashlib.sha256(f"{payload_hash}:{signing_secret}".encode("utf-8")).hexdigest()
