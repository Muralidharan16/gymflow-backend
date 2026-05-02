from fastapi import APIRouter, Header, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from ..database import get_db
from ..schemas.attendance import AccessVerifyRequest, AccessVerifyResponse
from ..services.access_control import verify_and_process_access
from ..redis_client import rate_limit, redis_client
from ..config import settings

router = APIRouter(prefix="/access", tags=["access"])


@router.post('/verify', response_model=AccessVerifyResponse)
async def access_verify(payload: AccessVerifyRequest, x_bridge_token: str | None = Header(None), db: AsyncSession = Depends(get_db)):
    if not x_bridge_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Missing bridge token')

    # rate limit per bridge token
    key = f"rate:access:{x_bridge_token}"
    allowed = await rate_limit(key, limit=int(settings.RATE_LIMIT_PER_MINUTE), period_seconds=60)
    if not allowed:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail='Rate limit exceeded')

    try:
        result = await verify_and_process_access(db, redis_client, x_bridge_token, payload.device_id, payload.fingerprint_id)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc))

    return AccessVerifyResponse(
        allowed=bool(result.get('allowed')),
        member_id=result.get('member_id'),
        member_name=result.get('member_name'),
        subscription_end=result.get('subscription_end'),
        reason=result.get('reason')
    )
