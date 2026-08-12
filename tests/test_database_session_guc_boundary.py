from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATABASE = ROOT / "app/core/database.py"
TEST_DB_CLEANUP = ROOT / "tests/db_cleanup.py"
BRANCH_MANAGEMENT = ROOT / "tests/test_branch_management.py"


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


def test_pytest_forced_rls_cleanup_is_transactional_and_test_runner_only() -> None:
    source = TEST_DB_CLEANUP.read_text(encoding="utf-8")

    required = (
        '"test" not in str(identity["database_name"]).lower()',
        'identity["session_name"] != "migration_owner"',
        'identity["migration_bypassrls"]',
        'identity["runner_bypassrls"]',
        "pg_catalog.pg_has_role(",
        '"app_runtime"',
        '"auth_runtime"',
        '"worker_runtime"',
        '"lifecycle_maintenance_runtime"',
        'trace_id="pytest-forced-rls-cleanup"',
        'role="superadmin"',
        "GRANT DELETE ON TABLE public.org_branch_state TO test_runner",
        "GRANT SELECT (org_id) ON TABLE public.org_branch_state TO test_runner",
        "CREATE POLICY pytest_org_branch_state_cleanup",
        "FOR DELETE TO test_runner",
        "current_setting('app.current_org_id', true)",
        "current_setting('app.current_role', true) = 'superadmin'",
        "SET LOCAL ROLE test_runner",
        "DELETE FROM public.org_branch_state WHERE org_id = :org_id",
        "RESET ROLE",
        "DROP POLICY pytest_org_branch_state_cleanup ON public.org_branch_state",
        "REVOKE SELECT (org_id) ON TABLE public.org_branch_state FROM test_runner",
        "REVOKE DELETE ON TABLE public.org_branch_state FROM test_runner",
        '"pytest cleanup capability was not fully removed before commit"',
    )
    for token in required:
        assert token in source

    grant_delete = source.index(
        "GRANT DELETE ON TABLE public.org_branch_state TO test_runner"
    )
    create_policy = source.index("CREATE POLICY pytest_org_branch_state_cleanup")
    set_runner = source.index("SET LOCAL ROLE test_runner")
    delete_row = source.index("DELETE FROM public.org_branch_state WHERE org_id = :org_id")
    reset_role = source.index("RESET ROLE", set_runner)
    drop_policy = source.index(
        "DROP POLICY pytest_org_branch_state_cleanup ON public.org_branch_state"
    )
    revoke_delete = source.index(
        "REVOKE DELETE ON TABLE public.org_branch_state FROM test_runner"
    )

    assert grant_delete < create_policy < set_runner < delete_row
    assert delete_row < reset_role < drop_policy < revoke_delete

    for forbidden in (
        "SET LOCAL ROLE app_runtime",
        "SET ROLE app_runtime",
        "SET ROLE app_rls_executor",
        "SET ROLE app_security_owner",
        "GRANT DELETE ON TABLE public.org_branch_state TO app_runtime",
        "GRANT DELETE ON TABLE public.org_branches",
        "GRANT DELETE ON TABLE public.organizations",
        "GRANT DELETE ON TABLE public.gym_owners",
        "DISABLE ROW LEVEL SECURITY",
        "row_security = off",
        "TRUNCATE",
        "CASCADE",
    ):
        assert forbidden not in source


def test_branch_management_routes_forced_rls_child_cleanup_through_helper() -> None:
    source = BRANCH_MANAGEMENT.read_text(encoding="utf-8")
    cleanup_start = source.index("async def cleanup_branch_management_fixture")
    cleanup_end = source.index("@pytest_asyncio.fixture", cleanup_start)
    cleanup = source[cleanup_start:cleanup_end]

    assert "delete_org_branch_state_fixture(" in cleanup
    assert "DELETE FROM public.org_branch_state" not in cleanup
    assert "TRUNCATE" not in cleanup

    child_cleanup = cleanup.index("delete_org_branch_state_fixture(")
    parent_cleanup = cleanup.index("DELETE FROM public.org_branches")
    owner_cleanup = cleanup.index("DELETE FROM public.gym_owners")
    tenant_cleanup = cleanup.index("DELETE FROM public.organizations")
    assert child_cleanup < parent_cleanup < owner_cleanup < tenant_cleanup
