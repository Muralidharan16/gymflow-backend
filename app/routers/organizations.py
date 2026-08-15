from typing import List
from fastapi import APIRouter, Depends, HTTPException, status, Request
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.aws_kms import (
    AWSKMSContractError,
    AWSKMSUnavailableError,
    RegistrationKMSConfigurationError,
)
from app.core.database import get_db
from app.core.service_managed_database import get_service_managed_db
from app.core.deps import require_org_admin, Staff
from app.repositories.organization_profile import (
    ProfileAuthorizationError,
    get_current_organization_profile,
)
from app.repositories.organization_registration_mutations import (
    CreatedOrganizationRegistration,
    RegistrationConflictError,
    RegistrationKeyStateError,
    RegistrationMutationAuthorizationError,
    RegistrationMutationValidationError,
    RegistrationTargetNotFoundError,
)
from app.repositories.organization_registrations import (
    RegistrationAuthorizationError,
    list_current_organization_registrations,
)
from app.repositories.registration_keys import RegistrationKeyAuthorizationError
from app.schemas.organization import (
    RegistrationCreate, RegistrationResponse, OrganizationProfileResponse, OrganizationUpdate,
    LogoUploadUrlResponse, LogoConfirmRequest, LogoStatusResponse
)
from app.schemas.common import Response
from app.services.organization_profile_mutation_service import (
    OrganizationProfileNotFoundError,
    OrganizationProfileTransactionStateError,
    RegistrationMutationPlan,
    mutate_organization_profile_atomically,
)
from app.services.organization_registration_service import (
    create_secure_organization_registration,
)
from app.utils.encryption import mask_id_number
from app.utils.s3 import get_s3_client
from app.core.config import settings
from app.tasks.logos import process_org_logo
from app.core.deps import get_current_active_staff

router = APIRouter(prefix="/organizations", tags=["Organizations"])


def _profile_response(
    org: dict,
    registrations: list[dict],
) -> OrganizationProfileResponse:
    """Build the profile without decrypting registration identifiers.

    P3B treats registration identifiers as write-only secrets for the normal
    profile surface. The masked registration collection is the read contract;
    legacy plaintext convenience fields remain present as nullable compatibility
    fields but are deliberately never populated from encrypted storage.
    """

    return OrganizationProfileResponse(
        id=org["id"],
        name=org["name"],
        business_type=org["business_type"],
        tagline=org["tagline"],
        description=org["description"],
        year_established=org["year_established"],
        website_url=org["website_url"],
        social_links=org["social_links"],
        registrations=[
            RegistrationResponse.model_validate(registration)
            for registration in registrations
        ],
        business_id=None,
        gst_number=None,
        pan_number=None,
        logo_status=org["logo_status"],
        logo_thumb_url=(
            f"{settings.CDN_BASE_URL}/{org['logo_thumb_key']}"
            if org["logo_thumb_key"]
            else None
        ),
        logo_medium_url=(
            f"{settings.CDN_BASE_URL}/{org['logo_medium_key']}"
            if org["logo_medium_key"]
            else None
        ),
        logo_full_url=(
            f"{settings.CDN_BASE_URL}/{org['logo_full_key']}"
            if org["logo_full_key"]
            else None
        ),
        cover_status=org["cover_status"],
        cover_mobile_url=(
            f"{settings.CDN_BASE_URL}/{org['cover_mobile_key']}"
            if org["cover_mobile_key"]
            else None
        ),
        cover_tablet_url=(
            f"{settings.CDN_BASE_URL}/{org['cover_tablet_key']}"
            if org["cover_tablet_key"]
            else None
        ),
        cover_desktop_url=(
            f"{settings.CDN_BASE_URL}/{org['cover_desktop_key']}"
            if org["cover_desktop_key"]
            else None
        ),
    )


def _registration_response(
    registration: CreatedOrganizationRegistration,
) -> RegistrationResponse:
    return RegistrationResponse(
        id=registration.id,
        id_type=registration.id_type,
        id_number_masked=registration.id_number_masked,
        country_code=registration.country_code,
        is_verified=registration.is_verified,
        verified_at=registration.verified_at,
    )


def _looks_like_server_mask(identifier: str) -> bool:
    """Reject the exact shape emitted by ``mask_id_number``.

    Registration convenience fields are write-only.  A client must never be
    able to round-trip a server mask (for example ``XXXXXX1234``) as a new
    identifier and thereby overwrite the real encrypted value.
    """

    value = identifier.strip()
    if len(value) <= 4:
        return False
    prefix = value[:-4]
    return bool(prefix) and set(prefix) == {"X"}


def _registration_material(
    *,
    id_type: str,
    id_number: str,
    country_code: str,
) -> tuple[str, str, str, str, str | None]:
    """Apply public validation before bounded crypto/database work."""

    if _looks_like_server_mask(id_number):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Masked organization registration identifiers cannot be submitted",
        )

    try:
        validated = RegistrationCreate(
            id_type=id_type,
            id_number=id_number,
            country_code=country_code,
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid organization registration identifier",
        ) from exc

    canonical_type = validated.id_type.strip().upper()
    canonical_country = validated.country_code.strip().upper()
    identifier = validated.id_number
    entity_type = (
        identifier[3].upper()
        if canonical_type == "PAN" and len(identifier) >= 4
        else None
    )
    return (
        canonical_type,
        identifier,
        mask_id_number(identifier),
        canonical_country,
        entity_type,
    )


def _registration_write_http_exception(exc: Exception) -> HTTPException:
    """Map bounded registration failures without controlling a transaction."""

    if isinstance(
        exc,
        (RegistrationMutationAuthorizationError, RegistrationKeyAuthorizationError),
    ):
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Organization registration access denied",
        )
    if isinstance(exc, RegistrationMutationValidationError):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid organization registration identifier",
        )
    if isinstance(exc, RegistrationConflictError):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Organization registration already exists",
        )
    if isinstance(exc, RegistrationTargetNotFoundError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Organization registration changed concurrently; retry the request",
        )
    if isinstance(
        exc,
        (
            RegistrationKeyStateError,
            AWSKMSUnavailableError,
            AWSKMSContractError,
            RegistrationKMSConfigurationError,
        ),
    ):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Organization registration encryption service is unavailable",
        )
    raise exc


async def _raise_registration_write_http(
    db: AsyncSession,
    exc: Exception,
) -> None:
    """Preserve P3B standalone-write rollback behavior and stable HTTP mapping."""

    await db.rollback()
    raise _registration_write_http_exception(exc) from exc


async def _get_profile_or_forbidden(db: AsyncSession) -> dict | None:
    try:
        return await get_current_organization_profile(db)
    except ProfileAuthorizationError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Organization profile access denied",
        ) from exc


async def _get_registrations_or_forbidden(db: AsyncSession) -> list[dict]:
    try:
        return await list_current_organization_registrations(db)
    except RegistrationAuthorizationError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Organization registration access denied",
        ) from exc


@router.post("/registrations", response_model=Response[RegistrationResponse])
async def add_registration(
    data: RegistrationCreate,
    current_staff: Staff = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db)
):
    """Create one KMS-backed registration through the bounded P3B capability."""

    id_type, identifier, masked, country_code, entity_type = _registration_material(
        id_type=data.id_type,
        id_number=data.id_number,
        country_code=data.country_code,
    )
    try:
        registration = await create_secure_organization_registration(
            db,
            id_type=id_type,
            normalized_identifier=identifier,
            masked_identifier=masked,
            country_code=country_code,
            entity_type=entity_type,
        )
        await db.commit()
    except (
        RegistrationMutationAuthorizationError,
        RegistrationMutationValidationError,
        RegistrationKeyStateError,
        RegistrationConflictError,
        RegistrationTargetNotFoundError,
        RegistrationKeyAuthorizationError,
        AWSKMSUnavailableError,
        AWSKMSContractError,
        RegistrationKMSConfigurationError,
    ) as exc:
        await _raise_registration_write_http(db, exc)

    return Response(data=_registration_response(registration))


@router.get("/profile", response_model=Response[OrganizationProfileResponse])
async def get_org_profile(
    current_staff: Staff = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db)
):
    """
    Get organization branding and masked registration details.

    Both the tenant-root profile and registration projection are read through
    database capabilities bound to the verified request principal. Plaintext
    registration identifiers are not decrypted into this response.
    """
    org = await _get_profile_or_forbidden(db)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    registrations = await _get_registrations_or_forbidden(db)
    return Response(data=_profile_response(org, registrations))


@router.patch("/profile", response_model=Response[OrganizationProfileResponse])
async def update_org_profile(
    data: OrganizationUpdate,
    current_staff: Staff = Depends(require_org_admin),
    db: AsyncSession = Depends(get_service_managed_db),
):
    """Atomically update the P3A profile and P3B registration state.

    P3C owns exactly one SQLAlchemy transaction for this combined business
    mutation.  The router validates registration input before the transaction;
    the service then composes only the certified P3A/P3B capabilities.  Any
    profile, registration, KMS, authorization, concurrency, cancellation or
    commit failure rolls the entire combined mutation back.
    """

    update_data = data.model_dump(exclude_unset=True)
    raw_registration_updates: dict[str, str | None] = {}
    if "business_id" in update_data:
        raw_registration_updates["BUSINESS_ID"] = update_data.pop("business_id")
    if "gst_number" in update_data:
        raw_registration_updates["GST"] = update_data.pop("gst_number")
    if "pan_number" in update_data:
        raw_registration_updates["PAN"] = update_data.pop("pan_number")

    # Preserve the existing API meaning: omitted/null/empty registration
    # convenience fields do not mutate stored registration state.  Validate all
    # actual identifier changes before opening the database transaction.
    registration_plans: list[RegistrationMutationPlan] = []
    for requested_type, requested_identifier in raw_registration_updates.items():
        if not requested_identifier:
            continue
        requested_country = "IN" if requested_type in {"GST", "PAN"} else "US"
        (
            id_type,
            identifier,
            masked,
            country_code,
            entity_type,
        ) = _registration_material(
            id_type=requested_type,
            id_number=requested_identifier,
            country_code=requested_country,
        )
        registration_plans.append(
            RegistrationMutationPlan(
                id_type=id_type,
                normalized_identifier=identifier,
                masked_identifier=masked,
                country_code=country_code,
                entity_type=entity_type,
            )
        )

    try:
        result = await mutate_organization_profile_atomically(
            db,
            profile_patch=update_data,
            registration_updates=tuple(registration_plans),
        )
    except OrganizationProfileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Organization not found") from exc
    except ProfileAuthorizationError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Organization profile access denied",
        ) from exc
    except RegistrationAuthorizationError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Organization registration access denied",
        ) from exc
    except (
        RegistrationMutationAuthorizationError,
        RegistrationMutationValidationError,
        RegistrationKeyStateError,
        RegistrationConflictError,
        RegistrationTargetNotFoundError,
        RegistrationKeyAuthorizationError,
        AWSKMSUnavailableError,
        AWSKMSContractError,
        RegistrationKMSConfigurationError,
    ) as exc:
        raise _registration_write_http_exception(exc) from exc
    except OrganizationProfileTransactionStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Organization profile transaction could not be started",
        ) from exc

    return Response(data=_profile_response(result.profile, result.registrations))
