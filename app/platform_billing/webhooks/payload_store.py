from __future__ import annotations

from dataclasses import dataclass, field

from app.platform_billing.domain.webhooks import (
    StoredWebhookPayload,
    WebhookPayloadStorageFailure,
)


@dataclass
class InMemoryEncryptedWebhookPayloadStore:
    fail_put: bool = False
    fail_get: bool = False
    payloads: dict[str, bytes] = field(default_factory=dict)
    put_calls: list[str] = field(default_factory=list)
    get_calls: list[str] = field(default_factory=list)
    delete_calls: list[str] = field(default_factory=list)

    async def put_verified_payload(
        self,
        *,
        provider_code: str,
        provider_event_id: str,
        payload_sha256: str,
        raw_body: bytes,
    ) -> StoredWebhookPayload:
        if self.fail_put:
            raise WebhookPayloadStorageFailure("Verified webhook payload could not be stored")
        pointer = f"mem-encrypted://{provider_code}/{provider_event_id}/{payload_sha256}"
        self.put_calls.append(pointer)
        self.payloads[pointer] = _simulate_encryption(raw_body)
        return StoredWebhookPayload(encrypted_payload_ref=pointer)

    async def get_verified_payload(self, encrypted_payload_ref: str) -> bytes:
        self.get_calls.append(encrypted_payload_ref)
        if self.fail_get:
            raise WebhookPayloadStorageFailure("Verified webhook payload could not be loaded")
        try:
            encrypted = self.payloads[encrypted_payload_ref]
        except KeyError as exc:
            raise WebhookPayloadStorageFailure("Verified webhook payload is missing") from exc
        return _simulate_encryption(encrypted)

    async def delete_uncommitted_payload(self, encrypted_payload_ref: str) -> None:
        self.delete_calls.append(encrypted_payload_ref)
        self.payloads.pop(encrypted_payload_ref, None)


def _simulate_encryption(raw_body: bytes) -> bytes:
    return raw_body[::-1]
