from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


_COMPLETE_ONBOARDING_PROFILE_SQL = text(
    "SELECT app_secure.complete_current_organization_onboarding_profile("
    "CAST(:patch AS jsonb)"
    ")"
)


async def complete_current_organization_onboarding_profile(
    session: AsyncSession,
    patch: dict[str, Any],
) -> None:
    """Apply onboarding profile data to the verified current owner/tenant.

    The auth deployment identity intentionally has no direct UPDATE privilege on
    ``organizations``.  PostgreSQL validates the current owner/organization
    request context before the bounded SECURITY DEFINER command may mutate the
    tenant root.
    """

    completed = await session.scalar(
        _COMPLETE_ONBOARDING_PROFILE_SQL,
        {"patch": json.dumps(patch, separators=(",", ":"))},
    )
    if completed is not True:
        raise RuntimeError("organization onboarding profile capability did not complete")
