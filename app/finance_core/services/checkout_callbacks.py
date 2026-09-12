from __future__ import annotations

import hashlib

from sqlalchemy.exc import DBAPIError

from sqlalchemy.ext.asyncio import AsyncSession

from app.finance_core.domain.checkout_callbacks import (
    CHECKOUT_CALLBACK_EVENT_TYPE,
    CHECKOUT_CALLBACK_SOURCE,
    CheckoutCallbackRecordingResult,
    FinanceCheckoutCallbackError,
    RecordCheckoutCallbackCommand,
    redact_provider_reference,
)
from app.finance_core.domain.invoice_engine import canonical_hash
from app.finance_core.domain.payment_ledger import FinancePaymentConflictError
from app.finance_core.domain.provider_boundary import (
    FinancePaymentStateTransitionError,
    payment_state_transition_action,
)
from app.finance_core.domain.razorpay_sandbox import RazorpaySandboxConfig
from app.finance_core.repositories.payments import FinancePaymentRepository
from app.finance_core.services.razorpay_checkout import (
    RazorpayCheckoutSignatureVerificationCommand,
    RazorpayCheckoutSignatureVerificationService,
)


PROVIDER_CODE = "razorpay_sandbox"
IDEMPOTENCY_SCOPE = "finance.checkout_callback.record"


class FinanceCheckoutCallbackRecordingService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        razorpay_config: RazorpaySandboxConfig,
        provider_code: str = PROVIDER_CODE,
    ):
        self._repo = FinancePaymentRepository(session)
        self._provider_code = provider_code
        self._signature_verifier = RazorpayCheckoutSignatureVerificationService(config=razorpay_config)

    async def record_verified_callback(
        self,
        command: RecordCheckoutCallbackCommand,
    ) -> CheckoutCallbackRecordingResult:
        verification = self._signature_verifier.verify(
            RazorpayCheckoutSignatureVerificationCommand(
                razorpay_order_id=command.razorpay_order_id,
                razorpay_payment_id=command.razorpay_payment_id,
                razorpay_signature=command.razorpay_signature,
            )
        )

        idempotency_key = _required_idempotency_key(
            command.idempotency_key
        )

        if command.source != CHECKOUT_CALLBACK_SOURCE:
            raise FinanceCheckoutCallbackError(
                "CHECKOUT_CALLBACK_SOURCE_INVALID",
                "Checkout callback source is not allowed.",
            )

        if (
            len(verification.provider_order_id) > 200
            or len(verification.provider_payment_id) > 200
        ):
            raise FinanceCheckoutCallbackError(
                "CHECKOUT_CALLBACK_REFERENCE_INVALID",
                "Checkout callback provider reference is invalid.",
            )

        payload = {
            "provider_code": self._provider_code,
            "provider_order_ref": verification.provider_order_id,
            "provider_payment_ref": verification.provider_payment_id,
            "event_type": CHECKOUT_CALLBACK_EVENT_TYPE,
            "source": command.source,
            "target_status": "authorized",
        }

        payload_hash = canonical_hash(payload)

        try:
            row = await self._repo.record_verified_checkout_callback(
                provider_code=self._provider_code,
                provider_order_ref=verification.provider_order_id,
                provider_payment_ref=verification.provider_payment_id,
                idempotency_key=idempotency_key,
                request_hash=payload_hash,
            )
        except DBAPIError as exc:
            error_text = str(exc)

            if (
                "P4D finance idempotency request conflict"
                in error_text
                or "P4D checkout callback already processing"
                in error_text
            ):
                raise FinancePaymentConflictError(
                    "Checkout callback conflicts with the existing "
                    "idempotency request."
                ) from exc

            mappings = (
                (
                    "P4D checkout callback order unavailable",
                    "CHECKOUT_CALLBACK_ORDER_NOT_FOUND",
                    "Checkout callback references an unknown "
                    "provider order.",
                ),
                (
                    "P4D checkout callback payment mismatch",
                    "CHECKOUT_CALLBACK_PAYMENT_MISMATCH",
                    "Checkout callback payment reference does not "
                    "match the existing payment.",
                ),
                (
                    "P4D checkout callback payment conflict",
                    "CHECKOUT_CALLBACK_PAYMENT_CONFLICT",
                    "Checkout payment reference is already bound "
                    "to another provider order.",
                ),
                (
                    "P4D checkout callback replay inconsistent",
                    "CHECKOUT_CALLBACK_REPLAY_INCONSISTENT",
                    "Checkout callback replay state is inconsistent.",
                ),
                (
                    "P4D checkout callback replay conflict",
                    "CHECKOUT_CALLBACK_REPLAY_CONFLICT",
                    "Checkout callback replay does not match the "
                    "recorded event.",
                ),
                (
                    "P4D checkout callback payment state invalid",
                    "CHECKOUT_CALLBACK_PAYMENT_STATE_INVALID",
                    "Checkout callback cannot update the current "
                    "payment state.",
                ),
                (
                    "P4D checkout callback tenant conflict",
                    "CHECKOUT_CALLBACK_TENANT_CONFLICT",
                    "Checkout callback tenant authority conflicts "
                    "with the current transaction.",
                ),
            )

            for marker, code, message in mappings:
                if marker in error_text:
                    raise FinanceCheckoutCallbackError(
                        code,
                        message,
                    ) from exc

            raise

        return CheckoutCallbackRecordingResult(
            payment_id=row["payment_id"],
            provider_order_ref=redact_provider_reference(
                verification.provider_order_id
            ),
            provider_payment_ref=redact_provider_reference(
                verification.provider_payment_id
            ),
            previous_payment_status=str(
                row["previous_status"]
            ),
            payment_status=str(
                row["payment_status"]
            ),
            verification_result="verified",
            event_recorded=bool(
                row["event_recorded"]
            ),
            replayed=bool(
                row["replayed"]
            ),
        )


def _required_idempotency_key(value: str) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not normalized or len(normalized) > 200:
        raise FinanceCheckoutCallbackError(
            "CHECKOUT_CALLBACK_IDEMPOTENCY_KEY_INVALID",
            "Checkout callback idempotency key is required and must be at most 200 characters.",
        )
    return normalized


def _callback_event_id(*, provider_order_ref: str, provider_payment_ref: str) -> str:
    digest = hashlib.sha256(
        f"{provider_order_ref}|{provider_payment_ref}".encode("utf-8")
    ).hexdigest()
    return f"checkout_callback:{digest}"


def _validate_provider_payment_ref(existing: str | None, callback_ref: str) -> None:
    if existing is not None and existing != callback_ref:
        raise FinanceCheckoutCallbackError(
            "CHECKOUT_CALLBACK_PAYMENT_MISMATCH",
            "Checkout callback payment reference does not match the existing payment.",
        )


def _validate_replay_event(existing_event, *, payment_id, payload_hash: str) -> None:
    if (
        existing_event is None
        or existing_event.payment_id != payment_id
        or existing_event.event_payload_sha256 != payload_hash
        or existing_event.event_type != CHECKOUT_CALLBACK_EVENT_TYPE
    ):
        raise FinanceCheckoutCallbackError(
            "CHECKOUT_CALLBACK_REPLAY_CONFLICT",
            "Checkout callback replay does not match the recorded event.",
        )


def _result(*, payment, previous_status: str, event_recorded: bool, replayed: bool) -> CheckoutCallbackRecordingResult:
    return CheckoutCallbackRecordingResult(
        payment_id=payment.id,
        provider_order_ref=redact_provider_reference(payment.provider_order_ref or ""),
        provider_payment_ref=redact_provider_reference(payment.provider_payment_ref or ""),
        previous_payment_status=previous_status,
        payment_status=payment.status,
        verification_result="verified",
        event_recorded=event_recorded,
        replayed=replayed,
    )
