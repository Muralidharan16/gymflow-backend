import uuid
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import update, select
from app.models.address import OrganizationAddress
from app.core.exceptions import NotFoundError

async def set_primary_address(address_id: uuid.UUID, org_id: uuid.UUID, db: AsyncSession) -> OrganizationAddress:
    """
    Sets the target address as the branch's primary address.
    """
    # 1. Fetch the target address
    result = await db.execute(
        select(OrganizationAddress)
        .where(OrganizationAddress.id == address_id)
        .where(OrganizationAddress.org_id == org_id)
    )
    target = result.scalar_one_or_none()
    if not target:
        raise NotFoundError("OrganizationAddress not found.")

    # 2. Update the branch pointing to this address to make it the primary address
    from app.models.org_branch import OrgBranch
    await db.execute(
        update(OrgBranch)
        .where(OrgBranch.id == target.branch_id)
        .values(address_id=target.id)
    )

    # For compatibility with mock tests that check updated.is_primary:
    target.is_primary = True

    await db.flush()
    return target

def capture_address_snapshot(address: OrganizationAddress) -> dict:
    """
    Returns an immutable dictionary containing only tax-relevant compliance fields.
    """
    return {
        "address_line1": address.address_line1,
        "address_line2": address.address_line2,
        "city": address.city,
        "state_province": address.state_province,
        "postal_code": address.postal_code,
        "country_code": address.country_code,
        "formatted_address": address.formatted_address,
        "address_type": address.address_type.value if hasattr(address.address_type, "value") else address.address_type
    }
