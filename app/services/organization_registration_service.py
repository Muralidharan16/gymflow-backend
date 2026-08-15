from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.organization_registration_mutations import (
    CreatedOrganizationRegistration,
    create_organization_registration_envelope,
)
from app.services.registration_key_service import (
    encrypt_current_registration_identifier,
)


async def create_secure_organization_registration(
    session: AsyncSession,
    *,
    id_type: str,
    normalized_identifier: str,
    masked_identifier: str,
    country_code: str,
    entity_type: str | None,
    registration_id: uuid.UUID | None = None,
) -> CreatedOrganizationRegistration:
    """Encrypt and persist one registration without exposing table DML.

    Inputs are expected to have already passed the API's domain-specific
    validation. This service deliberately does not invent PAN/GST/VAT/EIN
    normalization rules. It only couples the pre-generated record id to the
    domain-bound ciphertext AAD and the atomic database create capability.
    """

    resolved_id = registration_id or uuid.uuid4()
    envelope = await encrypt_current_registration_identifier(
        session,
        registration_id=resolved_id,
        normalized_identifier=normalized_identifier,
    )
    return await create_organization_registration_envelope(
        session,
        registration_id=resolved_id,
        id_type=id_type,
        id_number_masked=masked_identifier,
        country_code=country_code,
        entity_type=entity_type,
        payload_encrypted=envelope.payload,
        key_version=envelope.key_version,
    )
