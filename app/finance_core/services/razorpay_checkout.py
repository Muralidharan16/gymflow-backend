from __future__ import annotations

from dataclasses import dataclass

from app.finance_core.domain.razorpay_sandbox import (
    RazorpayCheckoutSignatureVerificationResult,
    RazorpaySandboxConfig,
    validate_razorpay_sandbox_config,
    verify_razorpay_checkout_signature,
)


@dataclass(frozen=True)
class RazorpayCheckoutSignatureVerificationCommand:
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


class RazorpayCheckoutSignatureVerificationService:
    def __init__(self, *, config: RazorpaySandboxConfig):
        self._config = validate_razorpay_sandbox_config(config)

    def verify(
        self,
        command: RazorpayCheckoutSignatureVerificationCommand,
    ) -> RazorpayCheckoutSignatureVerificationResult:
        return verify_razorpay_checkout_signature(
            razorpay_order_id=command.razorpay_order_id,
            razorpay_payment_id=command.razorpay_payment_id,
            razorpay_signature=command.razorpay_signature,
            key_secret=self._config.key_secret,
        )
