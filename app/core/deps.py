import uuid
from typing import Optional
from fastapi import Request, HTTPException, Depends
from pydantic import BaseModel

class Staff(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    gym_id: Optional[uuid.UUID]
    role: str

def get_current_active_staff(request: Request) -> Staff:
    if not hasattr(request.state, "staff_id"):
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    gym_id_val = request.state.gym_id
    if gym_id_val == "None" or not gym_id_val:
        gym_id_val = None
    elif isinstance(gym_id_val, str):
        gym_id_val = uuid.UUID(gym_id_val)

    return Staff(
        id=uuid.UUID(request.state.staff_id) if isinstance(request.state.staff_id, str) else request.state.staff_id,
        org_id=uuid.UUID(request.state.org_id) if isinstance(request.state.org_id, str) else request.state.org_id,
        gym_id=gym_id_val,
        role=request.state.role
    )

def require_org_admin(staff: Staff = Depends(get_current_active_staff)) -> Staff:
    if staff.role not in ("owner", "admin") or staff.gym_id is not None:
        raise HTTPException(status_code=403, detail="Organization admin access required")
    return staff

async def require_gym_access(
    gym_id: uuid.UUID, 
    staff: Staff = Depends(get_current_active_staff)
) -> Staff:
    if staff.gym_id is not None and staff.gym_id != gym_id:
        raise HTTPException(status_code=403, detail="Access denied to this branch")
    return staff
