from __future__ import annotations

import asyncio
import inspect
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from jose import jwt
from starlette.requests import Request
from starlette.responses import Response

from app.core.browser_session import (
    clear_auth_cookies,
    clear_signup_poll_cookie,
    require_trusted_browser_origin,
    set_auth_cookies,
    set_signup_poll_cookie,
)
from app.core.config import settings
from app.core.security import create_access_token, create_refresh_token, verify_token
from app.schemas.auth import SignupStatusRequest, TokenResponse
from app.services.auth_service import AuthService


def _request(method: str, origin: str | None) -> Request:
    headers = []
    if origin is not None:
        headers.append((b"origin", origin.encode("ascii")))
    return Request({
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": "/auth/refresh",
        "raw_path": b"/auth/refresh",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("api.example.invalid", 443),
    })


def test_owner_token_pair_is_bound_to_one_session_family() -> None:
    family_id = str(uuid.uuid4())
    owner_id = str(uuid.uuid4())
    org_id = str(uuid.uuid4())
    access = create_access_token(owner_id, org_id, "owner@example.invalid", family_id=family_id)
    refresh = create_refresh_token(owner_id, family_id=family_id)
    access_payload = verify_token(access, "access")
    refresh_payload = verify_token(refresh, "refresh")
    assert access_payload["f_id"] == family_id
    assert refresh_payload["f_id"] == family_id
    assert access_payload["sub"] == refresh_payload["sub"] == owner_id


def test_refresh_service_verifies_type_before_locked_database_rotation() -> None:
    source = inspect.getsource(AuthService.refresh_token)
    assert source.index('verify_token(raw_refresh_token, "refresh")') < source.index("select(AuthSession)")
    assert ".with_for_update()" in source
    assert "db_session.revoked_at is not None" in source
    assert "Session compromised. Please login again." in source
    assert "subject_id = uuid.UUID" in source
    assert "claimed_family_id" in source


def test_refresh_rejects_malformed_uuid_claims_before_database_lookup() -> None:
    class NoDatabaseSession:
        async def execute(self, *_args, **_kwargs):
            raise AssertionError("database lookup must not run for malformed signed claims")

    token = jwt.encode(
        {
            "sub": "not-a-uuid",
            "type": "refresh",
            "jti": str(uuid.uuid4()),
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        },
        settings.SECRET_KEY,
        algorithm="HS256",
    )
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(AuthService(NoDatabaseSession()).refresh_token(token))
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid refresh token claims"


def test_browser_cookie_contract_is_http_only_scoped_and_token_free() -> None:
    response = Response()
    set_auth_cookies(response, TokenResponse(access_token="access-secret", refresh_token="refresh-secret", onboarding_completed=False))
    cookies = response.headers.getlist("set-cookie")
    assert any("access_token=access-secret" in value and "HttpOnly" in value and "Path=/" in value for value in cookies)
    assert any("refresh_token=refresh-secret" in value and "HttpOnly" in value and "Path=/auth" in value for value in cookies)
    assert response.headers["cache-control"] == "no-store"

    clear_auth_cookies(response)
    expired = response.headers.getlist("set-cookie")
    assert any("access_token=" in value and "Max-Age=0" in value and "Path=/" in value for value in expired)
    assert any("refresh_token=" in value and "Max-Age=0" in value and "Path=/auth" in value for value in expired)


def test_signup_poll_capability_is_http_only_and_not_part_of_request_schema() -> None:
    response = Response()
    set_signup_poll_cookie(response, "poll-secret")
    cookies = response.headers.getlist("set-cookie")
    assert any(
        "signup_poll_token=poll-secret" in value
        and "HttpOnly" in value
        and "Path=/auth" in value
        and "Max-Age=600" in value
        for value in cookies
    )
    assert set(SignupStatusRequest.model_fields) == {"email"}

    clear_signup_poll_cookie(response)
    expired = response.headers.getlist("set-cookie")
    assert any("signup_poll_token=" in value and "Max-Age=0" in value and "Path=/auth" in value for value in expired)


def test_production_cookie_mutations_require_exact_trusted_origin() -> None:
    require_trusted_browser_origin(_request("POST", "https://app.example.invalid"))
    with pytest.raises(HTTPException) as missing:
        require_trusted_browser_origin(_request("POST", None))
    assert missing.value.status_code == 403
    with pytest.raises(HTTPException) as hostile:
        require_trusted_browser_origin(_request("POST", "https://evil.example.invalid"))
    assert hostile.value.status_code == 403
    require_trusted_browser_origin(_request("GET", None))


def test_auth_router_does_not_serialize_browser_credentials() -> None:
    from app.routers import auth

    source = inspect.getsource(auth)
    assert '@router.post("/signup-status")' in source
    assert '@router.post("/logout")' in source
    assert "await request.json()" not in source
    assert '"access_token": tokens.access_token' not in source
    assert '"refresh_token": tokens.refresh_token' not in source
    assert '"access_token": sync_data' not in source
    assert '"refresh_token": sync_data' not in source
    assert 'result.pop("signup_poll_token", None)' in source
    assert 'request.cookies.get("signup_poll_token")' in source
    assert 'request.cookies.get("refresh_token")' in source
    assert 'owner_id = uuid.UUID(str(sync_data.get("sub")))' in source


def test_tenant_boundary_requires_access_type_and_durable_owner_family() -> None:
    from app.core import middleware

    source = inspect.getsource(middleware.TenantMiddleware)
    exemptions = middleware.EXEMPT_PATHS
    assert "/auth/logout" in exemptions
    assert "/onboarding/status" not in exemptions
    assert "/onboarding/complete" not in exemptions
    assert 'verify_token(token, "access")' in source
    assert "uuid.UUID(str(user_id))" in source
    assert "uuid.UUID(str(org_id))" in source
    assert "AuthSessionFamily.id == family_uuid" in source
    assert 'principal_type == "owner" and family_uuid is None' in source
    assert "family.revoked_at is not None" in source
    assert "Session validation unavailable." in source
    assert "require_trusted_browser_origin(request)" in source


def test_onboarding_uses_centrally_verified_request_principal() -> None:
    from app.routers import onboarding

    source = inspect.getsource(onboarding)
    assert "decode_token" not in source
    assert "OAuth2PasswordBearer" not in source
    assert 'request.state, "principal_type"' in source
    assert '!= "owner"' in source
