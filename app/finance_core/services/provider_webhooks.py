from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.finance_core.domain.invoice_engine import canonical_hash
from app.finance_core.domain.payment_ledger import (
    FinancePaymentConflictError,
    RecordPaymentEventCommand,
)
from app.finance_core.domain.provider_boundary import (
    FinancePaymentStateTransitionError,
    FinanceWebhookNormalizationError,
    FinanceWebhookSignatureError,
    NormalizedProviderEventCommand,
    NormalizedProviderEventResult,
    ProviderSandboxConfig,
    ProviderWebhookSignatureVerifier,
    payment_state_transition_action,
    validate_sandbox_provider_config,
)
from app.finance_core.repositories.payments import FinancePaymentRepository
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
        self._session = session
        self._payment_service = FinancePaymentLedgerService(session)
        self._payment_repo = FinancePaymentRepository(session)
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
        payment_status = None
        state_applied = False
        state_ignored = False
        if command.payment_id is not None:
            payment_status, state_applied, state_ignored = await self._apply_payment_state(command)

        if result.replayed:
            return NormalizedProviderEventResult(
                payment_event_id=result.payment_event_id,
                provider_code=command.provider_code,
                provider_event_id=command.provider_event_id,
                event_type=command.event_type,
                raw_status=command.raw_status,
                payment_id=command.payment_id,
                payment_status=payment_status,
                state_applied=state_applied,
                state_ignored=state_ignored,
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
            payment_id=command.payment_id,
            payment_status=payment_status,
            state_applied=state_applied,
            state_ignored=state_ignored,
        )

    async def _apply_payment_state(self, command: NormalizedProviderEventCommand) -> tuple[str | None, bool, bool]:
        if command.payment_id is None:
            return None, False, False
        payment = await self._payment_repo.get_payment(command.payment_id, for_update=True)
        if payment is None:
            raise FinanceWebhookNormalizationError("Provider event references an unknown payment")
        if payment.provider_code != command.provider_code:
            raise FinanceWebhookNormalizationError("Provider event payment/provider mismatch")

        try:
            action = payment_state_transition_action(payment.status, command.raw_status)
        except FinancePaymentStateTransitionError:
            raise

        if action == "apply":
            previous_status = payment.status
            await self._payment_repo.update_payment_status(payment, status=command.raw_status, raw_status=command.raw_status)
            payload = {
                "payment_id": str(payment.id),
                "previous_status": previous_status,
                "status": payment.status,
                "provider_event_id": command.provider_event_id,
            }
            await self._payment_repo.create_outbox_event(
                organization_id=payment.organization_id,
                legal_entity_id=payment.legal_entity_id,
                division_id=payment.division_id,
                brand_id=payment.brand_id,
                aggregate_type="payment",
                aggregate_id=payment.id,
                event_type="finance.payment.state_changed",
                idempotency_key=f"{command.idempotency_key}:state",
                payload=payload,
            )
            return payment.status, True, False
        if action == "ignore_stale":
            return payment.status, False, True
        return payment.status, False, False


def is_duplicate_provider_event_error(error: Exception) -> bool:
    return isinstance(error, FinancePaymentConflictError) and "Provider payment event already exists" in str(error)
