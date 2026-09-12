"""P3B registration ciphertext codec.

The format is intentionally registration-specific so existing envelope
ciphertexts keep their legacy AAD semantics. Payload bytes are:

    4-byte big-endian key version || 12-byte nonce || AES-256-GCM ciphertext

AAD binds schema version, tenant, data domain, registration record, and DEK
version. Moving ciphertext between any of those contexts therefore fails GCM
authentication.
"""

from __future__ import annotations

import os
import struct
import uuid
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


_DOMAIN = b"organization_registrations"
_AAD_PREFIX = b"doers:p3b:registration-envelope:v1"
_NONCE_BYTES = 12
_MIN_ENVELOPE_BYTES = 4 + _NONCE_BYTES + 16
_MAX_IDENTIFIER_BYTES = 4096


class RegistrationCryptoError(ValueError):
    """Registration ciphertext is malformed or fails authenticated decryption."""


@dataclass(frozen=True, slots=True)
class RegistrationEnvelope:
    key_version: int
    payload: bytes


def zeroize_key(key: bytearray) -> None:
    for index in range(len(key)):
        key[index] = 0


def _validate_key(key: bytes | bytearray) -> bytes:
    key_bytes = bytes(key)
    if len(key_bytes) != 32:
        raise RegistrationCryptoError("registration data key must be AES-256")
    return key_bytes


def _validate_key_version(key_version: int) -> int:
    if not isinstance(key_version, int) or isinstance(key_version, bool):
        raise RegistrationCryptoError("registration key version is invalid")
    if key_version < 1 or key_version > 0x7FFFFFFF:
        raise RegistrationCryptoError("registration key version is invalid")
    return key_version


def _aad(
    *,
    tenant_id: uuid.UUID,
    registration_id: uuid.UUID,
    key_version: int,
) -> bytes:
    return b"\x00".join(
        (
            _AAD_PREFIX,
            tenant_id.bytes,
            _DOMAIN,
            registration_id.bytes,
            struct.pack(">I", _validate_key_version(key_version)),
        )
    )


def encrypt_registration_identifier(
    identifier: str,
    *,
    key: bytes | bytearray,
    tenant_id: uuid.UUID,
    registration_id: uuid.UUID,
    key_version: int,
) -> RegistrationEnvelope:
    if not isinstance(identifier, str):
        raise RegistrationCryptoError("registration identifier must be text")
    plaintext = identifier.encode("utf-8")
    if not plaintext or len(plaintext) > _MAX_IDENTIFIER_BYTES:
        raise RegistrationCryptoError("registration identifier length is invalid")

    version = _validate_key_version(key_version)
    key_bytes = _validate_key(key)
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = AESGCM(key_bytes).encrypt(
        nonce,
        plaintext,
        _aad(
            tenant_id=tenant_id,
            registration_id=registration_id,
            key_version=version,
        ),
    )
    return RegistrationEnvelope(
        key_version=version,
        payload=struct.pack(">I", version) + nonce + ciphertext,
    )


def envelope_key_version(payload: bytes) -> int:
    if not isinstance(payload, bytes) or len(payload) < _MIN_ENVELOPE_BYTES:
        raise RegistrationCryptoError("registration ciphertext envelope is invalid")
    version = struct.unpack(">I", payload[:4])[0]
    return _validate_key_version(version)


def decrypt_registration_identifier(
    payload: bytes,
    *,
    key: bytes | bytearray,
    tenant_id: uuid.UUID,
    registration_id: uuid.UUID,
    expected_key_version: int | None = None,
) -> str:
    version = envelope_key_version(payload)
    if expected_key_version is not None:
        expected = _validate_key_version(expected_key_version)
        if version != expected:
            raise RegistrationCryptoError("registration ciphertext key version mismatch")

    key_bytes = _validate_key(key)
    nonce = payload[4 : 4 + _NONCE_BYTES]
    ciphertext = payload[4 + _NONCE_BYTES :]
    try:
        plaintext = AESGCM(key_bytes).decrypt(
            nonce,
            ciphertext,
            _aad(
                tenant_id=tenant_id,
                registration_id=registration_id,
                key_version=version,
            ),
        )
    except InvalidTag as exc:
        raise RegistrationCryptoError(
            "registration ciphertext authentication failed"
        ) from exc

    try:
        identifier = plaintext.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RegistrationCryptoError(
            "registration ciphertext plaintext is not valid UTF-8"
        ) from exc
    if not identifier:
        raise RegistrationCryptoError("registration ciphertext plaintext is empty")
    return identifier
