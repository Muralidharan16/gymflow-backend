from starlette.responses import Response

from app.core.browser_session import set_auth_cookies
from app.core.config import settings
from app.schemas.auth import TokenResponse


def test_browser_cookie_lifetimes_follow_validated_jwt_settings(monkeypatch) -> None:
    """CERT-B must be able to shorten JWT lifetime without creating cookie/JWT drift."""
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
        "refresh_token=refresh-secret" in value and "Max-Age=259200" in value
        for value in cookies
    )
