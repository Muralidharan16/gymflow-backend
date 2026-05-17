from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.database import get_db
from app.core.deps import require_org_admin, Staff
from app.models.organization import OrganizationRegistration, Organization
from app.schemas.organization import RegistrationCreate, RegistrationResponse, OrganizationProfileResponse, OrganizationUpdate
from app.schemas.common import Response
from app.utils.encryption import encrypt_data, mask_id_number

router = APIRouter(prefix="/organizations", tags=["Organizations"])

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
    """
    q = select(Organization).where(Organization.id == current_staff.org_id)
    result = await db.execute(q)
    org = result.scalar_one_or_none()
    
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    # Get registrations
    reg_q = select(OrganizationRegistration).where(OrganizationRegistration.org_id == org.id)
    reg_res = await db.execute(reg_q)
    registrations = reg_res.scalars().all()

    return Response(data=OrganizationProfileResponse(
        id=org.id,
        name=org.name,
        tagline=org.tagline,
        description=org.description,
        year_established=org.year_established,
        website_url=org.website_url,
        social_links=org.social_links,
        registrations=[RegistrationResponse.model_validate(r, from_attributes=True) for r in registrations]
    ))

@router.patch("/profile", response_model=Response[OrganizationProfileResponse])
async def update_org_profile(
    data: OrganizationUpdate,
    current_staff: Staff = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db)
):
    """
    Update organization branding and profile details.
    """
    q = select(Organization).where(Organization.id == current_staff.org_id)
    result = await db.execute(q)
    org = result.scalar_one_or_none()
    
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    # Update fields
    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(org, key, value)
    
    await db.commit()
    await db.refresh(org)

    # Get registrations for response
    reg_q = select(OrganizationRegistration).where(OrganizationRegistration.org_id == org.id)
    reg_res = await db.execute(reg_q)
    registrations = reg_res.scalars().all()

    return Response(data=OrganizationProfileResponse(
        id=org.id,
        name=org.name,
        tagline=org.tagline,
        description=org.description,
        year_established=org.year_established,
        website_url=org.website_url,
        social_links=org.social_links,
        registrations=[RegistrationResponse.model_validate(r, from_attributes=True) for r in registrations]
    ))
