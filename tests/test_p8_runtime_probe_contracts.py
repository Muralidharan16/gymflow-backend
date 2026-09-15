from __future__ import annotations

from pathlib import Path

from app.observability.runtime_probes import _is_database_disconnect_exception


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "app/observability/runtime_probes.py").read_text(encoding="utf-8")


class _SqlStateFailure(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


class _PgCodeFailure(Exception):
    def __init__(self, pgcode: str) -> None:
        super().__init__(pgcode)
        self.pgcode = pgcode


def test_direct_asyncpg_cannot_connect_now_is_a_disconnect() -> None:
    # asyncpg CannotConnectNowError uses PostgreSQL SQLSTATE 57P03 and can
    # escape directly while SQLAlchemy is establishing a physical connection.
    assert _is_database_disconnect_exception(_SqlStateFailure("57P03")) is True


def test_postgresql_connection_exception_class_is_a_disconnect() -> None:
    for state in ("08000", "08001", "08003", "08004", "08006", "08007", "08P01"):
        assert _is_database_disconnect_exception(_SqlStateFailure(state)) is True


def test_postgresql_shutdown_states_are_disconnect_evidence() -> None:
    for state in ("57P01", "57P02", "57P03"):
        assert _is_database_disconnect_exception(_PgCodeFailure(state)) is True


def test_non_connectivity_postgresql_and_unknown_errors_are_not_mislabeled() -> None:
    assert _is_database_disconnect_exception(_SqlStateFailure("23505")) is False
    assert _is_database_disconnect_exception(_SqlStateFailure("40001")) is False
    assert _is_database_disconnect_exception(ValueError("programming defect")) is False


def test_probe_records_classified_direct_driver_failure_without_changing_authority() -> None:
    assert "if _is_database_disconnect_exception(exc):" in SOURCE
    assert 'metrics.database_disconnect(pool="api")' in SOURCE
    assert "Unknown probe failures are evidence only" in SOURCE
    assert "await async_engine.connect()" in SOURCE
