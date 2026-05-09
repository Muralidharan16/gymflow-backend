from typing import Optional, List
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from ..config import settings
from ..database import get_db
from ..models.models import Staff, Organization, StaffRole
from ..schemas.tenant import TenantContext

security = HTTPBearer(auto_error=False)

async def get_tenant_context(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> TenantContext:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing credentials")

    token = credentials.credentials
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    staff_id = payload.get('sub')
    org_id = payload.get('org_id')
    
    if staff_id is None or org_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Malformed token")

    # Fast DB Check
    stmt = select(Staff).where(Staff.id == staff_id, Staff.org_id == org_id, Staff.deleted_at.is_(None))
    result = await db.execute(stmt)
    staff = result.scalar_one_or_none()
    
    if not staff or not staff.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Staff account inactive or deleted")

    # Fast DB Check
    org_stmt = select(Organization).where(Organization.id == org_id)
    org_result = await db.execute(org_stmt)
    org = org_result.scalar_one_or_none()
    
    if not org or not org.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Organization is inactive")

    return TenantContext(
        org_id=staff.org_id,
        primary_branch_id=staff.primary_branch_id,
        role=staff.role,
        staff_id=staff.id
    )

def RequireRole(allowed_roles: List[StaffRole]):
    async def role_checker(context: TenantContext = Depends(get_tenant_context)) -> TenantContext:
        if context.role not in allowed_roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
        return context
    return role_checker
