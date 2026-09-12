from __future__ import annotations

from pathlib import Path


MIDDLEWARE = Path("app/core/middleware.py")
SECURITY = Path("app/core/security.py")
DATABASE = Path("app/core/database.py")


def test_access_tokens_are_typed_and_middleware_propagates_the_claim() -> None:
    security = SECURITY.read_text(encoding="utf-8")
    middleware = MIDDLEWARE.read_text(encoding="utf-8")

    assert '"principal_type": normalized_principal_type' in security
    assert "ACCESS_TOKEN_PRINCIPAL_TYPES" in security
    assert "principal_type = payload.get(\"principal_type\")" in middleware
    assert "principal_type not in ACCESS_TOKEN_PRINCIPAL_TYPES" in middleware
    assert "request.state.principal_type = principal_type" in middleware


def test_tenant_middleware_rejects_missing_or_unsupported_principal_type() -> None:
    middleware = MIDDLEWARE.read_text(encoding="utf-8")

    assert 'content={"detail": "Invalid principal type."}' in middleware
    assert "if principal_type not in ACCESS_TOKEN_PRINCIPAL_TYPES:" in middleware
    assert middleware.index("principal_type = payload.get") < middleware.index(
        "request.state.principal_type = principal_type"
    )


def test_database_initializer_consumes_request_principal_type() -> None:
    database = DATABASE.read_text(encoding="utf-8")

    assert 'principal_type = getattr(state, "principal_type", None)' in database
    assert "principal_type=str(principal_type) if principal_type else None" in database
    assert "_ALLOWED_PRINCIPAL_TYPES" in database
