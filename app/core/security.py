import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import bcrypt
import jwt
from jwt.exceptions import ExpiredSignatureError, PyJWTError
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

# Access-token subjects are explicitly typed. UUIDs alone are not a sufficient
# identity contract because this codebase currently contains both the owner
# authentication domain and the organization-user RBAC domain.
ACCESS_TOKEN_PRINCIPAL_TYPES = frozenset({"owner", "organization_user"})


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
    "typed_principals": True,
}


def _authentication_epoch(value: int | datetime | None = None) -> int:
    if value is None:
        return int(datetime.now(timezone.utc).timestamp())
    if isinstance(value, bool):
        raise ValueError("auth_time must be an epoch integer or datetime")
    if isinstance(value, datetime):
        normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return int(normalized.timestamp())
    if isinstance(value, int) and value >= 0:
        return value
    raise ValueError("auth_time must be an epoch integer or datetime")


def create_access_token(
    owner_id: str,
    org_id: str,
    email: str,
    role: str = "owner",
    branch_ids: list[str] = None,
    principal_type: str = "owner",
    auth_time: int | datetime | None = None,
    family_id: str | None = None,
) -> str:
    """Create a short-lived typed access token with session/step-up evidence."""
    normalized_principal_type = str(principal_type).strip().lower()
    if normalized_principal_type not in ACCESS_TOKEN_PRINCIPAL_TYPES:
        raise ValueError(
            f"Unsupported access-token principal type: {principal_type!r}"
        )

    now = datetime.now(timezone.utc)
    authenticated_at = _authentication_epoch(auth_time)
    payload = {
        "sub": str(owner_id),
        "principal_type": normalized_principal_type,
        "org_id": str(org_id),
        "email": email,
        "role": role,
        "branch_ids": branch_ids or [],
        "type": "access",
        "jti": str(uuid.uuid4()),
        "iat": int(now.timestamp()),
        "auth_time": authenticated_at,
        "exp": now + timedelta(minutes=15),
    }
    if family_id:
        payload["f_id"] = str(family_id)
    return jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")


def create_refresh_token(
    owner_id: str,
    auth_time: int | datetime | None = None,
    family_id: str | None = None,
) -> str:
    """Create a rotating refresh token that preserves the original auth time."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(owner_id),
        "type": "refresh",
        "jti": str(uuid.uuid4()),
        "iat": int(now.timestamp()),
        "auth_time": _authentication_epoch(auth_time),
        "exp": now + timedelta(days=7),
    }
    if family_id:
        payload["f_id"] = str(family_id)
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
    except PyJWTError as exc:
        raise InvalidTokenError(f"Invalid token: {str(exc)}") from exc


def verify_token(token: str, expected_type: str = "access") -> Dict[str, Any]:
    """Decode a token and verify its type matches ``expected_type``."""
    payload = decode_token(token)
    if payload.get("type") != expected_type:
        raise InvalidTokenError(
            f"Expected {expected_type} token, got {payload.get('type')}"
        )
    return payload


def finance_csrf_token(access_token: str) -> str:
    """Derive a CSRF proof bound to one signed access-token JTI.

    The token carries no credential material and can safely be exposed to the
    browser as a non-HttpOnly cookie. The access token itself remains HttpOnly.
    """
    payload = verify_token(access_token, expected_type="access")
    jti = payload.get("jti")
    if not isinstance(jti, str) or not jti:
        raise InvalidTokenError("Access token missing jti claim")
    message = f"doers-finance-csrf-v1:{jti}".encode("utf-8")
    return hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        message,
        hashlib.sha256,
    ).hexdigest()


def verify_finance_csrf_token(access_token: str, supplied_token: str) -> bool:
    if not isinstance(supplied_token, str) or not supplied_token:
        return False
    try:
        expected = finance_csrf_token(access_token)
    except (InvalidTokenError, ExpiredTokenError):
        return False
    return hmac.compare_digest(expected, supplied_token)


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
