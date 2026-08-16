import pytest
from starlette.responses import Response

from app.core.browser_session import (
    clear_auth_cookies,
    clear_signup_poll_cookie,
    set_auth_cookies,
    set_signup_poll_cookie,
)
from app.core.config import _validated_public_api_path_prefix, settings
from app.schemas.auth import TokenResponse
from app.utils.email_utils import _verification_url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ""),
        ("/", ""),
        ("/api", "/api"),
        ("/api/", "/api"),
        ("/api/v1", "/api/v1"),
    ],
)
def test_public_api_path_prefix_normalizes_safe_paths(raw: str, expected: str) -> None:
    assert _validated_public_api_path_prefix(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "api",
        "//api",
        "/api//v1",
        "/api?x=1",
        "/api#fragment",
        "/api\\v1",
        "/api/%2e%2e",
        "/api/..",
        "/./api",
        "/api path",
        "/api\tv1",
    ],
)
def test_public_api_path_prefix_rejects_ambiguous_or_unsafe_paths(raw: str) -> None:
    with pytest.raises(ValueError):
        _validated_public_api_path_prefix(raw)


def test_public_api_prefix_controls_verification_and_narrow_cookie_paths(monkeypatch) -> None:
    monkeypatch.setattr(settings, "BACKEND_BASE_URL", "https://app.example.invalid/")
    monkeypatch.setattr(settings, "PUBLIC_API_PATH_PREFIX", "/api/")

    assert settings.public_api_base_url == "https://app.example.invalid/api"
    assert (
        _verification_url("verification-secret")
        == "https://app.example.invalid/api/auth/verify?token=verification-secret"
    )

    response = Response()
    set_auth_cookies(
        response,
        TokenResponse(
            access_token="access-secret",
            refresh_token="refresh-secret",
            onboarding_completed=False,
        ),
    )
    set_signup_poll_cookie(response, "signup-secret")

    cookies = response.headers.getlist("set-cookie")
    assert any(
        "access_token=access-secret" in value and "Path=/" in value
        for value in cookies
    )
    assert any(
        "refresh_token=refresh-secret" in value and "Path=/api/auth" in value
        for value in cookies
    )
    assert any(
        "signup_poll_token=signup-secret" in value and "Path=/api/auth" in value
        for value in cookies
    )

    cleared = Response()
    clear_auth_cookies(cleared)
    clear_signup_poll_cookie(cleared)
    cleared_cookies = cleared.headers.getlist("set-cookie")
    assert any(
        "refresh_token=" in value
        and "Path=/api/auth" in value
        and "Max-Age=0" in value
        for value in cleared_cookies
    )
    assert any(
        "signup_poll_token=" in value
        and "Path=/api/auth" in value
        and "Max-Age=0" in value
        for value in cleared_cookies
    )


def test_browser_cookie_lifetimes_follow_validated_jwt_settings(monkeypatch) -> None:
    """CERT-B must be able to shorten JWT lifetime without creating cookie/JWT drift."""
    monkeypatch.setattr(settings, "PUBLIC_API_PATH_PREFIX", "")
    monkeypatch.setattr(settings, "ACCESS_TOKEN_EXPIRE_MINUTES", 2)
    monkeypatch.setattr(settings, "REFRESH_TOKEN_EXPIRE_DAYS", 3)

    response = Response()
    set_auth_cookies(
        response,
        TokenResponse(
            access_token="access-secret",
            refresh_token="refresh-secret",
            onboarding_completed=False,
        ),
    )

    cookies = response.headers.getlist("set-cookie")
    assert any(
        "access_token=access-secret" in value and "Max-Age=120" in value
        for value in cookies
    )
    assert any(
        "refresh_token=refresh-secret" in value
        and "Max-Age=259200" in value
        and "Path=/auth" in value
        for value in cookies
    )
