from __future__ import annotations

import asyncio
import ast
from pathlib import Path

import pytest

from app.core.aws_kms import AWSKMSProvider


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "app/core/aws_kms.py"
TENANT_ID = "11111111-1111-4111-8111-111111111111"
DOMAIN = "organization_registrations"
CONTEXT = {
    "doers:tenant_id": TENANT_ID,
    "doers:data_domain": DOMAIN,
    "doers:purpose": "doers-envelope-dek-v1",
}


class FakeKMSClient:
    def __init__(self) -> None:
        self.generate_calls: list[dict] = []
        self.decrypt_calls: list[dict] = []

    def generate_data_key(self, **kwargs):
        self.generate_calls.append(kwargs)
        return {
            "Plaintext": bytes(range(32)),
            "CiphertextBlob": b"kms-wrapped-data-key",
            "KeyId": "arn:aws:kms:us-east-1:123456789012:key/example",
        }

    def decrypt(self, **kwargs):
        self.decrypt_calls.append(kwargs)
        return {"Plaintext": bytes(reversed(range(32)))}


class InvalidGenerateClient(FakeKMSClient):
    def generate_data_key(self, **kwargs):
        self.generate_calls.append(kwargs)
        return {"Plaintext": b"short", "CiphertextBlob": b"wrapped"}


class InvalidDecryptClient(FakeKMSClient):
    def decrypt(self, **kwargs):
        self.decrypt_calls.append(kwargs)
        return {"Plaintext": b"short"}


def _provider(client) -> AWSKMSProvider:
    return AWSKMSProvider(
        key_id="alias/doers-production",
        region_name="us-east-1",
        tenant_id=TENANT_ID,
        data_domain=DOMAIN,
        client=client,
    )


def test_provider_uses_generate_data_key_with_exact_tenant_domain_context() -> None:
    client = FakeKMSClient()
    provider = _provider(client)

    generated = asyncio.run(provider.generate_data_key())

    assert client.generate_calls == [
        {
            "KeyId": "alias/doers-production",
            "KeySpec": "AES_256",
            "EncryptionContext": CONTEXT,
        }
    ]
    assert bytes(generated.plaintext) == bytes(range(32))
    assert generated.ciphertext == b"kms-wrapped-data-key"
    assert provider.encryption_context == CONTEXT

    generated.zeroize()
    assert generated.plaintext == bytearray(32)


def test_decrypt_requires_same_context_and_does_not_pin_current_key_alias() -> None:
    client = FakeKMSClient()
    provider = _provider(client)

    plaintext = asyncio.run(provider.decrypt_dek(b"historical-wrapped-key"))

    assert plaintext == bytes(reversed(range(32)))
    assert client.decrypt_calls == [
        {
            "CiphertextBlob": b"historical-wrapped-key",
            "EncryptionContext": CONTEXT,
        }
    ]
    assert "KeyId" not in client.decrypt_calls[0]


def test_provider_fails_closed_on_invalid_kms_key_material() -> None:
    with pytest.raises(RuntimeError, match="invalid AES-256 plaintext data key"):
        asyncio.run(_provider(InvalidGenerateClient()).generate_data_key())

    with pytest.raises(RuntimeError, match="invalid decrypted AES-256 data key"):
        asyncio.run(_provider(InvalidDecryptClient()).decrypt_dek(b"wrapped"))

    with pytest.raises(ValueError, match="encrypted DEK is required"):
        asyncio.run(_provider(FakeKMSClient()).decrypt_dek(b""))


def test_provider_rejects_missing_security_context_inputs() -> None:
    values = {
        "key_id": "alias/doers-production",
        "region_name": "us-east-1",
        "tenant_id": TENANT_ID,
        "data_domain": DOMAIN,
    }
    for field_name in values:
        kwargs = dict(values)
        kwargs[field_name] = "   "
        with pytest.raises(ValueError):
            AWSKMSProvider(**kwargs, client=FakeKMSClient())


def test_production_adapter_never_derives_wrapping_keys_from_application_secret() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(SOURCE))

    assert "generate_data_key" in source
    assert "EncryptionContext" in source
    assert "AES_256" in source
    assert "asyncio.to_thread" in source
    assert "kms_bulkhead" in source
    assert "_guardrails" in source

    for forbidden in (
        "SECRET_KEY",
        "raw_master_key",
        "Fernet",
        "base64.urlsafe_b64encode",
    ):
        assert forbidden not in source

    imports_boto3 = any(
        isinstance(node, ast.Import)
        and any(alias.name == "boto3" for alias in node.names)
        for node in tree.body
    )
    assert imports_boto3
