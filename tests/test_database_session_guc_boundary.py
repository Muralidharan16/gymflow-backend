from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATABASE = ROOT / "app/core/database.py"
TESTS = ROOT / "tests"
TEST_DB_CLEANUP = TESTS / "db_cleanup.py"
BRANCH_MANAGEMENT = TESTS / "test_branch_management.py"
BRANCH_READ_MATRIX = TESTS / "test_branch_read_rls_matrix.py"
THIS_TEST = Path(__file__).resolve()


def _source() -> str:
    return DATABASE.read_text(encoding="utf-8")


def test_request_session_does_not_set_privileged_deadlock_timeout() -> None:
    source = _source()

    assert "SET LOCAL deadlock_timeout" not in source
    assert "SET deadlock_timeout" not in source
    assert "_DEADLOCK_MS" not in source
    assert "administrator-owned PostgreSQL configuration boundary" in source


def test_request_session_keeps_only_user_settable_timeout_guards() -> None:
    source = _source()

    for setting in (
        "statement_timeout",
        "lock_timeout",
        "idle_in_transaction_session_timeout",
    ):
        assert setting in source

    # Timeouts now use the same parameterized transaction-local set_config path
    # as tenant/audit context rather than embedding SET LOCAL SQL strings.
    assert "pg_catalog.set_config(:setting_name, :setting_value, true)" in source
    assert "set_config(:setting_name, :setting_value, false)" not in source


def test_request_context_gucs_reapply_on_every_transaction_begin() -> None:
    source = _source()

    for setting in (
        "app.current_user",
        "app.current_user_id",
        "app.current_principal_type",
        "app.current_org_id",
        "app.current_gym_id",
        "app.current_role",
    ):
        assert setting in source

    assert '@event.listens_for(Session, "after_begin")' in source
    assert "session.info.get(_SESSION_CONTEXT_KEY)" in source
    assert "_apply_context_sync(connection, context)" in source
    assert "pg_catalog.set_config(:setting_name, :setting_value, true)" in source


def test_initializer_does_not_end_context_before_request_work_begins() -> None:
    source = _source()
    start = source.index("class SessionContextInitializer:")
    end = source.index("async def initialize_request_session", start)
    initializer = source[start:end]

    # Context initialization must only attach verified identity/tenant state to
    # Session.info. It must not open/commit a transaction whose SET LOCAL state
    # disappears before the request performs protected work.
    assert "async with session.begin():" not in initializer
    assert "session.info[_SESSION_CONTEXT_KEY]" in initializer
    assert "commit(" not in initializer
    assert "rollback(" not in initializer


def test_obsolete_forced_rls_cleanup_privilege_bridge_is_absent() -> None:
    """Fixture cleanup must not manufacture a database capability production lacks."""
    assert not TEST_DB_CLEANUP.exists(), (
        "tests/db_cleanup.py must not reintroduce the obsolete branch-state "
        "hard-delete privilege bridge"
    )

    forbidden_tokens = (
        "GRANT DELETE ON TABLE public.org_branch_state TO test_runner",
        "GRANT SELECT (org_id) ON TABLE public.org_branch_state TO test_runner",
        "CREATE POLICY pytest_org_branch_state_cleanup_select",
        "CREATE POLICY pytest_org_branch_state_cleanup_delete",
        "SET LOCAL ROLE test_runner",
        "DELETE FROM public.org_branch_state",
    )
    offenders: list[str] = []

    for path in sorted(TESTS.glob("*.py")):
        if path.resolve() == THIS_TEST:
            continue
        source = path.read_text(encoding="utf-8")
        if any(token in source for token in forbidden_tokens):
            offenders.append(path.name)

    assert offenders == [], (
        "branch-state fixture teardown must use isolated disposable test data, "
        f"not a test-only privilege/RLS bridge; offenders={offenders}"
    )


def test_no_test_imports_or_calls_obsolete_branch_state_cleanup_helper() -> None:
    offenders: list[str] = []

    for path in sorted(TESTS.glob("*.py")):
        if path.resolve() == THIS_TEST:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "db_cleanup":
                offenders.append(path.name)
                break
            if isinstance(node, ast.Import) and any(
                alias.name == "db_cleanup" for alias in node.names
            ):
                offenders.append(path.name)
                break
            if (
                isinstance(node, ast.Name)
                and node.id == "delete_org_branch_state_fixture"
            ):
                offenders.append(path.name)
                break

    assert offenders == [], (
        "tests must not depend on the removed branch-state privilege bridge; "
        f"offenders={offenders}"
    )


def _assert_disposable_branch_fixture(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    normalized = " ".join(source.split())

    assert "delete_org_branch_state_fixture" not in source
    assert "DELETE FROM public.org_branch_state" not in source
    assert "DELETE FROM public.org_branches" not in source
    assert "DELETE FROM public.gym_owners" not in source
    assert "DELETE FROM public.organizations" not in source
    assert "TRUNCATE" not in source
    assert "CASCADE" not in source

    for token in (
        "UUID-scoped",
        "disposable CI database",
        "test-only grants",
        "temporary RLS policies",
        "hidden cascades",
    ):
        assert token in normalized


def test_branch_management_uses_isolated_disposable_fixture_roots() -> None:
    source = BRANCH_MANAGEMENT.read_text(encoding="utf-8")
    assert "async def cleanup_branch_management_fixture" not in source
    _assert_disposable_branch_fixture(BRANCH_MANAGEMENT)


def test_branch_read_matrix_uses_isolated_disposable_fixture_roots() -> None:
    _assert_disposable_branch_fixture(BRANCH_READ_MATRIX)
