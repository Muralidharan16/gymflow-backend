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
from app.core.deps import require_org_admin, Staff
from app.repositories.organization_profile import (
    ProfileAuthorizationError,
    get_current_organization_profile,
    update_current_organization_profile,
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
from app.services.organization_registration_service import (
    create_secure_organization_registration,
    replace_secure_organization_registration,
)
from app.utils.encryption import mask_id_number
import uuid
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


def _registration_material(
    *,
    id_type: str,
    id_number: str,
    country_code: str,
) -> tuple[str, str, str, str, str | None]:
    """Apply the public registration schema before bounded crypto/database work."""

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


async def _raise_registration_write_http(
    db: AsyncSession,
    exc: Exception,
) -> None:
    """Rollback a failed registration transaction and expose a stable HTTP error."""

    await db.rollback()
    if isinstance(
        exc,
        (RegistrationMutationAuthorizationError, RegistrationKeyAuthorizationError),
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Organization registration access denied",
        ) from exc
    if isinstance(exc, RegistrationMutationValidationError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid organization registration identifier",
        ) from exc
    if isinstance(exc, RegistrationConflictError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Organization registration already exists",
        ) from exc
    if isinstance(exc, RegistrationTargetNotFoundError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Organization registration changed concurrently; retry the request",
        ) from exc
    if isinstance(
        exc,
        (
            RegistrationKeyStateError,
            AWSKMSUnavailableError,
            AWSKMSContractError,
            RegistrationKMSConfigurationError,
        ),
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Organization registration encryption service is unavailable",
        ) from exc
    raise exc


async def _get_profile_or_forbidden(db: AsyncSession) -> dict | None:
    try:
        return await get_current_organization_profile(db)
    except ProfileAuthorizationError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Organization profile access denied",
        ) from exc


async def _update_profile_or_forbidden(
    db: AsyncSession,
    patch: dict,
) -> dict | None:
    try:
        return await update_current_organization_profile(db, patch)
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
    db: AsyncSession = Depends(get_db)
):
    """
    Update organization branding and profile details.

    P3A applies organization-table fields through its bounded capability. P3B
    applies registration changes through separate KMS-backed create/replace
    capabilities. These domains intentionally remain separate transactions;
    P3C alone owns cross-domain atomic composition.
    """
    update_data = data.model_dump(exclude_unset=True)

    reg_updates = {}
    if "business_id" in update_data:
        reg_updates["BUSINESS_ID"] = update_data.pop("business_id")
    if "gst_number" in update_data:
        reg_updates["GST"] = update_data.pop("gst_number")
    if "pan_number" in update_data:
        reg_updates["PAN"] = update_data.pop("pan_number")

    if update_data:
        org = await _update_profile_or_forbidden(db, update_data)
        if not org:
            raise HTTPException(status_code=404, detail="Organization not found")
        await db.commit()
    else:
        org = await _get_profile_or_forbidden(db)
        if not org:
            raise HTTPException(status_code=404, detail="Organization not found")

    pending_registration_updates = [
        (id_type, id_number)
        for id_type, id_number in reg_updates.items()
        if id_number
    ]
    if pending_registration_updates:
        registrations = await _get_registrations_or_forbidden(db)
        by_business_key = {
            (
                str(registration["id_type"]).strip().upper(),
                str(registration["country_code"]).strip().upper(),
            ): registration
            for registration in registrations
        }

        try:
            for requested_type, requested_identifier in pending_registration_updates:
                requested_country = (
                    "IN" if requested_type in {"GST", "PAN"} else "US"
                )
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
                existing = by_business_key.get((id_type, country_code))
                if existing is None:
                    registration = await create_secure_organization_registration(
                        db,
                        id_type=id_type,
                        normalized_identifier=identifier,
                        masked_identifier=masked,
                        country_code=country_code,
                        entity_type=entity_type,
                    )
                else:
                    registration = await replace_secure_organization_registration(
                        db,
                        registration_id=uuid.UUID(str(existing["id"])),
                        id_type=id_type,
                        normalized_identifier=identifier,
                        masked_identifier=masked,
                        country_code=country_code,
                        entity_type=entity_type,
                    )
                by_business_key[(id_type, country_code)] = {
                    "id": registration.id,
                    "id_type": registration.id_type,
                    "country_code": registration.country_code,
                }
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

    org = await _get_profile_or_forbidden(db)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    registrations = await _get_registrations_or_forbidden(db)
    return Response(data=_profile_response(org, registrations))