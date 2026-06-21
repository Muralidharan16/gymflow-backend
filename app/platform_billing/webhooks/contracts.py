from __future__ import annotations

from typing import Protocol

from app.platform_billing.domain.webhooks import (
    StoredWebhookPayload,
    VerifiedWebhook,
    WebhookEnvelope,
)


class WebhookSignatureVerifier(Protocol):
    async def verify(self, envelope: WebhookEnvelope) -> VerifiedWebhook:
        """Verify exact raw webhook bytes and return safe normalized identity."""


class EncryptedWebhookPayloadStore(Protocol):
    async def put_verified_payload(
        self,
        *,
        provider_code: str,
        provider_event_id: str,
        payload_sha256: str,
        raw_body: bytes,
    ) -> StoredWebhookPayload:
        """Persist a verified raw payload and return an opaque encrypted-storage pointer."""

    async def get_verified_payload(self, encrypted_payload_ref: str) -> bytes:
        """Load a verified encrypted payload by opaque pointer."""

    async def delete_uncommitted_payload(self, encrypted_payload_ref: str) -> None:
        """Best-effort cleanup when DB acceptance fails after storage succeeds."""
