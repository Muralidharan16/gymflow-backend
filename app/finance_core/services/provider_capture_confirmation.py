from __future__ import annotations

import hashlib
import re
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.finance_core.domain.invoice_engine import canonical_hash
from app.finance_core.domain.payment_ledger import FinancePaymentConflictError
from app.finance_core.domain.provider_boundary import payment_state_transition_action
from app.finance_core.domain.provider_capture_confirmation import (
    SUPPORTED_CAPTURE_CONFIRMATION_EVENTS,
    VERIFIED_RAZORPAY_WEBHOOK_SOURCE,
    ConfirmProviderPaymentEvidenceCommand,
    FinanceProviderEvidenceError,
    ProviderPaymentEvidenceResult,
)
from app.finance_core.repositories.payments import FinancePaymentRepository


IDEMPOTENCY_SCOPE = "finance.provider.capture.confirm"
_PROVIDER_CODE_PATTERN = re.compile(r"^[a-z0-9_]{1,40}$")
_PROVIDER_REFERENCE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
_PROVIDER_EVENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
_EVENT_TARGET_STATUS = {
    "payment.authorized": "authorized",
    "payment.captured": "captured",
    "payment.failed": "failed",
    "order.paid": "captured",
}


class FinanceProviderCaptureConfirmationService:
    """Apply trusted provider evidence to payment state without accounting."""

    def __init__(self, session: AsyncSession, *, provider_code: str):
        self._session = session
        self._repo = FinancePaymentRepository(session)
        self._provider_code = _required_provider_code(provider_code)

    async def confirm_provider_evidence(
        self,
        command: ConfirmProviderPaymentEvidenceCommand,
    ) -> ProviderPaymentEvidenceResult:
        evidence = _validated_evidence(command, expected_provider_code=self._provider_code)
        idempotency_key = _required_idempotency_key(command.idempotency_key)
        payload_hash = canonical_hash(evidence)
        target_status = _EVENT_TARGET_STATUS[command.event_type]

        async with self._session.begin_nested():
            await self._repo.acquire_provider_event_lock(
                provider_code=command.provider_code,
                provider_event_id=command.provider_event_id,
            )
            payment = await self._locked_matching_payment(command)
            _validate_payment_value(payment, command)

            existing_event = await self._repo.get_payment_event_by_provider_id(
                provider_code=command.provider_code,
                provider_event_id=command.provider_event_id,
                for_update=True,
            )
            if existing_event is not None:
                _validate_existing_event(
                    existing_event,
                    payment_id=payment.id,
                    event_type=command.event_type,
                    payload_hash=payload_hash,
                )
                idem, created = await self._repo.reserve_idempotency_key(
                    organization_id=payment.organization_id,
                    scope=IDEMPOTENCY_SCOPE,
                    idempotency_key=idempotency_key,
                    request_hash=payload_hash,
                )
                if created:
                    await self._repo.complete_idempotency_key(idem, response_ref=str(existing_event.id))
                elif not idem.response_ref:
                    raise FinancePaymentConflictError(
                        "Provider evidence is already processing for this request idempotency key"
                    )
                return ProviderPaymentEvidenceResult(
                    payment_event_id=existing_event.id,
                    payment_id=payment.id,
                    provider_code=command.provider_code,
                    provider_event_id=command.provider_event_id,
                    event_type=command.event_type,
                    previous_payment_status=payment.status,
                    payment_status=payment.status,
                    event_recorded=False,
                    state_changed=False,
                    state_ignored=payment.status != target_status,
                    replayed=True,
                )

            idem, created = await self._repo.reserve_idempotency_key(
                organization_id=payment.organization_id,
                scope=IDEMPOTENCY_SCOPE,
                idempotency_key=idempotency_key,
                request_hash=payload_hash,
            )
            if not created:
                raise FinancePaymentConflictError(
                    "Provider evidence idempotency replay is inconsistent with its provider event"
                )

            transition_action = payment_state_transition_action(payment.status, target_status)

            if payment.provider_payment_ref is None:
                await self._repo.set_provider_payment_ref(
                    payment,
                    provider_payment_ref=command.provider_payment_ref,
                )

            event = await self._repo.create_payment_event(
                payment_id=payment.id,
                provider_code=command.provider_code,
                provider_event_id=command.provider_event_id,
                event_type=command.event_type,
                event_payload_sha256=payload_hash,
            )
            previous_status = payment.status
            state_changed = transition_action == "apply"
            state_ignored = transition_action == "ignore_stale"
            if state_changed:
                await self._repo.update_payment_status(
                    payment,
                    status=target_status,
                    raw_status=command.provider_payment_status,
                )
                await self._repo.create_outbox_event(
                    organization_id=payment.organization_id,
                    legal_entity_id=payment.legal_entity_id,
                    division_id=payment.division_id,
                    brand_id=payment.brand_id,
                    aggregate_type="payment",
                    aggregate_id=payment.id,
                    event_type="finance.payment.state_changed",
                    idempotency_key=_state_outbox_idempotency_key(
                        provider_code=command.provider_code,
                        provider_event_id=command.provider_event_id,
                    ),
                    payload={
                        "payment_id": str(payment.id),
                        "previous_status": previous_status,
                        "status": payment.status,
                        "provider_event_id": command.provider_event_id,
                    },
                )

            await self._repo.complete_idempotency_key(idem, response_ref=str(event.id))
            return ProviderPaymentEvidenceResult(
                payment_event_id=event.id,
                payment_id=payment.id,
                provider_code=command.provider_code,
                provider_event_id=command.provider_event_id,
                event_type=command.event_type,
                previous_payment_status=previous_status,
                payment_status=payment.status,
                event_recorded=True,
                state_changed=state_changed,
                state_ignored=state_ignored,
                replayed=False,
            )

    async def _locked_matching_payment(self, command: ConfirmProviderPaymentEvidenceCommand):
        candidates = await self._repo.get_payments_by_provider_references(
            provider_code=command.provider_code,
            provider_order_ref=command.provider_order_ref,
            provider_payment_ref=command.provider_payment_ref,
            for_update=True,
        )
        payments_by_order = [
            payment for payment in candidates if payment.provider_order_ref == command.provider_order_ref
        ]
        payment_by_ref = next(
            (payment for payment in candidates if payment.provider_payment_ref == command.provider_payment_ref),
            None,
        )
        if not payments_by_order:
            if payment_by_ref is not None:
                raise FinanceProviderEvidenceError(
                    "PROVIDER_EVIDENCE_ORDER_MISMATCH",
                    "Provider evidence order does not match its payment.",
                )
            raise FinanceProviderEvidenceError(
                "PROVIDER_EVIDENCE_PAYMENT_NOT_FOUND",
                "Provider evidence references an unknown payment.",
            )
        if len(payments_by_order) != 1:
            raise FinanceProviderEvidenceError(
                "PROVIDER_EVIDENCE_ORDER_AMBIGUOUS",
                "Provider order reference is associated with multiple payments.",
            )
        payment_by_order = payments_by_order[0]
        if payment_by_ref is not None and payment_by_ref.id != payment_by_order.id:
            raise FinanceProviderEvidenceError(
                "PROVIDER_EVIDENCE_REFERENCE_MISMATCH",
                "Provider order and payment references do not identify the same payment.",
            )
        if (
            payment_by_order.provider_payment_ref is not None
            and payment_by_order.provider_payment_ref != command.provider_payment_ref
        ):
            raise FinanceProviderEvidenceError(
                "PROVIDER_EVIDENCE_PAYMENT_MISMATCH",
                "Provider payment reference does not match the existing payment.",
            )
        return payment_by_order


def _validated_evidence(
    command: ConfirmProviderPaymentEvidenceCommand,
    *,
    expected_provider_code: str,
) -> dict[str, object]:
    if command.webhook_signature_verified is not True:
        raise FinanceProviderEvidenceError(
            "PROVIDER_EVIDENCE_UNVERIFIED",
            "Provider evidence must come from the verified webhook boundary.",
        )
    if command.source != VERIFIED_RAZORPAY_WEBHOOK_SOURCE:
        raise FinanceProviderEvidenceError(
            "PROVIDER_EVIDENCE_SOURCE_INVALID",
            "Provider evidence source is not allowed.",
        )
    provider_code = _required_provider_code(command.provider_code)
    if provider_code != expected_provider_code:
        raise FinanceProviderEvidenceError(
            "PROVIDER_EVIDENCE_PROVIDER_MISMATCH",
            "Provider evidence does not match the configured provider.",
        )
    if not _PROVIDER_EVENT_ID_PATTERN.fullmatch(command.provider_event_id):
        raise FinanceProviderEvidenceError(
            "PROVIDER_EVENT_ID_INVALID",
            "Provider event id is required and must use the supported format.",
        )
    if command.event_type not in SUPPORTED_CAPTURE_CONFIRMATION_EVENTS:
        raise FinanceProviderEvidenceError(
            "PROVIDER_EVENT_TYPE_UNSUPPORTED",
            "Provider event type is not supported.",
        )
    _required_reference(command.provider_order_ref, "PROVIDER_ORDER_REF_INVALID")
    _required_reference(command.provider_payment_ref, "PROVIDER_PAYMENT_REF_INVALID")
    _required_reference(command.provider_payment_order_ref, "PROVIDER_PAYMENT_ORDER_REF_INVALID")
    if command.provider_payment_order_ref != command.provider_order_ref:
        raise FinanceProviderEvidenceError(
            "PROVIDER_EVIDENCE_ORDER_MISMATCH",
            "Provider payment does not belong to the supplied order.",
        )
    if (
        isinstance(command.provider_amount_subunits, bool)
        or not isinstance(command.provider_amount_subunits, int)
        or command.provider_amount_subunits < 0
    ):
        raise FinanceProviderEvidenceError(
            "PROVIDER_AMOUNT_INVALID",
            "Provider payment amount is missing or invalid.",
        )
    if not isinstance(command.provider_currency, str) or not _CURRENCY_PATTERN.fullmatch(command.provider_currency):
        raise FinanceProviderEvidenceError(
            "PROVIDER_CURRENCY_INVALID",
            "Provider payment currency is missing or invalid.",
        )
    expected_status = _EVENT_TARGET_STATUS[command.event_type]
    if command.provider_payment_status != expected_status:
        raise FinanceProviderEvidenceError(
            "PROVIDER_PAYMENT_STATUS_MISMATCH",
            "Provider payment status does not match the event type.",
        )
    if command.provider_captured is not None and not isinstance(command.provider_captured, bool):
        raise FinanceProviderEvidenceError(
            "PROVIDER_CAPTURED_FLAG_INVALID",
            "Provider captured flag is invalid.",
        )
    if command.event_type in {"payment.authorized", "payment.failed"} and command.provider_captured is True:
        raise FinanceProviderEvidenceError(
            "PROVIDER_CAPTURED_FLAG_MISMATCH",
            "Provider captured flag does not match the event type.",
        )
    if command.event_type in {"payment.captured", "order.paid"} and command.provider_captured is False:
        raise FinanceProviderEvidenceError(
            "PROVIDER_CAPTURED_FLAG_MISMATCH",
            "Provider evidence does not confirm capture.",
        )
    if command.provider_event_timestamp is not None and (
        isinstance(command.provider_event_timestamp, bool)
        or not isinstance(command.provider_event_timestamp, int)
        or command.provider_event_timestamp < 0
    ):
        raise FinanceProviderEvidenceError(
            "PROVIDER_EVENT_TIMESTAMP_INVALID",
            "Provider event timestamp is invalid.",
        )
    if command.event_type == "order.paid":
        _required_reference(command.provider_order_entity_ref, "PROVIDER_ORDER_ENTITY_REF_INVALID")
        if command.provider_order_entity_ref != command.provider_order_ref:
            raise FinanceProviderEvidenceError(
                "PROVIDER_ORDER_ENTITY_MISMATCH",
                "Provider order entity does not match the payment order.",
            )
        if command.provider_order_status != "paid":
            raise FinanceProviderEvidenceError(
                "PROVIDER_ORDER_STATUS_MISMATCH",
                "Provider order status does not confirm payment.",
            )

    return {
        "provider_code": provider_code,
        "provider_event_id": command.provider_event_id,
        "event_type": command.event_type,
        "provider_order_ref": command.provider_order_ref,
        "provider_payment_ref": command.provider_payment_ref,
        "provider_amount_subunits": command.provider_amount_subunits,
        "provider_currency": command.provider_currency,
        "provider_payment_status": command.provider_payment_status,
        "provider_captured": command.provider_captured,
        "provider_payment_order_ref": command.provider_payment_order_ref,
        "provider_order_entity_ref": command.provider_order_entity_ref,
        "provider_order_status": command.provider_order_status,
        "provider_event_timestamp": command.provider_event_timestamp,
        "source": command.source,
    }


def _required_idempotency_key(value: str) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not normalized or len(normalized) > 200:
        raise FinanceProviderEvidenceError(
            "PROVIDER_EVIDENCE_IDEMPOTENCY_INVALID",
            "Provider evidence request idempotency key is required and bounded.",
        )
    return normalized


def _required_provider_code(value: str) -> str:
    if not isinstance(value, str) or not _PROVIDER_CODE_PATTERN.fullmatch(value):
        raise FinanceProviderEvidenceError(
            "PROVIDER_CODE_INVALID",
            "Provider code is required and must use the supported format.",
        )
    return value


def _required_reference(value: str | None, code: str) -> str:
    if not isinstance(value, str) or not _PROVIDER_REFERENCE_PATTERN.fullmatch(value):
        raise FinanceProviderEvidenceError(code, "Provider reference is required and must use the supported format.")
    return value


def _validate_payment_value(payment, command: ConfirmProviderPaymentEvidenceCommand) -> None:
    amount_subunits = Decimal(payment.amount) * 100
    if amount_subunits != amount_subunits.to_integral_value():
        raise FinanceProviderEvidenceError(
            "SERVER_PAYMENT_AMOUNT_INVALID",
            "Server payment amount cannot be represented in provider subunits.",
        )
    if int(amount_subunits) != command.provider_amount_subunits:
        raise FinanceProviderEvidenceError(
            "PROVIDER_AMOUNT_MISMATCH",
            "Provider payment amount does not match the server payment.",
        )
    if payment.currency_code != command.provider_currency:
        raise FinanceProviderEvidenceError(
            "PROVIDER_CURRENCY_MISMATCH",
            "Provider payment currency does not match the server payment.",
        )


def _validate_existing_event(
    existing_event,
    *,
    payment_id,
    event_type: str,
    payload_hash: str,
) -> None:
    if (
        existing_event.payment_id != payment_id
        or existing_event.event_type != event_type
        or existing_event.event_payload_sha256 != payload_hash
    ):
        raise FinancePaymentConflictError(
            "Provider event id already exists with different normalized evidence"
        )


def _state_outbox_idempotency_key(*, provider_code: str, provider_event_id: str) -> str:
    digest = hashlib.sha256(f"{provider_code}|{provider_event_id}".encode("utf-8")).hexdigest()
    return f"provider-state:{digest}"
