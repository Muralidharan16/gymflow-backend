"""Production AWS KMS adapter for tenant-scoped envelope data keys.

This module intentionally does not derive wrapping keys from application
secrets. AWS KMS owns the wrapping key. New registration-key provisioning uses
GenerateDataKeyWithoutPlaintext so concurrent losers never receive plaintext
DEKs. When plaintext is actually required, decryption is bound to the exact
KMS key identifier stored alongside the wrapped DEK.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import boto3

from app.core.crypto import _guardrails, kms_bulkhead


_PURPOSE = "doers-envelope-dek-v1"
_REGISTRATION_DOMAIN = "organization_registrations"


class AWSKMSUnavailableError(RuntimeError):
    """AWS KMS is operationally unavailable for a bounded crypto operation."""


class AWSKMSContractError(RuntimeError):
    """AWS KMS returned key material that violates the expected response contract."""


class RegistrationKMSConfigurationError(RuntimeError):
    """Registration KMS configuration is absent or unsafe for this environment."""


@dataclass(slots=True)
class EncryptedDataKey:
    """A durable KMS-wrapped DEK plus the exact wrapping-key identity."""

    ciphertext: bytes
    key_id: str


@dataclass(slots=True)
class GeneratedDataKey(EncryptedDataKey):
    """A short-lived plaintext DEK paired with its durable KMS ciphertext."""

    plaintext: bytearray

    def zeroize(self) -> None:
        for index in range(len(self.plaintext)):
            self.plaintext[index] = 0


class AWSKMSProvider:
    """Bound AWS KMS operations for one tenant and one non-secret data domain."""

    def __init__(
        self,
        *,
        key_id: str,
        region_name: str,
        tenant_id: str,
        data_domain: str,
        endpoint_url: str | None = None,
        client: Any | None = None,
    ) -> None:
        key_id = key_id.strip()
        region_name = region_name.strip()
        tenant_id = tenant_id.strip()
        data_domain = data_domain.strip()
        endpoint_url = endpoint_url.strip() if endpoint_url else None
        if not key_id:
            raise ValueError("AWS KMS key id is required")
        if not region_name:
            raise ValueError("AWS KMS region is required")
        if not tenant_id:
            raise ValueError("AWS KMS tenant id is required")
        if not data_domain:
            raise ValueError("AWS KMS data domain is required")

        self._key_id = key_id
        self._region_name = region_name
        self._encryption_context = {
            "doers:tenant_id": tenant_id,
            "doers:data_domain": data_domain,
            "doers:purpose": _PURPOSE,
        }
        # Use the normal boto3 credential provider chain. Production should use
        # workload/IAM-role credentials rather than embedding credentials here.
        self._client = client or boto3.client(
            "kms",
            region_name=region_name,
            endpoint_url=endpoint_url,
        )

    @property
    def encryption_context(self) -> dict[str, str]:
        return dict(self._encryption_context)

    async def _call(self, operation_name: str, **kwargs: Any) -> dict[str, Any]:
        breaker = await kms_bulkhead.get_breaker(
            self._region_name,
            account="aws-kms",
        )
        if not await breaker.allow_request():
            raise AWSKMSUnavailableError(
                f"AWS KMS circuit breaker is open for region={self._region_name}"
            )

        operation = getattr(self._client, operation_name)
        async with _guardrails.kms_decrypts:
            try:
                response = await asyncio.to_thread(operation, **kwargs)
            except Exception as exc:
                await breaker.record_failure()
                raise AWSKMSUnavailableError(
                    f"AWS KMS {operation_name} failed"
                ) from exc
            else:
                await breaker.record_success()
                return response

    @staticmethod
    def _wrapped_key(response: dict[str, Any]) -> EncryptedDataKey:
        ciphertext = response.get("CiphertextBlob")
        key_id = response.get("KeyId")
        if not isinstance(ciphertext, (bytes, bytearray)) or not ciphertext:
            raise AWSKMSContractError(
                "AWS KMS returned an invalid encrypted data key"
            )
        if not isinstance(key_id, str) or not key_id.strip():
            raise AWSKMSContractError(
                "AWS KMS returned no wrapping key identity"
            )
        return EncryptedDataKey(
            ciphertext=bytes(ciphertext),
            key_id=key_id.strip(),
        )

    async def generate_encrypted_data_key(self) -> EncryptedDataKey:
        """Provision an AES-256 DEK without returning plaintext key material."""

        response = await self._call(
            "generate_data_key_without_plaintext",
            KeyId=self._key_id,
            KeySpec="AES_256",
            EncryptionContext=self._encryption_context,
        )
        return self._wrapped_key(response)

    async def generate_data_key(self) -> GeneratedDataKey:
        """Generate plaintext only for workflows that immediately require it."""

        response = await self._call(
            "generate_data_key",
            KeyId=self._key_id,
            KeySpec="AES_256",
            EncryptionContext=self._encryption_context,
        )
        plaintext = response.get("Plaintext")
        if not isinstance(plaintext, (bytes, bytearray)) or len(plaintext) != 32:
            raise AWSKMSContractError(
                "AWS KMS returned an invalid AES-256 plaintext data key"
            )
        wrapped = self._wrapped_key(response)
        return GeneratedDataKey(
            ciphertext=wrapped.ciphertext,
            key_id=wrapped.key_id,
            plaintext=bytearray(plaintext),
        )

    async def decrypt_dek(
        self,
        encrypted_dek: bytes,
        wrapping_key_id: str,
    ) -> bytes:
        """Decrypt using the exact original KMS key and tenant/domain context."""

        if not isinstance(encrypted_dek, bytes) or not encrypted_dek:
            raise ValueError("encrypted DEK is required")
        wrapping_key_id = wrapping_key_id.strip()
        if not wrapping_key_id:
            raise ValueError("wrapping KMS key id is required")

        response = await self._call(
            "decrypt",
            CiphertextBlob=encrypted_dek,
            KeyId=wrapping_key_id,
            EncryptionContext=self._encryption_context,
        )
        plaintext = response.get("Plaintext")
        if not isinstance(plaintext, (bytes, bytearray)) or len(plaintext) != 32:
            raise AWSKMSContractError(
                "AWS KMS returned an invalid decrypted AES-256 data key"
            )
        response_key_id = response.get("KeyId")
        if not isinstance(response_key_id, str) or response_key_id.strip() != wrapping_key_id:
            raise AWSKMSContractError(
                "AWS KMS decrypted the DEK under an unexpected key"
            )
        return bytes(plaintext)


def registration_kms_provider(
    tenant_id: str,
    *,
    client: Any | None = None,
) -> AWSKMSProvider:
    """Build the fixed-domain P3B provider and reject unsafe configuration."""

    from app.core.config import settings

    key_id = settings.AWS_KMS_KEY_ID.strip()
    if not key_id:
        raise RegistrationKMSConfigurationError(
            "AWS_KMS_KEY_ID is required for registration encryption"
        )

    endpoint_url = settings.AWS_KMS_ENDPOINT_URL.strip()
    if settings.is_production and endpoint_url:
        raise RegistrationKMSConfigurationError(
            "AWS_KMS_ENDPOINT_URL is forbidden in production"
        )

    return AWSKMSProvider(
        key_id=key_id,
        region_name=settings.AWS_REGION_NAME,
        tenant_id=str(tenant_id),
        data_domain=_REGISTRATION_DOMAIN,
        endpoint_url=endpoint_url or None,
        client=client,
    )
