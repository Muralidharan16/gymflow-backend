from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.finance_core.domain.provider_capture_confirmation import (
    ConfirmProviderPaymentEvidenceCommand,
)
from app.finance_core.domain.razorpay_webhooks import RazorpayWebhookPaymentReference


@dataclass(frozen=True)
class ProviderWebhookReceipt:
    inbox_id: uuid.UUID
    status: str
    processing_attempts: int
    replayed: bool


@dataclass(frozen=True)
class ClaimedProviderWebhook:
    inbox_id: uuid.UUID
    status: str
    lease_fence: int
    processing_attempts: int
    claimed: bool
    provider_code: str
    environment: str
    provider_event_id: str
    payload_sha256: str
    event_type: str
    provider_order_ref: str
    provider_payment_ref: str
    provider_amount_subunits: int
    provider_currency: str
    provider_payment_status: str
    provider_captured: bool
    provider_payment_order_ref: str
    provider_order_entity_ref: str | None
    provider_order_status: str | None
    provider_event_timestamp: int | None

    def confirmation_command(self) -> ConfirmProviderPaymentEvidenceCommand:
        return ConfirmProviderPaymentEvidenceCommand(
            provider_code=self.provider_code,
            provider_event_id=self.provider_event_id,
            event_type=self.event_type,
            provider_order_ref=self.provider_order_ref,
            provider_payment_ref=self.provider_payment_ref,
            provider_amount_subunits=self.provider_amount_subunits,
            provider_currency=self.provider_currency,
            provider_payment_status=self.provider_payment_status,
            provider_captured=self.provider_captured,
            provider_payment_order_ref=self.provider_payment_order_ref,
            provider_order_entity_ref=self.provider_order_entity_ref,
            provider_order_status=self.provider_order_status,
            provider_event_timestamp=self.provider_event_timestamp,
            idempotency_key=f"pay8:webhook:{self.inbox_id}",
            webhook_signature_verified=True,
        )


class FinanceProviderWebhookInboxService:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def record_verified(
        self,
        *,
        provider_code: str,
        environment: str,
        reference: RazorpayWebhookPaymentReference,
        raw_body: bytes,
        signature: str,
    ) -> ProviderWebhookReceipt:
        payload_hash = hashlib.sha256(raw_body).hexdigest()
        signature_hash = hashlib.sha256(
            signature.encode("utf-8")
        ).hexdigest()
        result = await self._session.execute(
            text(
                """
                SELECT *
                FROM app_secure.record_finance_provider_webhook(
                    :provider_code,
                    :environment,
                    :provider_event_id,
                    :payload_sha256,
                    :signature_sha256,
                    :event_type,
                    :provider_order_ref,
                    :provider_payment_ref,
                    :provider_amount_subunits,
                    :provider_currency,
                    :provider_payment_status,
                    :provider_captured,
                    :provider_payment_order_ref,
                    :provider_order_entity_ref,
                    :provider_order_status,
                    :provider_event_timestamp
                )
                """
            ),
            {
                "provider_code": provider_code,
                "environment": environment,
                "provider_event_id": reference.provider_event_id,
                "payload_sha256": payload_hash,
                "signature_sha256": signature_hash,
                "event_type": reference.event_type,
                "provider_order_ref": reference.provider_order_id,
                "provider_payment_ref": reference.provider_payment_id,
                "provider_amount_subunits": reference.provider_amount_subunits,
                "provider_currency": reference.provider_currency,
                "provider_payment_status": reference.provider_payment_status,
                "provider_captured": reference.provider_captured,
                "provider_payment_order_ref": reference.provider_payment_order_id,
                "provider_order_entity_ref": reference.provider_order_entity_id,
                "provider_order_status": reference.provider_order_status,
                "provider_event_timestamp": reference.provider_event_timestamp,
            },
        )
        return ProviderWebhookReceipt(**dict(result.mappings().one()))

    async def claim(
        self,
        *,
        inbox_id: uuid.UUID,
        lease_owner: uuid.UUID,
    ) -> ClaimedProviderWebhook:
        result = await self._session.execute(
            text(
                """
                SELECT *
                FROM app_secure.claim_finance_provider_webhook(
                    :inbox_id,
                    :lease_owner
                )
                """
            ),
            {"inbox_id": inbox_id, "lease_owner": lease_owner},
        )
        return ClaimedProviderWebhook(**dict(result.mappings().one()))

    async def claim_next(
        self,
        *,
        lease_owner: uuid.UUID,
    ) -> ClaimedProviderWebhook | None:
        result = await self._session.execute(
            text(
                """
                SELECT *
                FROM app_secure.claim_next_finance_provider_webhook(
                    :lease_owner
                )
                """
            ),
            {"lease_owner": lease_owner},
        )
        row = result.mappings().one_or_none()
        return ClaimedProviderWebhook(**dict(row)) if row is not None else None

    async def complete(
        self,
        *,
        inbox_id: uuid.UUID,
        lease_owner: uuid.UUID,
        lease_fence: int,
        payment_event_id: uuid.UUID,
    ) -> None:
        await self._session.execute(
            text(
                """
                SELECT *
                FROM app_secure.complete_finance_provider_webhook(
                    :inbox_id,
                    :lease_owner,
                    :lease_fence,
                    :payment_event_id
                )
                """
            ),
            {
                "inbox_id": inbox_id,
                "lease_owner": lease_owner,
                "lease_fence": lease_fence,
                "payment_event_id": payment_event_id,
            },
        )

    async def fail(
        self,
        *,
        inbox_id: uuid.UUID,
        lease_owner: uuid.UUID,
        lease_fence: int,
        error_code: str,
        retryable: bool,
    ) -> str:
        result = await self._session.execute(
            text(
                """
                SELECT *
                FROM app_secure.fail_finance_provider_webhook(
                    :inbox_id,
                    :lease_owner,
                    :lease_fence,
                    :error_code,
                    :retryable
                )
                """
            ),
            {
                "inbox_id": inbox_id,
                "lease_owner": lease_owner,
                "lease_fence": lease_fence,
                "error_code": error_code,
                "retryable": retryable,
            },
        )
        return str(result.mappings().one()["status"])
