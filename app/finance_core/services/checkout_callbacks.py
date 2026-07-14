from __future__ import annotations

import hashlib

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
        idempotency_key = _required_idempotency_key(command.idempotency_key)
        if command.source != CHECKOUT_CALLBACK_SOURCE:
            raise FinanceCheckoutCallbackError(
                "CHECKOUT_CALLBACK_SOURCE_INVALID",
                "Checkout callback source is not allowed.",
            )
        if len(verification.provider_order_id) > 200 or len(verification.provider_payment_id) > 200:
            raise FinanceCheckoutCallbackError(
                "CHECKOUT_CALLBACK_REFERENCE_INVALID",
                "Checkout callback provider reference is invalid.",
            )

        payment = await self._repo.get_payment_by_provider_order_ref(
            provider_code=self._provider_code,
            provider_order_ref=verification.provider_order_id,
            for_update=True,
        )
        if payment is None:
            raise FinanceCheckoutCallbackError(
                "CHECKOUT_CALLBACK_ORDER_NOT_FOUND",
                "Checkout callback references an unknown provider order.",
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
        event_id = _callback_event_id(
            provider_order_ref=verification.provider_order_id,
            provider_payment_ref=verification.provider_payment_id,
        )
        idem, idempotency_created = await self._repo.reserve_idempotency_key(
            organization_id=payment.organization_id,
            scope=IDEMPOTENCY_SCOPE,
            idempotency_key=idempotency_key,
            request_hash=payload_hash,
        )

        existing_event = await self._repo.get_payment_event_by_provider_id(
            provider_code=self._provider_code,
            provider_event_id=event_id,
            for_update=True,
        )
        if not idempotency_created:
            if not idem.response_ref:
                raise FinancePaymentConflictError(
                    "Checkout callback is already processing for this idempotency key"
                )
            _validate_replay_event(existing_event, payment_id=payment.id, payload_hash=payload_hash)
            _validate_provider_payment_ref(payment.provider_payment_ref, verification.provider_payment_id)
            return _result(
                payment=payment,
                previous_status=payment.status,
                event_recorded=False,
                replayed=True,
            )

        _validate_provider_payment_ref(payment.provider_payment_ref, verification.provider_payment_id)
        payment_with_ref = await self._repo.get_payment_by_provider_ref(
            provider_code=self._provider_code,
            provider_payment_ref=verification.provider_payment_id,
            for_update=True,
        )
        if payment_with_ref is not None and payment_with_ref.id != payment.id:
            raise FinanceCheckoutCallbackError(
                "CHECKOUT_CALLBACK_PAYMENT_CONFLICT",
                "Checkout payment reference is already bound to another provider order.",
            )

        if existing_event is not None:
            _validate_replay_event(existing_event, payment_id=payment.id, payload_hash=payload_hash)
            if payment.provider_payment_ref is None:
                raise FinanceCheckoutCallbackError(
                    "CHECKOUT_CALLBACK_REPLAY_INCONSISTENT",
                    "Checkout callback replay state is inconsistent.",
                )
            await self._repo.complete_idempotency_key(idem, response_ref=str(existing_event.id))
            return _result(
                payment=payment,
                previous_status=payment.status,
                event_recorded=False,
                replayed=True,
            )

        previous_status = payment.status
        try:
            transition_action = payment_state_transition_action(previous_status, "authorized")
        except FinancePaymentStateTransitionError as exc:
            raise FinanceCheckoutCallbackError(
                "CHECKOUT_CALLBACK_PAYMENT_STATE_INVALID",
                "Checkout callback cannot update the current payment state.",
            ) from exc

        if payment.provider_payment_ref is None:
            await self._repo.set_provider_payment_ref(
                payment,
                provider_payment_ref=verification.provider_payment_id,
            )
        event = await self._repo.create_payment_event(
            payment_id=payment.id,
            provider_code=self._provider_code,
            provider_event_id=event_id,
            event_type=CHECKOUT_CALLBACK_EVENT_TYPE,
            event_payload_sha256=payload_hash,
        )

        if transition_action == "apply":
            await self._repo.update_payment_status(
                payment,
                status="authorized",
                raw_status="authorized",
            )
            await self._repo.create_outbox_event(
                organization_id=payment.organization_id,
                legal_entity_id=payment.legal_entity_id,
                division_id=payment.division_id,
                brand_id=payment.brand_id,
                aggregate_type="payment",
                aggregate_id=payment.id,
                event_type="finance.payment.state_changed",
                idempotency_key=f"{event_id}:state",
                payload={
                    "payment_id": str(payment.id),
                    "previous_status": previous_status,
                    "status": payment.status,
                    "provider_event_id": event_id,
                },
            )

        await self._repo.complete_idempotency_key(idem, response_ref=str(event.id))
        return _result(
            payment=payment,
            previous_status=previous_status,
            event_recorded=True,
            replayed=transition_action != "apply",
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
