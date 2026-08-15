"""P3C organization profile + registration atomic composition.

This service owns one SQLAlchemy transaction and composes only the already
certified P3A profile and P3B registration capabilities.  It does not accept a
tenant id, principal id, runtime role or raw table handle; authorization remains
bound to the verified request context installed on the session.
"""

from __future__ import annotations

from dataclasses import dataclass
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.organization_profile import (
    get_current_organization_profile,
    update_current_organization_profile,
)
from app.repositories.organization_registrations import (
    list_current_organization_registrations,
)
from app.services.organization_registration_service import (
    create_secure_organization_registration,
    replace_secure_organization_registration,
)


class OrganizationProfileNotFoundError(RuntimeError):
    """The request principal has no organization profile visible to mutate."""


class OrganizationProfileTransactionStateError(RuntimeError):
    """The atomic service was entered with an already-open transaction."""


class MaskedRegistrationIdentifierError(ValueError):
    """A client attempted to submit the exact masked identifier returned by the API."""


@dataclass(frozen=True, slots=True)
class RegistrationMutationPlan:
    id_type: str
    normalized_identifier: str
    masked_identifier: str
    country_code: str
    entity_type: str | None


@dataclass(frozen=True, slots=True)
class OrganizationProfileMutationResult:
    profile: dict
    registrations: list[dict]


async def mutate_organization_profile_atomically(
    session: AsyncSession,
    *,
    profile_patch: dict,
    registration_updates: tuple[RegistrationMutationPlan, ...],
) -> OrganizationProfileMutationResult:
    """Commit the P3A profile and P3B registration mutation as one unit.

    Registration work is intentionally composed before the P3A profile UPDATE.
    The P3B path may call external KMS, so this ordering avoids holding the
    organization profile/root row lock while waiting on network I/O.  P3A still
    executes inside the same transaction; if its authorization or mutation fails,
    all registration/key/payload database work rolls back.

    The caller must provide a service-managed request session with no active
    transaction. Any authorization, validation, KMS, key-state, uniqueness,
    concurrency, cancellation, final-read or commit failure exits the transaction
    context exceptionally and therefore rolls back every database mutation made
    by this operation.
    """

    if session.in_transaction():
        raise OrganizationProfileTransactionStateError(
            "P3C atomic mutation requires a clean service-managed session"
        )

    async with session.begin():
        if registration_updates:
            registrations = await list_current_organization_registrations(session)
            by_business_key = {
                (
                    str(registration["id_type"]).strip().upper(),
                    str(registration["country_code"]).strip().upper(),
                ): registration
                for registration in registrations
            }

            for requested in registration_updates:
                business_key = (
                    requested.id_type.strip().upper(),
                    requested.country_code.strip().upper(),
                )
                existing = by_business_key.get(business_key)

                # Compare to the exact value the bounded read capability returned.
                # Do not guess mask syntax: legitimate identifiers beginning with
                # X remain valid unless they literally equal this tenant's stored
                # masked representation.
                if existing is not None and requested.normalized_identifier == str(
                    existing["id_number_masked"]
                ):
                    raise MaskedRegistrationIdentifierError(
                        "masked registration identifiers are read-only representations"
                    )

                if existing is None:
                    registration = await create_secure_organization_registration(
                        session,
                        id_type=requested.id_type,
                        normalized_identifier=requested.normalized_identifier,
                        masked_identifier=requested.masked_identifier,
                        country_code=requested.country_code,
                        entity_type=requested.entity_type,
                    )
                else:
                    registration = await replace_secure_organization_registration(
                        session,
                        registration_id=uuid.UUID(str(existing["id"])),
                        id_type=requested.id_type,
                        normalized_identifier=requested.normalized_identifier,
                        masked_identifier=requested.masked_identifier,
                        country_code=requested.country_code,
                        entity_type=requested.entity_type,
                    )

                by_business_key[business_key] = {
                    "id": registration.id,
                    "id_type": registration.id_type,
                    "id_number_masked": registration.id_number_masked,
                    "country_code": registration.country_code,
                }

        if profile_patch:
            profile = await update_current_organization_profile(session, profile_patch)
        else:
            profile = await get_current_organization_profile(session)

        if profile is None:
            raise OrganizationProfileNotFoundError

        final_profile = await get_current_organization_profile(session)
        if final_profile is None:
            raise OrganizationProfileNotFoundError
        final_registrations = await list_current_organization_registrations(session)

        result = OrganizationProfileMutationResult(
            profile=final_profile,
            registrations=final_registrations,
        )

    return result
