from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

import app.services.registration_key_service as service
from app.repositories.registration_keys import RegistrationDEK


TENANT = uuid.UUID("11111111-1111-4111-8111-111111111111")
RECORD = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
WINNER = RegistrationDEK(
    key_version=9,
    encrypted_dek=b"winner-wrapped-dek",
    wrapping_key_id="arn:aws:kms:us-east-1:123456789012:key/winner",
)


class ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class FakeSession:
    async def execute(self, statement):
        assert "app.current_org_id" in str(statement)
        return ScalarResult(TENANT)


class FakeProvider:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def generate_encrypted_data_key(self):
        self.events.append("kms-generate-wrapped")
        return SimpleNamespace(
            ciphertext=b"loser-candidate-wrapped-dek",
            key_id="arn:aws:kms:us-east-1:123456789012:key/loser",
        )

    async def decrypt_dek(self, encrypted_dek: bytes, wrapping_key_id: str):
        self.events.append(f"kms-decrypt:{encrypted_dek.decode()}")
        assert encrypted_dek == WINNER.encrypted_dek
        assert wrapping_key_id == WINNER.wrapping_key_id
        return bytes(range(32))


def test_first_key_flow_authorizes_before_kms_and_decrypts_only_database_winner(monkeypatch) -> None:
    events: list[str] = []

    async def current(_session):
        events.append("db-current-authorize")
        return None

    async def install(_session, *, encrypted_dek: bytes, wrapping_key_id: str):
        events.append("db-install-serialize")
        assert encrypted_dek == b"loser-candidate-wrapped-dek"
        assert wrapping_key_id.endswith("/loser")
        return WINNER

    fake_provider = FakeProvider(events)
    monkeypatch.setattr(service, "current_registration_dek", current)
    monkeypatch.setattr(service, "install_registration_dek", install)
    monkeypatch.setattr(service, "registration_kms_provider", lambda _tenant: fake_provider)

    async def scenario():
        captured = None
        async with service.active_registration_data_key(FakeSession()) as key:
            nonlocal_captured[0] = key
            assert key.tenant_id == TENANT
            assert key.key_version == WINNER.key_version
            assert key.key == bytearray(range(32))
        return nonlocal_captured[0]

    nonlocal_captured = [None]
    captured = asyncio.run(scenario())

    assert events == [
        "db-current-authorize",
        "kms-generate-wrapped",
        "db-install-serialize",
        "kms-decrypt:winner-wrapped-dek",
    ]
    assert captured is not None
    assert captured.key == bytearray(32)


def test_existing_key_skips_generation_and_is_zeroized(monkeypatch) -> None:
    events: list[str] = []

    async def current(_session):
        events.append("db-current-authorize")
        return WINNER

    fake_provider = FakeProvider(events)
    monkeypatch.setattr(service, "current_registration_dek", current)
    monkeypatch.setattr(service, "registration_kms_provider", lambda _tenant: fake_provider)

    async def scenario():
        holder = [None]
        async with service.active_registration_data_key(FakeSession()) as key:
            holder[0] = key
            assert key.key == bytearray(range(32))
        return holder[0]

    captured = asyncio.run(scenario())
    assert events == [
        "db-current-authorize",
        "kms-decrypt:winner-wrapped-dek",
    ]
    assert captured.key == bytearray(32)


def test_missing_historical_key_never_calls_kms(monkeypatch) -> None:
    events: list[str] = []

    async def lookup(_session, *, key_version: int):
        events.append(f"db-lookup:{key_version}")
        return None

    monkeypatch.setattr(service, "lookup_registration_dek", lookup)
    monkeypatch.setattr(
        service,
        "registration_kms_provider",
        lambda _tenant: (_ for _ in ()).throw(AssertionError("KMS must not be called")),
    )

    async def scenario():
        with pytest.raises(service.RegistrationKeyNotFoundError):
            async with service.historical_registration_data_key(
                FakeSession(),
                key_version=404,
            ):
                pass

    asyncio.run(scenario())
    assert events == ["db-lookup:404"]


def test_encrypt_helper_uses_bound_tenant_record_and_active_key(monkeypatch) -> None:
    class FakeContext:
        async def __aenter__(self):
            return service.RegistrationPlaintextKey(
                tenant_id=TENANT,
                key_version=11,
                key=bytearray(range(32)),
            )

        async def __aexit__(self, exc_type, exc, tb):
            return False

    captured = {}

    def fake_encrypt(identifier, *, key, tenant_id, registration_id, key_version):
        captured.update(
            identifier=identifier,
            key=bytes(key),
            tenant_id=tenant_id,
            registration_id=registration_id,
            key_version=key_version,
        )
        return "ciphertext-envelope"

    monkeypatch.setattr(service, "active_registration_data_key", lambda _session: FakeContext())
    monkeypatch.setattr(service, "encrypt_registration_identifier", fake_encrypt)

    result = asyncio.run(
        service.encrypt_current_registration_identifier(
            FakeSession(),
            registration_id=RECORD,
            normalized_identifier="ABCDE1234F",
        )
    )

    assert result == "ciphertext-envelope"
    assert captured == {
        "identifier": "ABCDE1234F",
        "key": bytes(range(32)),
        "tenant_id": TENANT,
        "registration_id": RECORD,
        "key_version": 11,
    }
