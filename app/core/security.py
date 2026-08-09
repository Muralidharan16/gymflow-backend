import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import bcrypt
from jose import ExpiredSignatureError, JWTError, jwt
from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError

from app.core.config import settings
from app.core.exceptions import ExpiredTokenError, InvalidTokenError, SecurityError


# New credentials use Argon2id via pwdlib's recommended hasher. Existing bcrypt
# hashes remain verifiable so deployed users are not locked out during the
# transition away from Passlib. Legacy bcrypt historically uses only the first
# 72 password bytes; preserving that behavior here is required to verify hashes
# that may already exist in the database. Newly-created Argon2 hashes have no
# such truncation behavior.
password_hash = PasswordHash.recommended()
_BCRYPT_PREFIXES = ("$2a$", "$2b$", "$2y$")
_BCRYPT_MAX_PASSWORD_BYTES = 72


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
    "address_exclusion": "Addresses are never serialized into tokens. Address data is always fetched fresh from DB per request via RLS-scoped session.",
}


def create_access_token(
    owner_id: str,
    org_id: str,
    email: str,
    role: str = "owner",
    branch_ids: list[str] = None,
) -> str:
    """Create JWT access token as per spec."""
    payload = {
        "sub": str(owner_id),
        "org_id": str(org_id),
        "email": email,
        "role": role,
        "branch_ids": branch_ids or [],
        "type": "access",
        "jti": str(uuid.uuid4()),
        "exp": datetime.now(timezone.utc) + timedelta(minutes=15),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")


def create_refresh_token(owner_id: str) -> str:
    """Create JWT refresh token as per spec."""
    payload = {
        "sub": str(owner_id),
        "type": "refresh",
        "jti": str(uuid.uuid4()),
        "exp": datetime.now(timezone.utc) + timedelta(days=7),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")


def decode_token(token: str) -> Dict[str, Any]:
    """Decode and validate a JWT token."""
    try:
        return jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=["HS256"],
            options={"verify_exp": True},
        )
    except ExpiredSignatureError:
        raise ExpiredTokenError("Token has expired")
    except JWTError as exc:
        raise InvalidTokenError(f"Invalid token: {str(exc)}") from exc


def verify_token(token: str, expected_type: str = "access") -> Dict[str, Any]:
    """Decode a token and verify its type matches ``expected_type``."""
    payload = decode_token(token)
    if payload.get("type") != expected_type:
        raise InvalidTokenError(
            f"Expected {expected_type} token, got {payload.get('type')}"
        )
    return payload


def get_token_family_id(refresh_token: str) -> str:
    """Extract family_id from a valid refresh token."""
    payload = decode_token(refresh_token)
    if payload.get("type") != "refresh":
        raise InvalidTokenError("Not a refresh token")
    family_id = payload.get("f_id")
    if not family_id:
        raise InvalidTokenError("Refresh token missing family_id claim")
    return family_id


def get_token_jti(token: str) -> str:
    """Extract jti (JWT ID) from any valid token."""
    payload = decode_token(token)
    jti = payload.get("jti")
    if not jti:
        raise InvalidTokenError("Token missing jti claim")
    return jti


def hash_password(password: str) -> str:
    """Hash a new password with the current Argon2id policy."""
    return password_hash.hash(password)


def _verify_legacy_bcrypt(password: str, hashed_password: str) -> bool:
    """Verify a pre-existing bcrypt hash without Passlib.

    Older bcrypt implementations silently ignored bytes beyond byte 72. We
    preserve that historical verification behavior only for existing bcrypt
    hashes so current users are not locked out. All new hashes are Argon2id.
    """
    try:
        password_bytes = password.encode("utf-8")[:_BCRYPT_MAX_PASSWORD_BYTES]
        hash_bytes = hashed_password.encode("ascii")
        return bcrypt.checkpw(password_bytes, hash_bytes)
    except (TypeError, ValueError, UnicodeEncodeError):
        return False


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify Argon2id credentials and legacy bcrypt credentials safely."""
    if not isinstance(hashed_password, str) or not hashed_password:
        return False

    if hashed_password.startswith(_BCRYPT_PREFIXES):
        return _verify_legacy_bcrypt(plain_password, hashed_password)

    try:
        return password_hash.verify(plain_password, hashed_password)
    except (UnknownHashError, TypeError, ValueError):
        return False
