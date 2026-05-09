from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from ..models.models import GymBranch

async def generate_next_branch_code(db: AsyncSession, org_id: str) -> str:
    """Generates the next sequential branch code for an organization (e.g. BR001, BR002)."""
    stmt = select(func.count(GymBranch.id)).where(GymBranch.org_id == org_id)
    result = await db.execute(stmt)
    count = result.scalar() or 0
    
    return f"BR{count + 1:03d}"
