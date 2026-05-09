import uuid
import functools
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from fastapi import HTTPException, status, Request
from jose import JWTError, jwt  # noqa: F401
from passlib.context import CryptContext
from app.core.config import settings
from app.models.enums import StaffRole

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(payload: dict) -> str:
    data = payload.copy()
    data["exp"] = datetime.now(timezone.utc) + timedelta(
        minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
    )
    data["type"] = "access"
    data["jti"] = str(uuid.uuid4())
    return jwt.encode(data, settings.SECRET_KEY, algorithm="HS256")


def create_refresh_token(payload: dict, family_id: Optional[str] = None) -> str:
    data = payload.copy()
    data["exp"] = datetime.now(timezone.utc) + timedelta(
        days=settings.REFRESH_TOKEN_EXPIRE_DAYS
    )
    data["type"] = "refresh"
    data["jti"] = str(uuid.uuid4())
    data["f_id"] = family_id or str(uuid.uuid4())  # Family ID for rotation tracking
    return jwt.encode(data, settings.SECRET_KEY, algorithm="HS256")


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])


def role_required(allowed_roles: List[StaffRole]):
    """
    Decorator to enforce RBAC on FastAPI endpoints.
    Requires TenantMiddleware to have already injected request.state context.
    """
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            # Extract request from kwargs (FastAPI injects it if type-hinted)
            request = kwargs.get("request")
            if not request:
                # Fallback to searching args
                for arg in args:
                    if isinstance(arg, Request):
                        request = arg
                        break
            
            if not request:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="RBAC decorator requires Request object"
                )

            user_role = getattr(request.state, "role", None)
            if not user_role or user_role not in [role.value for role in allowed_roles]:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Not enough permissions"
                )
            return await func(*args, **kwargs)
        return wrapper
    return decorator
