from __future__ import annotations

import re

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.finance_core.domain.invoice_engine import canonical_hash
from app.finance_core.domain.payment_ledger import FinancePaymentConflictError
from app.finance_core.domain.provider_boundary import (
    FinancePaymentStateTransitionError,
)
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
        evidence = _validated_evidence(
            command,
            expected_provider_code=self._provider_code,
        )
        idempotency_key = _required_idempotency_key(
            command.idempotency_key
        )
        payload_hash = canonical_hash(evidence)

        try:
            async with self._session.begin_nested():
                row = (
                    await self._repo
                    .confirm_provider_evidence_capability(
                        provider_code=command.provider_code,
                        provider_event_id=(
                            command.provider_event_id
                        ),
                        event_type=command.event_type,
                        provider_order_ref=(
                            command.provider_order_ref
                        ),
                        provider_payment_ref=(
                            command.provider_payment_ref
                        ),
                        provider_amount_subunits=(
                            command.provider_amount_subunits
                        ),
                        provider_currency=(
                            command.provider_currency
                        ),
                        provider_payment_status=(
                            command.provider_payment_status
                        ),
                        idempotency_key=idempotency_key,
                        request_hash=payload_hash,
                    )
                )
        except DBAPIError as exc:
            translated = (
                _translate_provider_evidence_db_error(exc)
            )
            if translated is None:
                raise
            raise translated from exc

        return ProviderPaymentEvidenceResult(
            payment_event_id=row["payment_event_id"],
            payment_id=row["payment_id"],
            provider_code=row["provider_code"],
            provider_event_id=row["provider_event_id"],
            event_type=row["event_type"],
            previous_payment_status=(
                row["previous_payment_status"]
            ),
            payment_status=row["payment_status"],
            event_recorded=row["event_recorded"],
            state_changed=row["state_changed"],
            state_ignored=row["state_ignored"],
            replayed=row["replayed"],
        )




def _translate_provider_evidence_db_error(
    exc: DBAPIError,
) -> Exception | None:
    message = str(
        getattr(exc, "orig", None) or exc
    )

    provider_errors = (
        (
            "P4D provider evidence payment not found",
            "PROVIDER_EVIDENCE_PAYMENT_NOT_FOUND",
            "Provider evidence references an unknown payment.",
        ),
        (
            "P4D provider evidence order mismatch",
            "PROVIDER_EVIDENCE_ORDER_MISMATCH",
            "Provider evidence order does not match its payment.",
        ),
        (
            "P4D provider evidence reference mismatch",
            "PROVIDER_EVIDENCE_REFERENCE_MISMATCH",
            (
                "Provider order and payment references do not "
                "identify the same payment."
            ),
        ),
        (
            "P4D provider evidence payment mismatch",
            "PROVIDER_EVIDENCE_PAYMENT_MISMATCH",
            (
                "Provider payment reference does not match "
                "the existing payment."
            ),
        ),
        (
            "P4D provider evidence amount mismatch",
            "PROVIDER_AMOUNT_MISMATCH",
            (
                "Provider payment amount does not match "
                "the server payment."
            ),
        ),
        (
            "P4D provider evidence server amount invalid",
            "SERVER_PAYMENT_AMOUNT_INVALID",
            (
                "Server payment amount cannot be represented "
                "in provider subunits."
            ),
        ),
        (
            "P4D provider evidence currency mismatch",
            "PROVIDER_CURRENCY_MISMATCH",
            (
                "Provider payment currency does not match "
                "the server payment."
            ),
        ),
        (
            "P4D provider evidence tenant conflict",
            "PROVIDER_EVIDENCE_TENANT_CONFLICT",
            "Provider evidence tenant context conflicts.",
        ),
        (
            "P4D provider evidence existing tenant context invalid",
            "PROVIDER_EVIDENCE_TENANT_CONFLICT",
            "Provider evidence tenant context is invalid.",
        ),
    )

    for token, code, detail in provider_errors:
        if token in message:
            return FinanceProviderEvidenceError(
                code,
                detail,
            )

    if (
        "P4D provider evidence state transition invalid"
        in message
    ):
        return FinancePaymentStateTransitionError(
            "Invalid provider payment state transition"
        )

    conflict_tokens = (
        "P4D provider evidence event conflict",
        "P4D provider evidence already processing",
        "P4D provider evidence idempotency conflict",
        "P4D provider evidence payment conflict",
        "P4D finance idempotency request conflict",
    )

    if any(
        token in message
        for token in conflict_tokens
    ):
        return FinancePaymentConflictError(
            "Provider evidence conflicts with existing finance state"
        )

    return None

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
