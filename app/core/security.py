import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)

from jose import jwt, JWTError, ExpiredSignatureError

from app.core.config import settings
from app.core.exceptions import SecurityError, InvalidTokenError, ExpiredTokenError


import secrets
import hashlib

def generate_magic_token() -> str:
    """Generate 64-char URL-safe string."""
    return secrets.token_urlsafe(48)

def hash_token(token: str) -> str:
    """SHA256 hash for storage."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

# =====================================================================
# SECURITY COMPLIANCE POLICY: JWT TOKEN STRUCTURE
# ---------------------------------------------------------------------
# Addresses are never serialized into tokens. Doing so introduces high risks 
# of stale location bindings, vertical authorization escalation, and leaks of 
# customer PII in plain client-readable formats. 
# Address data is always fetched fresh from the DB per request via RLS-scoped sessions.
# =====================================================================
SECURITY_POLICY = {
    "token_payload_minimalist": True,
    "address_exclusion": "Addresses are never serialized into tokens. Address data is always fetched fresh from DB per request via RLS-scoped session."
}

def create_access_token(owner_id: str, org_id: str, email: str, role: str = "owner", branch_ids: list[str] = None) -> str:

    """
    Create JWT access token as per spec.
    """
    payload = {
        "sub": str(owner_id),
        "org_id": str(org_id),
        "email": email,
        "role": role,
        "branch_ids": branch_ids or [],
        "type": "access",
        "jti": str(uuid.uuid4()),
        "exp": datetime.now(timezone.utc) + timedelta(minutes=15)
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")


def create_refresh_token(owner_id: str) -> str:
    """
    Create JWT refresh token as per spec.
    """
    payload = {
        "sub": str(owner_id),
        "type": "refresh",
        "exp": datetime.now(timezone.utc) + timedelta(days=7)
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")


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
    except ExpiredSignatureError:
        raise ExpiredTokenError("Token has expired")
    except JWTError as e:
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

def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)