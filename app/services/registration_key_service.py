from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.aws_kms import registration_kms_provider
from app.core.registration_crypto import (
    RegistrationEnvelope,
    encrypt_registration_identifier,
    zeroize_key,
)
from app.repositories.registration_keys import (
    RegistrationDEK,
    RegistrationKeyAuthorizationError,
    current_registration_dek,
    install_registration_dek,
    lookup_registration_dek,
)


_BOUND_TENANT_SQL = text(
    "SELECT NULLIF(pg_catalog.current_setting('app.current_org_id', true), '')::uuid"
)


class RegistrationKeyNotFoundError(LookupError):
    """A referenced registration DEK version is unavailable for this tenant."""


@dataclass(slots=True)
class RegistrationPlaintextKey:
    tenant_id: uuid.UUID
    key_version: int
    key: bytearray

    def zeroize(self) -> None:
        zeroize_key(self.key)


async def _bound_tenant_id(session: AsyncSession) -> uuid.UUID:
    result = await session.execute(_BOUND_TENANT_SQL)
    tenant_id = result.scalar_one_or_none()
    if tenant_id is None:
        raise RegistrationKeyAuthorizationError(
            "organization registration key tenant context is missing"
        )
    return uuid.UUID(str(tenant_id))


async def _decrypt_registration_dek(
    *,
    tenant_id: uuid.UUID,
    dek: RegistrationDEK,
) -> bytearray:
    provider = registration_kms_provider(str(tenant_id))
    plaintext = await provider.decrypt_dek(
        dek.encrypted_dek,
        dek.wrapping_key_id,
    )
    return bytearray(plaintext)


@asynccontextmanager
async def active_registration_data_key(
    session: AsyncSession,
) -> AsyncIterator[RegistrationPlaintextKey]:
    """Yield the ACTIVE registration DEK and wipe our mutable copy on exit.

    The database capability is called before KMS so an invalid principal cannot
    cause external key operations. If no key exists, KMS creates only a wrapped
    candidate. The database serializes installation; only the returned winner is
    decrypted, so concurrent losing candidates never expose plaintext DEKs.
    """

    current = await current_registration_dek(session)
    tenant_id = await _bound_tenant_id(session)
    if current is None:
        provider = registration_kms_provider(str(tenant_id))
        candidate = await provider.generate_encrypted_data_key()
        current = await install_registration_dek(
            session,
            encrypted_dek=candidate.ciphertext,
            wrapping_key_id=candidate.key_id,
        )

    plaintext = await _decrypt_registration_dek(
        tenant_id=tenant_id,
        dek=current,
    )
    key = RegistrationPlaintextKey(
        tenant_id=tenant_id,
        key_version=current.key_version,
        key=plaintext,
    )
    try:
        yield key
    finally:
        key.zeroize()


@asynccontextmanager
async def historical_registration_data_key(
    session: AsyncSession,
    *,
    key_version: int,
) -> AsyncIterator[RegistrationPlaintextKey]:
    """Yield one tenant/domain-bound historical DEK and wipe it on exit."""

    dek = await lookup_registration_dek(
        session,
        key_version=key_version,
    )
    if dek is None:
        raise RegistrationKeyNotFoundError(
            f"registration DEK version {key_version} is unavailable"
        )
    tenant_id = await _bound_tenant_id(session)
    plaintext = await _decrypt_registration_dek(
        tenant_id=tenant_id,
        dek=dek,
    )
    key = RegistrationPlaintextKey(
        tenant_id=tenant_id,
        key_version=dek.key_version,
        key=plaintext,
    )
    try:
        yield key
    finally:
        key.zeroize()


async def encrypt_current_registration_identifier(
    session: AsyncSession,
    *,
    registration_id: uuid.UUID,
    normalized_identifier: str,
) -> RegistrationEnvelope:
    """Encrypt one normalized identifier using the bound tenant's ACTIVE DEK."""

    async with active_registration_data_key(session) as data_key:
        return encrypt_registration_identifier(
            normalized_identifier,
            key=data_key.key,
            tenant_id=data_key.tenant_id,
            registration_id=registration_id,
            key_version=data_key.key_version,
        )
