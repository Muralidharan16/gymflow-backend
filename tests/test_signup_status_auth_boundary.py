from __future__ import annotations

import inspect

from app.core.middleware import EXEMPT_PATHS, _is_exempt
from app.routers.auth import get_signup_status


def test_signup_status_is_pre_auth_but_capability_protected() -> None:
    assert "/auth/signup-status" in EXEMPT_PATHS
    assert _is_exempt("/auth/signup-status") is True

    signature = inspect.signature(get_signup_status)
    poll_token = signature.parameters["poll_token"]

    # Missing capability tokens are structural request errors (FastAPI 422),
    # while supplied-but-invalid tokens are rejected by the handler with 403.
    assert poll_token.default is inspect.Parameter.empty
    assert poll_token.annotation in (str, "str")


def test_authenticated_auth_endpoints_are_not_accidentally_exempted() -> None:
    assert _is_exempt("/auth/me") is False
