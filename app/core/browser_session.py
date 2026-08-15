"""Browser-session security helpers for cookie-based owner authentication."""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status
from starlette.responses import Response

from app.core.config import settings
from app.core.redis import get_redis_utils
from app.core.security import decode_token
from app.schemas.auth import TokenResponse

ACCESS_COOKIE_NAME = "access_token"
REFRESH_COOKIE_NAME = "refresh_token"
SIGNUP_POLL_COOKIE_NAME = "signup_poll_token"
_SIGNUP_POLL_TTL_SECONDS = 10 * 60
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _access_ttl_seconds() -> int:
    """Keep the browser cookie lifetime aligned with the signed access JWT."""
    return int(settings.ACCESS_TOKEN_EXPIRE_MINUTES) * 60


def _refresh_ttl_seconds() -> int:
    """Keep refresh cookies and family-revocation markers aligned with refresh JWTs."""
    return int(settings.REFRESH_TOKEN_EXPIRE_DAYS) * 24 * 60 * 60


def _normalized_origin(value: str) -> str:
    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    if not parsed.scheme or not parsed.hostname:
        return ""
    port = parsed.port
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    authority = parsed.hostname.lower()
    if port and port != default_port:
        authority = f"{authority}:{port}"
    return f"{parsed.scheme.lower()}://{authority}"


def trusted_browser_origins() -> set[str]:
    """Return the exact configured browser origins allowed to mutate cookie sessions."""
    origins = {_normalized_origin(settings.FRONTEND_URL)}
    origins.update(_normalized_origin(origin) for origin in settings.cors_origins_list)
    return {origin for origin in origins if origin}


def require_trusted_browser_origin(request: Request) -> None:
    """Reject unsafe production browser requests that do not come from a trusted Origin."""
    if settings.ENVIRONMENT != "production" or request.method.upper() not in _UNSAFE_METHODS:
        return

    origin = _normalized_origin(request.headers.get("origin", ""))
    if not origin or origin not in trusted_browser_origins():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Untrusted browser origin",
        )


def set_auth_cookies(response: Response, tokens: TokenResponse) -> None:
    """Set browser credentials only in HttpOnly cookies with narrow refresh scope."""
    is_prod = settings.ENVIRONMENT == "production"
    response.set_cookie(
        key=ACCESS_COOKIE_NAME,
        value=tokens.access_token,
        httponly=True,
        secure=is_prod,
        samesite="lax",
        path="/",
        max_age=_access_ttl_seconds(),
    )
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=tokens.refresh_token,
        httponly=True,
        secure=is_prod,
        samesite="lax",
        path="/auth",
        max_age=_refresh_ttl_seconds(),
    )
    response.headers["Cache-Control"] = "no-store"


def clear_auth_cookies(response: Response) -> None:
    """Expire both browser credential cookies using their exact issuance paths."""
    is_prod = settings.ENVIRONMENT == "production"
    response.delete_cookie(
        ACCESS_COOKIE_NAME,
        path="/",
        httponly=True,
        secure=is_prod,
        samesite="lax",
    )
    response.delete_cookie(
        REFRESH_COOKIE_NAME,
        path="/auth",
        httponly=True,
        secure=is_prod,
        samesite="lax",
    )
    response.headers["Cache-Control"] = "no-store"


def set_signup_poll_cookie(response: Response, raw_token: str) -> None:
    """Store the one-time signup-session capability outside JavaScript-visible storage."""
    response.set_cookie(
        key=SIGNUP_POLL_COOKIE_NAME,
        value=raw_token,
        httponly=True,
        secure=settings.ENVIRONMENT == "production",
        samesite="lax",
        path="/auth",
        max_age=_SIGNUP_POLL_TTL_SECONDS,
    )
    response.headers["Cache-Control"] = "no-store"


def clear_signup_poll_cookie(response: Response) -> None:
    response.delete_cookie(
        SIGNUP_POLL_COOKIE_NAME,
        path="/auth",
        httponly=True,
        secure=settings.ENVIRONMENT == "production",
        samesite="lax",
    )
    response.headers["Cache-Control"] = "no-store"


async def mark_family_revoked(family_id: str) -> None:
    """Publish a family-revocation marker for the maximum refresh-token lifetime."""
    await get_redis_utils().client.setex(
        f"family_revoked:{family_id}",
        _refresh_ttl_seconds(),
        "1",
    )


async def blacklist_access_token(raw_token: str | None) -> None:
    """Blacklist a still-valid access token until its natural JWT expiry."""
    if not raw_token:
        return
    try:
        payload = decode_token(raw_token)
    except Exception:
        return
    if payload.get("type") != "access" or not payload.get("jti"):
        return

    exp = payload.get("exp")
    try:
        exp_seconds = int(exp)
    except (TypeError, ValueError):
        return
    ttl = max(1, exp_seconds - int(datetime.now(timezone.utc).timestamp()))
    await get_redis_utils().client.setex(f"blacklist:{payload['jti']}", ttl, "1")