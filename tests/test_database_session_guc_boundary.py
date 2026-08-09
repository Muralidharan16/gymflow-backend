from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATABASE = ROOT / "app/core/database.py"


def _source() -> str:
    return DATABASE.read_text(encoding="utf-8")


def test_request_session_does_not_set_privileged_deadlock_timeout() -> None:
    source = _source()

    assert "SET LOCAL deadlock_timeout" not in source
    assert "_DEADLOCK_MS" not in source
    assert "administrator-owned PostgreSQL configuration boundary" in source


def test_request_session_keeps_only_user_settable_timeout_guards() -> None:
    source = _source()

    for statement in (
        "SET LOCAL statement_timeout",
        "SET LOCAL lock_timeout",
        "SET LOCAL idle_in_transaction_session_timeout",
    ):
        assert statement in source

    assert source.count("SET LOCAL statement_timeout") == 1
    assert source.count("SET LOCAL lock_timeout") == 1
    assert source.count("SET LOCAL idle_in_transaction_session_timeout") == 1


def test_request_context_gucs_remain_transaction_local() -> None:
    source = _source()

    for setting in (
        "app.current_user",
        "app.current_user_id",
        "app.current_org_id",
        "app.current_gym_id",
        "app.current_role",
    ):
        assert setting in source

    assert "async with session.begin():" in source
    assert "pg_catalog.set_config" in source
