from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


_PROFILE_READ_SQL = text(
    "SELECT * FROM app_secure.current_organization_profile()"
)
_PROFILE_UPDATE_SQL = text(
    "SELECT * FROM app_secure.update_current_organization_profile("
    "CAST(:patch AS jsonb)"
    ")"
)


async def get_current_organization_profile(
    session: AsyncSession,
) -> dict[str, Any] | None:
    """Return only the profile projection for ``app.current_org_id``.

    The database capability owns tenant binding and base-table access.  The API
    runtime intentionally has no direct SELECT privilege on ``organizations``.
    """

    result = await session.execute(_PROFILE_READ_SQL)
    row = result.mappings().one_or_none()
    return dict(row) if row is not None else None


async def update_current_organization_profile(
    session: AsyncSession,
    patch: dict[str, Any],
) -> dict[str, Any] | None:
    """Apply an allowlisted profile patch to the current tenant only.

    Omitted keys remain omitted in the JSON payload, while explicit ``None`` is
    encoded as JSON null.  This preserves PATCH semantics for nullable fields;
    the database capability validates the allowlist and integrity constraints.
    """

    result = await session.execute(
        _PROFILE_UPDATE_SQL,
        {"patch": json.dumps(patch, separators=(",", ":"))},
    )
    row = result.mappings().one_or_none()
    return dict(row) if row is not None else None
