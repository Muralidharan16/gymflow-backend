from typing import List
from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.database import get_db
from app.core.deps import require_org_admin, Staff
from app.models.organization import OrganizationRegistration
from app.repositories.organization_profile import (
    ProfileAuthorizationError,
    get_current_organization_profile,
    update_current_organization_profile,
)
from app.schemas.organization import (
    RegistrationCreate, RegistrationResponse, OrganizationProfileResponse, OrganizationUpdate,
    LogoUploadUrlResponse, LogoConfirmRequest, LogoStatusResponse
)
from app.schemas.common import Response
from app.utils.encryption import encrypt_data, mask_id_number, decrypt_data
import uuid
from app.utils.s3 import get_s3_client
from app.core.config import settings
from app.tasks.logos import process_org_logo
from app.core.deps import get_current_active_staff

router = APIRouter(prefix="/organizations", tags=["Organizations"])


def _profile_response(
    org: dict,
    registrations: list[OrganizationRegistration],
) -> OrganizationProfileResponse:
    business_id = None
    gst_number = None
    pan_number = None
    for registration in registrations:
        if registration.id_type == "BUSINESS_ID":
            business_id = decrypt_data(registration.id_number_encrypted)
        elif registration.id_type == "GST":
            gst_number = decrypt_data(registration.id_number_encrypted)
        elif registration.id_type == "PAN":
            pan_number = decrypt_data(registration.id_number_encrypted)

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
            RegistrationResponse.model_validate(r, from_attributes=True)
            for r in registrations
        ],
        business_id=business_id,
        gst_number=gst_number,
        pan_number=pan_number,
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


@router.post("/registrations", response_model=Response[RegistrationResponse])
async def add_registration(
    data: RegistrationCreate,
    current_staff: Staff = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db)
):
    """
    Add a business registration ID (PAN, VAT, etc.)
    """
    # 1. Check if same type/country already exists for this org
    q = select(OrganizationRegistration).where(
        OrganizationRegistration.org_id == current_staff.org_id,
        OrganizationRegistration.id_type == data.id_type,
        OrganizationRegistration.country_code == data.country_code
    )
    result = await db.execute(q)
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Registration of type {data.id_type} for {data.country_code} already exists."
        )

    # 2. Extract entity type for PAN
    entity_type = None
    if data.id_type.upper() == "PAN" and len(data.id_number) >= 4:
        entity_type = data.id_number[3].upper()

    # 3. Encrypt and Mask
    encrypted_id = encrypt_data(data.id_number)
    masked_id = mask_id_number(data.id_number)

    # 4. Save
    reg = OrganizationRegistration(
        org_id=current_staff.org_id,
        id_type=data.id_type.upper(),
        id_number_encrypted=encrypted_id,
        id_number_masked=masked_id,
        country_code=data.country_code.upper(),
        entity_type=entity_type
    )
    db.add(reg)
    await db.commit()
    await db.refresh(reg)
    
    return Response(data=RegistrationResponse.model_validate(reg, from_attributes=True))


@router.get("/profile", response_model=Response[OrganizationProfileResponse])
async def get_org_profile(
    current_staff: Staff = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db)
):
    """
    Get organization branding and registration details.

    The tenant-root organization row is read through a database capability
    bound to app.current_org_id. app_runtime intentionally has no direct
    organizations SELECT privilege.
    """
    org = await _get_profile_or_forbidden(db)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    reg_q = select(OrganizationRegistration).where(
        OrganizationRegistration.org_id == current_staff.org_id
    )
    reg_res = await db.execute(reg_q)
    registrations = reg_res.scalars().all()

    return Response(data=_profile_response(org, registrations))


@router.patch("/profile", response_model=Response[OrganizationProfileResponse])
async def update_org_profile(
    data: OrganizationUpdate,
    current_staff: Staff = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db)
):
    """
    Update organization branding and profile details.

    P3A applies organization-table fields only through the bounded
    current-tenant database capability. Registration mutation remains a
    separate transaction here and is intentionally deferred to P3C.
    """
    update_data = data.model_dump(exclude_unset=True)

    # Registration fields are not organization-table profile columns.
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

    # Process registrations. P3B/P3C will replace this direct registration
    # access with its own hardened boundary and a single atomic transaction.
    if reg_updates:
        for id_type, id_number in reg_updates.items():
            if not id_number:
                continue

            q_reg = select(OrganizationRegistration).where(
                OrganizationRegistration.org_id == current_staff.org_id,
                OrganizationRegistration.id_type == id_type
            )
            res_reg = await db.execute(q_reg)
            reg = res_reg.scalar_one_or_none()

            encrypted_id = encrypt_data(id_number)
            masked_id = mask_id_number(id_number)

            if reg:
                reg.id_number_encrypted = encrypted_id
                reg.id_number_masked = masked_id
            else:
                country_code = "IN" if id_type in ["GST", "PAN"] else "US"
                entity_type = (
                    id_number[3].upper()
                    if id_type == "PAN" and len(id_number) >= 4
                    else None
                )
                new_reg = OrganizationRegistration(
                    org_id=current_staff.org_id,
                    id_type=id_type,
                    id_number_encrypted=encrypted_id,
                    id_number_masked=masked_id,
                    country_code=country_code,
                    entity_type=entity_type
                )
                db.add(new_reg)
        await db.commit()

    # Re-read through the bounded capability instead of ORM refresh(), which
    # would require whole-row organizations SELECT.
    org = await _get_profile_or_forbidden(db)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    reg_q = select(OrganizationRegistration).where(
        OrganizationRegistration.org_id == current_staff.org_id
    )
    reg_res = await db.execute(reg_q)
    registrations = reg_res.scalars().all()

    return Response(data=_profile_response(org, registrations))
