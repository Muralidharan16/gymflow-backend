from __future__ import annotations

import struct
import uuid
from pathlib import Path

import pytest

from app.core.registration_crypto import (
    RegistrationCryptoError,
    decrypt_registration_identifier,
    encrypt_registration_identifier,
    envelope_key_version,
    zeroize_key,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "app/core/registration_crypto.py"
TENANT = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER_TENANT = uuid.UUID("22222222-2222-4222-8222-222222222222")
RECORD = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
OTHER_RECORD = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
KEY = bytes(range(32))


def test_registration_envelope_round_trip_and_header() -> None:
    envelope = encrypt_registration_identifier(
        "ABCDE1234F",
        key=KEY,
        tenant_id=TENANT,
        registration_id=RECORD,
        key_version=7,
    )

    assert envelope.key_version == 7
    assert envelope.payload[:4] == struct.pack(">I", 7)
    assert envelope_key_version(envelope.payload) == 7
    assert len(envelope.payload) >= 32
    assert decrypt_registration_identifier(
        envelope.payload,
        key=KEY,
        tenant_id=TENANT,
        registration_id=RECORD,
        expected_key_version=7,
    ) == "ABCDE1234F"


def test_aad_rejects_cross_tenant_and_cross_record_ciphertext_moves() -> None:
    envelope = encrypt_registration_identifier(
        "29ABCDE1234F1Z5",
        key=KEY,
        tenant_id=TENANT,
        registration_id=RECORD,
        key_version=3,
    )

    for tenant_id, registration_id in (
        (OTHER_TENANT, RECORD),
        (TENANT, OTHER_RECORD),
        (OTHER_TENANT, OTHER_RECORD),
    ):
        with pytest.raises(RegistrationCryptoError, match="authentication failed"):
            decrypt_registration_identifier(
                envelope.payload,
                key=KEY,
                tenant_id=tenant_id,
                registration_id=registration_id,
                expected_key_version=3,
            )


def test_header_tampering_and_expected_version_mismatch_fail_closed() -> None:
    envelope = encrypt_registration_identifier(
        "ABCDE1234F",
        key=KEY,
        tenant_id=TENANT,
        registration_id=RECORD,
        key_version=5,
    )

    tampered = struct.pack(">I", 6) + envelope.payload[4:]
    with pytest.raises(RegistrationCryptoError, match="authentication failed"):
        decrypt_registration_identifier(
            tampered,
            key=KEY,
            tenant_id=TENANT,
            registration_id=RECORD,
        )

    with pytest.raises(RegistrationCryptoError, match="key version mismatch"):
        decrypt_registration_identifier(
            envelope.payload,
            key=KEY,
            tenant_id=TENANT,
            registration_id=RECORD,
            expected_key_version=6,
        )


def test_key_and_identifier_inputs_are_bounded() -> None:
    with pytest.raises(RegistrationCryptoError, match="AES-256"):
        encrypt_registration_identifier(
            "ABCDE1234F",
            key=b"short",
            tenant_id=TENANT,
            registration_id=RECORD,
            key_version=1,
        )

    for identifier in ("", "x" * 4097):
        with pytest.raises(RegistrationCryptoError, match="length is invalid"):
            encrypt_registration_identifier(
                identifier,
                key=KEY,
                tenant_id=TENANT,
                registration_id=RECORD,
                key_version=1,
            )

    for version in (0, -1, 0x80000000, True):
        with pytest.raises(RegistrationCryptoError, match="key version is invalid"):
            encrypt_registration_identifier(
                "ABCDE1234F",
                key=KEY,
                tenant_id=TENANT,
                registration_id=RECORD,
                key_version=version,
            )


def test_key_buffer_zeroization_is_explicit() -> None:
    key = bytearray(range(32))
    zeroize_key(key)
    assert key == bytearray(32)


def test_registration_codec_is_independent_of_legacy_secret_key_and_fernet() -> None:
    source = SOURCE.read_text(encoding="utf-8")

    assert "AESGCM" in source
    assert 'b"organization_registrations"' in source
    assert "doers:p3b:registration-envelope:v1" in source
    assert "tenant_id.bytes" in source
    assert "registration_id.bytes" in source
    assert "struct.pack(\">I\"" in source
    for forbidden in ("SECRET_KEY", "Fernet", "urlsafe_b64encode"):
        assert forbidden not in source
