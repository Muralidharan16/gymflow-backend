from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from uuid import uuid4
from ..database import get_db
from ..middleware.auth_middleware import get_current_owner
from ..config import settings
from ..models.models import Device, Member
from ..schemas.device import DeviceRegisterRequest, DeviceRegisterResponse, DeviceStatusResponse

router = APIRouter(prefix="/devices", tags=["devices"])


@router.post('/connect', response_model=DeviceRegisterResponse)
async def connect_device(payload: DeviceRegisterRequest, x_bridge_init_token: str | None = Header(None), owner = Depends(get_current_owner), db: AsyncSession = Depends(get_db)):
    # Two modes: owner-authenticated registration or init token registration
    if x_bridge_init_token:
        if x_bridge_init_token != settings.BRIDGE_DEFAULT_TOKEN:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Invalid init token')
        # init token registration requires owner to specify gym via owner context
        gym_id = owner.gym_id
    else:
        gym_id = owner.gym_id

    auth_token = str(uuid4())
    device = Device(gym_id=gym_id, device_ip=payload.device_ip, device_model=payload.device_model, auth_token=auth_token)
    db.add(device)
    await db.commit()
    await db.refresh(device)
    return DeviceRegisterResponse(id=str(device.id), auth_token=device.auth_token)


@router.get('/status', response_model=list[DeviceStatusResponse])
async def devices_status(owner = Depends(get_current_owner), db: AsyncSession = Depends(get_db)):
    q = await db.execute(select(Device).where(Device.gym_id == owner.gym_id))
    devices = q.scalars().all()
    return [DeviceStatusResponse(id=str(d.id), device_ip=d.device_ip, device_model=d.device_model, last_connected=(d.last_connected.isoformat() if d.last_connected else None), status=d.status.value) for d in devices]


@router.post('/sync-members')
async def sync_members(x_bridge_token: str | None = Header(None), db: AsyncSession = Depends(get_db)):
    if not x_bridge_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Missing bridge token')
    q = await db.execute(select(Device).where(Device.auth_token == x_bridge_token))
    device = q.scalar_one_or_none()
    if device is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Invalid bridge token')
    # return members with fingerprint_id for this gym
    q = await db.execute(select(Member).where(Member.gym_id == device.gym_id, Member.fingerprint_id != None))
    members = q.scalars().all()
    items = []
    for m in members:
        items.append({ 'member_id': str(m.id), 'name': m.name, 'fingerprint_id': m.fingerprint_id })
    return { 'members': items }
