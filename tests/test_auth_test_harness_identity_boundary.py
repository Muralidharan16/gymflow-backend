from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AUTH_TEST = ROOT / "tests/test_auth_register.py"
AUTH_ROUTER = ROOT / "app/routers/auth.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_auth_registration_fixture_cleanup_is_admin_only_and_symmetric():
    source = _source(AUTH_TEST)
    assert "from app.core.database import AsyncSessionLocal" not in source
    assert "from conftest import AdminTestSessionLocal, cleanup_test_database_tables" in source
    assert "await cleanup_test_database_tables(_AUTH_TEST_TABLES)" in source

    module = ast.parse(source, filename=str(AUTH_TEST))
    fixture = next(
        node
        for node in module.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "cleanup_database_and_redis"
    )
    fixture_source = ast.unparse(fixture)
    assert fixture_source.count("await _clear_auth_test_data()") == 2
    assert "AsyncSessionLocal" not in fixture_source


def test_auth_registration_database_evidence_never_uses_general_runtime():
    source = _source(AUTH_TEST)
    assert "AsyncSessionLocal" not in source
    assert source.count("async with AdminTestSessionLocal() as session:") == 2
    assert "select(Owner)" in source
    assert "select(Organization)" in source


def test_auth_routes_keep_dedicated_auth_database_dependency():
    source = _source(AUTH_ROUTER)
    assert "from app.core.auth_database import get_auth_db" in source
    assert '@router.post("/signup")' in source
    assert '@router.get("/verify")' in source
    assert source.count("Depends(get_auth_db)") >= 2
