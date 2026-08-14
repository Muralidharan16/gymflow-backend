from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATABASE = ROOT / "app/core/database.py"
REPOSITORY = ROOT / "app/repositories/organization_profile.py"
ROUTER = ROOT / "app/routers/organizations.py"
SCHEMA = ROOT / "app/schemas/organization.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_get_db_requires_fastapi_request_and_installs_verified_context() -> None:
    source = _source(DATABASE)
    tree = ast.parse(source, filename=str(DATABASE))
    node = next(
        item
        for item in tree.body
        if isinstance(item, ast.AsyncFunctionDef) and item.name == "get_db"
    )

    assert len(node.args.args) == 1
    request_arg = node.args.args[0]
    assert request_arg.arg == "request"
    assert isinstance(request_arg.annotation, ast.Name)
    assert request_arg.annotation.id == "Request"
    assert node.args.defaults == []
    assert node.end_lineno is not None

    function_source = "".join(
        source.splitlines(keepends=True)[node.lineno - 1 : node.end_lineno]
    )
    assert "await initialize_request_session(session, request)" in function_source
    assert "request=None" not in function_source


def test_profile_repository_maps_only_postgres_authorization_denial() -> None:
    source = _source(REPOSITORY)

    assert "class ProfileAuthorizationError(PermissionError)" in source
    assert "except DBAPIError as exc" in source
    assert 'if _sqlstate(exc) == "42501"' in source
    assert "raise ProfileAuthorizationError(" in source
    assert "raise\n" in source


def test_router_sanitizes_profile_authorization_failure_as_403() -> None:
    source = _source(ROUTER)

    assert "ProfileAuthorizationError" in source
    assert source.count("except ProfileAuthorizationError as exc:") == 2
    assert source.count("status_code=status.HTTP_403_FORBIDDEN") >= 2
    assert source.count('detail="Organization profile access denied"') >= 2
    assert "str(exc)" not in source


def test_profile_request_rejects_unknown_control_plane_fields() -> None:
    source = _source(SCHEMA)
    tree = ast.parse(source, filename=str(SCHEMA))
    model = next(
        item
        for item in tree.body
        if isinstance(item, ast.ClassDef) and item.name == "OrganizationUpdate"
    )
    model_source = "".join(
        source.splitlines(keepends=True)[model.lineno - 1 : model.end_lineno]
    )

    assert 'model_config = ConfigDict(extra="forbid")' in model_source
    for protected in ("tier", "is_active", "max_branches", "verification_status"):
        assert f"{protected}:" not in model_source
