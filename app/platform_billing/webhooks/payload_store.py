from __future__ import annotations

import hashlib
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, unquote

from app.platform_billing.domain.webhooks import (
    StoredWebhookPayload,
    WebhookPayloadStorageFailure,
)


_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9_.:-]+$")


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


@dataclass(frozen=True)
class LocalEncryptedWebhookPayloadStore:
    root_dir: Path

    async def put_verified_payload(
        self,
        *,
        provider_code: str,
        provider_event_id: str,
        payload_sha256: str,
        raw_body: bytes,
    ) -> StoredWebhookPayload:
        path = self._path_for(
            provider_code=provider_code,
            provider_event_id=provider_event_id,
            payload_sha256=payload_sha256,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp.write_bytes(_simulate_encryption(raw_body))
            os.replace(temp, path)
        except OSError as exc:
            temp.unlink(missing_ok=True)
            raise WebhookPayloadStorageFailure("Verified webhook payload could not be stored") from exc
        return StoredWebhookPayload(encrypted_payload_ref=_pointer_for(path))

    async def get_verified_payload(self, encrypted_payload_ref: str) -> bytes:
        path = _path_from_pointer(encrypted_payload_ref)
        try:
            encrypted = path.read_bytes()
        except OSError as exc:
            raise WebhookPayloadStorageFailure("Verified webhook payload is missing") from exc
        return _simulate_encryption(encrypted)

    async def delete_uncommitted_payload(self, encrypted_payload_ref: str) -> None:
        try:
            _path_from_pointer(encrypted_payload_ref).unlink(missing_ok=True)
        except OSError:
            return

    def _path_for(self, *, provider_code: str, provider_event_id: str, payload_sha256: str) -> Path:
        provider = _safe_component(provider_code, "provider")
        payload = _safe_component(payload_sha256, "payload")
        event_hash = hashlib.sha256(provider_event_id.encode("utf-8")).hexdigest()
        return self.root_dir / provider / f"{payload}-{event_hash}.payload"


def _safe_component(value: str, label: str) -> str:
    if not value or not _SAFE_COMPONENT.fullmatch(value):
        raise WebhookPayloadStorageFailure(f"Invalid {label} payload storage component")
    return value


def _pointer_for(path: Path) -> str:
    return f"file-encrypted://{quote(str(path.resolve()), safe='/')}"


def _path_from_pointer(pointer: str) -> Path:
    prefix = "file-encrypted://"
    if not pointer.startswith(prefix):
        raise WebhookPayloadStorageFailure("Verified webhook payload reference is unsupported")
    return Path(unquote(pointer[len(prefix):]))


def _simulate_encryption(raw_body: bytes) -> bytes:
    return raw_body[::-1]
