from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATABASE = ROOT / "app/core/database.py"


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