from __future__ import annotations

import ast
from pathlib import Path


TARGET = Path(__file__).with_name("test_member_subscriptions_v2.py")


def _function(name: str) -> ast.AST:
    tree = ast.parse(TARGET.read_text())
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    ]
    assert len(matches) == 1, f"expected exactly one function named {name!r}"
    return matches[0]


def test_subscription_fixture_is_identity_split_and_non_destructive() -> None:
    source = TARGET.read_text()
    node = _function("test_data")
    fixture_source = ast.get_source_segment(source, node)
    assert fixture_source is not None

    assert "cleanup_test_database_tables" not in source
    assert "TRUNCATE" not in source.upper()
    assert "CASCADE" not in source.upper()

    args = {arg.arg for arg in node.args.args}
    assert {"admin_db_session", "auth_db_session", "db_session"} <= args
    assert "admin_db_session.add_all" in fixture_source
    assert "auth_db_session.add_all" in fixture_source
    assert "db_session.add_all" in fixture_source
    assert fixture_source.count("_set_owner_context(") >= 4
    assert "suffix = uuid.uuid4().hex" in fixture_source


def test_subscription_request_headers_use_fixture_owner_email() -> None:
    source = TARGET.read_text()
    create_node = _function("create_subscription")
    create_source = ast.get_source_segment(source, create_node)
    assert create_source is not None

    assert 'test_data["owner1_email"]' in create_source
    assert '"owner1@test.com"' not in create_source


def test_concurrent_member_seed_reinstalls_tenant_context() -> None:
    source = TARGET.read_text()
    node = _function("test_concurrent_subscription_code_generation_is_unique_and_sequential")
    function_source = ast.get_source_segment(source, node)
    assert function_source is not None

    assert "async with AsyncSessionLocal() as session" in function_source
    assert "await _set_owner_context(" in function_source
    assert 'org_id=test_data["org1_id"]' in function_source
