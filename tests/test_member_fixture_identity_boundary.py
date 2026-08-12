from __future__ import annotations

import ast
from pathlib import Path


TARGET = Path(__file__).with_name("test_members.py")


def _function(name: str) -> ast.AST:
    tree = ast.parse(TARGET.read_text())
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    ]
    assert len(matches) == 1, f"expected exactly one function named {name!r}"
    return matches[0]


def _source(name: str) -> str:
    source = TARGET.read_text()
    segment = ast.get_source_segment(source, _function(name))
    assert segment is not None
    return segment


def test_member_fixture_is_isolated_and_non_destructive() -> None:
    source = TARGET.read_text()
    fixture = _function("test_data")
    fixture_source = _source("test_data")

    assert "cleanup_test_database_tables" not in source
    assert "TRUNCATE" not in source.upper()
    assert "CASCADE" not in source.upper()
    assert "AsyncSessionLocal" not in source
    assert "RESET ROLE" not in source.upper()

    args = {arg.arg for arg in fixture.args.args}
    assert {"auth_db_session", "admin_db_session"} <= args
    assert "suffix = uuid.uuid4().hex" in fixture_source


def test_member_tenant_bootstrap_is_auth_owned_and_fk_ordered() -> None:
    fixture_source = _source("test_data")

    organization_position = fixture_source.find("Organization(")
    flush_position = fixture_source.find("await auth_db_session.flush()")
    owner_position = fixture_source.find("Owner(")
    commit_position = fixture_source.find("await auth_db_session.commit()")

    assert min(organization_position, flush_position, owner_position, commit_position) >= 0
    assert organization_position < flush_position < owner_position < commit_position
    assert fixture_source.count("_set_owner_context(") >= 2
    assert "auth_db_session.add_all" in fixture_source
    assert "auth_db_session.add(" in fixture_source


def test_member_admin_fixture_scope_is_legacy_gym_seed_only() -> None:
    fixture_source = _source("test_data")

    assert fixture_source.count("admin_db_session.add(") == 1
    admin_seed = fixture_source.split("admin_db_session.add(", 1)[1]
    assert admin_seed.lstrip().startswith("Gym(")
    assert "await admin_db_session.commit()" in admin_seed

    before_admin = fixture_source.split("admin_db_session.add(", 1)[0]
    assert "admin_db_session" not in before_admin


def test_direct_member_domain_seeds_use_runtime_context() -> None:
    for name in (
        "test_duplicate_member_number_same_org_is_rejected",
        "test_member_search_active_subscription_projection",
    ):
        node = _function(name)
        function_source = _source(name)
        args = {arg.arg for arg in node.args.args}

        assert "db_session" in args
        context_position = function_source.find("await _set_owner_context(")
        first_add_position = function_source.find("db_session.add")
        assert context_position >= 0
        assert first_add_position >= 0
        assert context_position < first_add_position
