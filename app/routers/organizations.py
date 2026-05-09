from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from ..database import get_db
from ..middleware.auth_middleware import get_tenant_context, RequireRole
from ..schemas.tenant import TenantContext
from ..models.models import StaffRole
from ..services.organizations import generate_next_branch_code

router = APIRouter(prefix="/organizations", tags=["organizations"])

@router.get("/next-branch-code")
async def get_next_branch_code(
    context: TenantContext = Depends(RequireRole([StaffRole.owner, StaffRole.admin])),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns the next sequential branch code for the current organization.
    Useful for the frontend to preview the branch code (e.g. BR002).
    """
    next_code = await generate_next_branch_code(db, context.org_id)
    return {"next_branch_code": next_code}
