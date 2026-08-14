from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession


_PROFILE_READ_SQL = text(
    "SELECT * FROM app_secure.current_organization_profile()"
)
_PROFILE_UPDATE_SQL = text(
    "SELECT * FROM app_secure.update_current_organization_profile("
    "CAST(:patch AS jsonb)"
    ")"
)


class ProfileAuthorizationError(PermissionError):
    """The database rejected the current principal/tenant profile context."""


def _sqlstate(exc: DBAPIError) -> str | None:
    """Extract PostgreSQL SQLSTATE across sync/async SQLAlchemy adapters."""

    orig = getattr(exc, "orig", None)
    cause = getattr(orig, "__cause__", None)
    return (
        getattr(orig, "sqlstate", None)
        or getattr(orig, "pgcode", None)
        or getattr(cause, "sqlstate", None)
        or getattr(cause, "pgcode", None)
    )


async def _execute_profile_capability(
    session: AsyncSession,
    statement,
    parameters: dict[str, Any] | None = None,
):
    try:
        return await session.execute(statement, parameters or {})
    except DBAPIError as exc:
        if _sqlstate(exc) == "42501":
            raise ProfileAuthorizationError(
                "organization profile authorization denied"
            ) from exc
        raise


async def get_current_organization_profile(
    session: AsyncSession,
) -> dict[str, Any] | None:
    """Return only the profile projection for ``app.current_org_id``.

    The database capability owns tenant binding and base-table access. The API
    runtime intentionally has no direct SELECT privilege on ``organizations``.
    """

    result = await _execute_profile_capability(session, _PROFILE_READ_SQL)
    row = result.mappings().one_or_none()
    return dict(row) if row is not None else None


async def update_current_organization_profile(
    session: AsyncSession,
    patch: dict[str, Any],
) -> dict[str, Any] | None:
    """Apply an allowlisted profile patch to the current tenant only.

    Omitted keys remain omitted in the JSON payload, while explicit ``None`` is
    encoded as JSON null. This preserves PATCH semantics for nullable fields;
    the database capability validates the allowlist and integrity constraints.
    """

    result = await _execute_profile_capability(
        session,
        _PROFILE_UPDATE_SQL,
        {"patch": json.dumps(patch, separators=(",", ":"))},
    )
    row = result.mappings().one_or_none()
    return dict(row) if row is not None else None
