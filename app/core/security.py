import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any

import jwt
from jwt.exceptions import PyJWTError

from app.core.config import settings
from app.core.exceptions import SecurityError, InvalidTokenError, ExpiredTokenError


def create_access_token(payload: dict) -> str:
    """
    Create JWT access token.
    
    Args:
        payload: Dictionary with claims (e.g., {"sub": staff_id, "org_id": ..., "gym_id": ...})
    
    Returns:
        Encoded JWT string
    """
    data = payload.copy()
    data["exp"] = datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    data["type"] = "access"
    data["jti"] = str(uuid.uuid4())
    return jwt.encode(data, settings.SECRET_KEY, algorithm="HS256")


def create_refresh_token(payload: dict, family_id: str = None) -> str:
    """
    Create JWT refresh token with family ID for token rotation.
    
    Args:
        payload: Dictionary with claims (e.g., {"sub": staff_id})
        family_id: Optional family identifier for token rotation. If None, generates new UUID.
    
    Returns:
        Encoded JWT string
    """
    data = payload.copy()
    data["exp"] = datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    data["type"] = "refresh"
    data["jti"] = str(uuid.uuid4())
    data["f_id"] = family_id or str(uuid.uuid4())
    return jwt.encode(data, settings.SECRET_KEY, algorithm="HS256")


def decode_token(token: str) -> Dict[str, Any]:
    """
    Decode and validate JWT token.
    
    Args:
        token: JWT string
    
    Returns:
        Dictionary of claims
    
    Raises:
        InvalidTokenError: If token is malformed or invalid
        ExpiredTokenError: If token has expired
    """
    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=["HS256"],
            options={"verify_exp": True}
        )
        return payload
    except jwt.ExpiredSignatureError as e:
        raise ExpiredTokenError("Token has expired") from e
    except PyJWTError as e:
        raise InvalidTokenError(f"Invalid token: {str(e)}") from e


def verify_token(token: str, expected_type: str = "access") -> Dict[str, Any]:
    """
    Decode token and verify its type matches expected.
    
    Args:
        token: JWT string
        expected_type: Either "access" or "refresh"
    
    Returns:
        Verified claims
    
    Raises:
        InvalidTokenError: If type mismatch or token invalid
    """
    payload = decode_token(token)
    if payload.get("type") != expected_type:
        raise InvalidTokenError(f"Expected {expected_type} token, got {payload.get('type')}")
    return payload


def get_token_family_id(refresh_token: str) -> str:
    """
    Extract family_id from refresh token.
    
    Args:
        refresh_token: Valid refresh token
    
    Returns:
        family_id string
    
    Raises:
        InvalidTokenError: If token lacks f_id claim
    """
    payload = decode_token(refresh_token)
    if payload.get("type") != "refresh":
        raise InvalidTokenError("Not a refresh token")
    f_id = payload.get("f_id")
    if not f_id:
        raise InvalidTokenError("Refresh token missing family_id claim")
    return f_id


def get_token_jti(token: str) -> str:
    """
    Extract jti (JWT ID) from any token.
    
    Args:
        token: JWT string
    
    Returns:
        jti string
    
    Raises:
        InvalidTokenError: If token lacks jti claim
    """
    payload = decode_token(token)
    jti = payload.get("jti")
    if not jti:
        raise InvalidTokenError("Token missing jti claim")
    return jti