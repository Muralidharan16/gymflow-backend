from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession


_CURRENT_SQL = text("SELECT * FROM app_secure.current_registration_dek()")
_INSTALL_SQL = text(
    "SELECT * FROM app_secure.install_registration_dek(:encrypted_dek, :wrapping_key_id)"
)
_LOOKUP_SQL = text(
    "SELECT * FROM app_secure.lookup_registration_dek(:key_version)"
)


@dataclass(frozen=True, slots=True)
class RegistrationDEK:
    key_version: int
    encrypted_dek: bytes
    wrapping_key_id: str


class RegistrationKeyAuthorizationError(PermissionError):
    """The database rejected the current registration-key principal context."""


def _sqlstate(exc: DBAPIError) -> str | None:
    orig = getattr(exc, "orig", None)
    cause = getattr(orig, "__cause__", None)
    return (
        getattr(orig, "sqlstate", None)
        or getattr(orig, "pgcode", None)
        or getattr(cause, "sqlstate", None)
        or getattr(cause, "pgcode", None)
    )


async def _execute(session: AsyncSession, statement, params: dict | None = None):
    try:
        return await session.execute(statement, params or {})
    except DBAPIError as exc:
        if _sqlstate(exc) == "42501":
            raise RegistrationKeyAuthorizationError(
                "organization registration key authorization denied"
            ) from exc
        raise


def _row_to_dek(row) -> RegistrationDEK:
    mapping = row._mapping
    return RegistrationDEK(
        key_version=int(mapping["key_version"]),
        encrypted_dek=bytes(mapping["encrypted_dek"]),
        wrapping_key_id=str(mapping["wrapping_key_id"]),
    )


async def current_registration_dek(
    session: AsyncSession,
) -> RegistrationDEK | None:
    """Return the current tenant/domain DEK metadata through app_secure only."""

    result = await _execute(session, _CURRENT_SQL)
    row = result.first()
    return _row_to_dek(row) if row is not None else None


async def install_registration_dek(
    session: AsyncSession,
    *,
    encrypted_dek: bytes,
    wrapping_key_id: str,
) -> RegistrationDEK:
    """Install-or-return the database-serialized ACTIVE registration DEK."""

    result = await _execute(
        session,
        _INSTALL_SQL,
        {
            "encrypted_dek": encrypted_dek,
            "wrapping_key_id": wrapping_key_id,
        },
    )
    row = result.one()
    return _row_to_dek(row)


async def lookup_registration_dek(
    session: AsyncSession,
    *,
    key_version: int,
) -> RegistrationDEK | None:
    """Return one historical registration DEK for the already-bound tenant."""

    result = await _execute(
        session,
        _LOOKUP_SQL,
        {"key_version": key_version},
    )
    row = result.first()
    if row is None:
        return None
    mapping = row._mapping
    return RegistrationDEK(
        key_version=key_version,
        encrypted_dek=bytes(mapping["encrypted_dek"]),
        wrapping_key_id=str(mapping["wrapping_key_id"]),
    )
