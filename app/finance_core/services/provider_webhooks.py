from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.finance_core.domain.invoice_engine import canonical_hash
from app.finance_core.domain.payment_ledger import (
    FinancePaymentConflictError,
    RecordPaymentEventCommand,
)
from app.finance_core.domain.provider_boundary import (
    FinanceWebhookNormalizationError,
    FinanceWebhookSignatureError,
    NormalizedProviderEventCommand,
    NormalizedProviderEventResult,
    ProviderSandboxConfig,
    ProviderWebhookSignatureVerifier,
    validate_sandbox_provider_config,
)
from app.finance_core.services.payment_ledger import FinancePaymentLedgerService


KNOWN_PROVIDER_STATUSES = {
    "created",
    "pending",
    "authorized",
    "captured",
    "failed",
    "cancelled",
    "refunded",
    "partially_refunded",
    "settled",
}


class FinanceProviderWebhookIntakeService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        config: ProviderSandboxConfig,
        signature_verifier: ProviderWebhookSignatureVerifier,
    ):
        self._payment_service = FinancePaymentLedgerService(session)
        self._config = validate_sandbox_provider_config(config)
        self._signature_verifier = signature_verifier

    async def normalize_provider_event(self, command: NormalizedProviderEventCommand) -> NormalizedProviderEventResult:
        if command.provider_code != self._config.provider_code:
            raise FinanceWebhookNormalizationError("Provider code does not match configured sandbox provider")
        if command.raw_status not in KNOWN_PROVIDER_STATUSES:
            raise FinanceWebhookNormalizationError("Unknown provider payment status")
        if not self._signature_verifier.verify(
            payload_hash=command.payload_hash,
            signature=command.signature,
            config=self._config,
        ):
            raise FinanceWebhookSignatureError("Invalid provider event signature")

        payload = {
            "provider_code": command.provider_code,
            "provider_event_id": command.provider_event_id,
            "event_type": command.event_type,
            "raw_status": command.raw_status,
            "payload_hash": command.payload_hash,
            "payment_id": str(command.payment_id) if command.payment_id else None,
        }
        result = await self._payment_service.record_payment_event(
            RecordPaymentEventCommand(
                payment_id=command.payment_id,
                provider_code=command.provider_code,
                provider_event_id=command.provider_event_id,
                event_type=command.event_type,
                event_payload_sha256=command.payload_hash,
                idempotency_key=command.idempotency_key,
            )
        )
        if result.replayed:
            return NormalizedProviderEventResult(
                payment_event_id=result.payment_event_id,
                provider_code=command.provider_code,
                provider_event_id=command.provider_event_id,
                event_type=command.event_type,
                raw_status=command.raw_status,
                replayed=True,
            )
        # Keep the normalization hash explicit for future audit comparison without storing raw payloads.
        canonical_hash(payload)
        return NormalizedProviderEventResult(
            payment_event_id=result.payment_event_id,
            provider_code=command.provider_code,
            provider_event_id=command.provider_event_id,
            event_type=command.event_type,
            raw_status=command.raw_status,
        )


def is_duplicate_provider_event_error(error: Exception) -> bool:
    return isinstance(error, FinancePaymentConflictError) and "Provider payment event already exists" in str(error)
