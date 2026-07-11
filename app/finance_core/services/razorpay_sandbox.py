from __future__ import annotations

from app.finance_core.domain.provider_boundary import (
    ProviderCheckoutIntentRequest,
    ProviderCheckoutIntentResponse,
)
from app.finance_core.domain.razorpay_sandbox import (
    RazorpayCheckoutFields,
    RazorpayOrderCreateRequest,
    RazorpaySandboxClient,
    RazorpaySandboxConfig,
    amount_to_razorpay_subunits,
    validate_razorpay_sandbox_config,
    verify_razorpay_webhook_signature,
)
from app.finance_core.services.operational_guards import FinanceOperationalGuardService


class RazorpaySandboxAdapter:
    provider_code = "razorpay_sandbox"

    def __init__(
        self,
        *,
        config: RazorpaySandboxConfig,
        client: RazorpaySandboxClient,
        guard_service: FinanceOperationalGuardService | None = None,
    ):
        self._config = validate_razorpay_sandbox_config(config)
        self._client = client
        self._guard_service = guard_service or FinanceOperationalGuardService()

    async def create_checkout_intent(
        self,
        request: ProviderCheckoutIntentRequest,
    ) -> ProviderCheckoutIntentResponse:
        self._guard_service.require_safe_preflight()
        order_request = self.build_order_request(request)
        order_response = await self._client.create_order(order_request)
        if order_response.amount_subunits != order_request.amount_subunits:
            raise ValueError("Razorpay order response amount mismatch")
        if order_response.currency_code.upper() != order_request.currency_code:
            raise ValueError("Razorpay order response currency mismatch")
        return ProviderCheckoutIntentResponse(
            provider_code=self.provider_code,
            provider_order_ref=order_response.order_id,
            status=order_response.status,
        )

    def build_order_request(self, request: ProviderCheckoutIntentRequest) -> RazorpayOrderCreateRequest:
        amount_subunits = amount_to_razorpay_subunits(request.amount)
        currency_code = request.currency_code.upper()
        receipt = f"fin_{request.invoice_id.hex[:32]}"
        return RazorpayOrderCreateRequest(
            amount_subunits=amount_subunits,
            currency_code=currency_code,
            receipt=receipt,
            notes={
                "finance_invoice_id": str(request.invoice_id),
                "finance_idempotency_key": request.idempotency_key,
            },
        )

    def checkout_fields(self, *, order_id: str) -> RazorpayCheckoutFields:
        return RazorpayCheckoutFields(key_id=self._config.key_id, order_id=order_id)

    def verify_webhook_signature(self, *, raw_body: bytes, signature: str) -> bool:
        return verify_razorpay_webhook_signature(
            raw_body=raw_body,
            signature=signature,
            webhook_secret=self._config.webhook_secret,
        )
