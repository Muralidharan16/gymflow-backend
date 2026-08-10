from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AUTH_TEST = ROOT / "tests/test_auth_register.py"
AUTH_ROUTER = ROOT / "app/routers/auth.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _async_function(module: ast.Module, name: str) -> ast.AsyncFunctionDef:
    return next(
        node
        for node in module.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name
    )


def test_auth_registration_fixture_cleanup_is_admin_only_row_scoped_and_symmetric():
    source = _source(AUTH_TEST)
    assert "from app.core.database import AsyncSessionLocal" not in source
    assert "from conftest import AdminTestSessionLocal" in source
    assert "cleanup_test_database_tables" not in source
    assert "TRUNCATE" not in source.upper()
    assert "CASCADE" not in source.upper()

    module = ast.parse(source, filename=str(AUTH_TEST))
    cleanup = _async_function(module, "_clear_auth_test_data")
    cleanup_source = ast.unparse(cleanup)
    assert "async with AdminTestSessionLocal() as session" in cleanup_source
    assert "Owner.email.in_(_AUTH_TEST_EMAILS)" in cleanup_source
    assert "Organization.name.in_(_AUTH_TEST_ORG_NAMES)" in cleanup_source

    ordered_deletes = (
        "delete(AuthSession)",
        "delete(AuthSessionFamily)",
        "delete(RefreshToken)",
        "delete(Gym)",
        "delete(Owner)",
        "delete(Organization)",
    )
    positions = [cleanup_source.index(fragment) for fragment in ordered_deletes]
    assert positions == sorted(positions)
    assert cleanup_source.count("await session.commit()") == 1

    fixture = _async_function(module, "cleanup_database_and_redis")
    fixture_source = ast.unparse(fixture)
    assert fixture_source.count("await _clear_auth_test_data()") == 2
    assert "AsyncSessionLocal" not in fixture_source


def test_auth_registration_database_evidence_never_uses_general_runtime():
    source = _source(AUTH_TEST)
    assert "AsyncSessionLocal" not in source

    # One admin session is the bounded row-scoped cleaner; the other two are
    # explicit database evidence reads in verification tests.
    assert source.count("async with AdminTestSessionLocal() as session:") == 3
    assert "select(Owner)" in source
    assert "select(Organization)" in source


def test_auth_cleanup_identity_set_covers_all_module_test_accounts():
    source = _source(AUTH_TEST)
    module = ast.parse(source, filename=str(AUTH_TEST))

    string_values = {
        node.value
        for node in ast.walk(module)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    literal_test_emails = {
        value.lower()
        for value in string_values
        if value.lower().endswith("@example.com")
    }

    # Dynamic rate{i}@example.com identities are explicitly represented by the
    # range expression in the cleanup set; all literal identities must be listed.
    cleanup_assignment = next(
        node
        for node in module.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "_AUTH_TEST_EMAILS"
            for target in node.targets
        )
    )
    cleanup_source = ast.unparse(cleanup_assignment)
    for email in literal_test_emails:
        assert repr(email) in cleanup_source
    assert "rate" in cleanup_source and "range(6)" in cleanup_source


def test_auth_routes_keep_dedicated_auth_database_dependency():
    source = _source(AUTH_ROUTER)
    assert "from app.core.auth_database import get_auth_db" in source
    assert '@router.post("/signup")' in source
    assert '@router.get("/verify")' in source
    assert source.count("Depends(get_auth_db)") >= 2
