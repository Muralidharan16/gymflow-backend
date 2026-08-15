from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession


_REGISTRATION_LIST_SQL = text(
    "SELECT * FROM app_secure.current_organization_registrations()"
)
_REGISTRATION_EXISTS_SQL = text(
    "SELECT app_secure.current_organization_has_registration()"
)


class RegistrationAuthorizationError(PermissionError):
    """The database rejected the current registration tenant/principal context."""


def _sqlstate(exc: DBAPIError) -> str | None:
    orig = getattr(exc, "orig", None)
    cause = getattr(orig, "__cause__", None)
    return (
        getattr(orig, "sqlstate", None)
        or getattr(orig, "pgcode", None)
        or getattr(cause, "sqlstate", None)
        or getattr(cause, "pgcode", None)
    )


async def _execute_registration_capability(
    session: AsyncSession,
    statement,
):
    try:
        return await session.execute(statement)
    except DBAPIError as exc:
        if _sqlstate(exc) == "42501":
            raise RegistrationAuthorizationError(
                "organization registration authorization denied"
            ) from exc
        raise


async def list_current_organization_registrations(
    session: AsyncSession,
) -> list[dict[str, Any]]:
    """Return masked registration metadata for the bound organization.

    Tenant and principal authorization are enforced by the database capability;
    this repository intentionally never selects encrypted registration payloads.
    """

    result = await _execute_registration_capability(session, _REGISTRATION_LIST_SQL)
    return [dict(row) for row in result.mappings().all()]


async def current_organization_has_registration(
    session: AsyncSession,
) -> bool:
    """Return only whether the bound organization has any registration."""

    result = await _execute_registration_capability(session, _REGISTRATION_EXISTS_SQL)
    return bool(result.scalar_one())
