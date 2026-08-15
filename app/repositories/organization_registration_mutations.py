from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession


_CREATE_SQL = text(
    """
    SELECT *
    FROM app_secure.create_organization_registration_envelope(
        :registration_id,
        :id_type,
        :id_number_masked,
        :country_code,
        :entity_type,
        :payload_encrypted,
        :key_version
    )
    """
)
_REPLACE_SQL = text(
    """
    SELECT *
    FROM app_secure.replace_organization_registration_envelope(
        :registration_id,
        :id_type,
        :id_number_masked,
        :country_code,
        :entity_type,
        :payload_encrypted,
        :key_version
    )
    """
)


@dataclass(frozen=True, slots=True)
class CreatedOrganizationRegistration:
    id: uuid.UUID
    id_type: str
    id_number_masked: str
    country_code: str
    entity_type: str | None
    is_verified: bool
    verified_at: datetime | None


class RegistrationMutationAuthorizationError(PermissionError):
    pass


class RegistrationMutationValidationError(ValueError):
    pass


class RegistrationKeyStateError(RuntimeError):
    pass


class RegistrationConflictError(RuntimeError):
    pass


class RegistrationTargetNotFoundError(LookupError):
    pass


def _sqlstate(exc: DBAPIError) -> str | None:
    orig = getattr(exc, "orig", None)
    cause = getattr(orig, "__cause__", None)
    return (
        getattr(orig, "sqlstate", None)
        or getattr(orig, "pgcode", None)
        or getattr(cause, "sqlstate", None)
        or getattr(cause, "pgcode", None)
    )


def _registration_from_row(row: Any) -> CreatedOrganizationRegistration:
    mapping = row._mapping
    return CreatedOrganizationRegistration(
        id=uuid.UUID(str(mapping["id"])),
        id_type=str(mapping["id_type"]),
        id_number_masked=str(mapping["id_number_masked"]),
        country_code=str(mapping["country_code"]),
        entity_type=(
            None if mapping["entity_type"] is None else str(mapping["entity_type"])
        ),
        is_verified=bool(mapping["is_verified"]),
        verified_at=mapping["verified_at"],
    )


async def _execute_mutation(
    session: AsyncSession,
    statement,
    params: dict[str, object],
    *,
    action: str,
) -> CreatedOrganizationRegistration:
    try:
        result = await session.execute(statement, params)
        return _registration_from_row(result.one())
    except DBAPIError as exc:
        state = _sqlstate(exc)
        if state == "42501":
            raise RegistrationMutationAuthorizationError(
                f"organization registration {action} is not authorized"
            ) from exc
        if state == "22023":
            raise RegistrationMutationValidationError(
                "organization registration input is invalid"
            ) from exc
        if state == "23503":
            raise RegistrationKeyStateError(
                "organization registration encryption key is unavailable"
            ) from exc
        if state == "P0002":
            raise RegistrationTargetNotFoundError(
                "organization registration replacement target does not exist"
            ) from exc
        if state == "23505":
            raise RegistrationConflictError(
                "organization registration already exists"
            ) from exc
        raise


async def create_organization_registration_envelope(
    session: AsyncSession,
    *,
    registration_id: uuid.UUID,
    id_type: str,
    id_number_masked: str,
    country_code: str,
    entity_type: str | None,
    payload_encrypted: bytes,
    key_version: int,
) -> CreatedOrganizationRegistration:
    """Create one crypto-v1 registration through the bounded DB capability."""

    return await _execute_mutation(
        session,
        _CREATE_SQL,
        {
            "registration_id": registration_id,
            "id_type": id_type,
            "id_number_masked": id_number_masked,
            "country_code": country_code,
            "entity_type": entity_type,
            "payload_encrypted": payload_encrypted,
            "key_version": key_version,
        },
        action="creation",
    )


async def replace_organization_registration_envelope(
    session: AsyncSession,
    *,
    registration_id: uuid.UUID,
    id_type: str,
    id_number_masked: str,
    country_code: str,
    entity_type: str | None,
    payload_encrypted: bytes,
    key_version: int,
) -> CreatedOrganizationRegistration:
    """Replace one existing registration through the bounded DB capability."""

    return await _execute_mutation(
        session,
        _REPLACE_SQL,
        {
            "registration_id": registration_id,
            "id_type": id_type,
            "id_number_masked": id_number_masked,
            "country_code": country_code,
            "entity_type": entity_type,
            "payload_encrypted": payload_encrypted,
            "key_version": key_version,
        },
        action="replacement",
    )
