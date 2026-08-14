from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.core.runtime_principal_attestation import validate_runtime_url_configuration


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "app/core/config.py"
DATABASE = ROOT / "app/core/database.py"


def test_production_rejects_worker_login_reuse_semantically() -> None:
    source = CONFIG.read_text(encoding="utf-8")
    assert "validate_runtime_url_configuration" in source

    parsed = [
        SimpleNamespace(drivername="postgresql+asyncpg", username="api_login", database="prod"),
        SimpleNamespace(drivername="postgresql+asyncpg", username="auth_login", database="prod"),
        SimpleNamespace(drivername="postgresql+asyncpg", username="api_login", database="prod"),
        SimpleNamespace(drivername="postgresql+asyncpg", username="maintenance_login", database="prod"),
    ]
    with patch(
        "app.core.runtime_principal_attestation.make_url",
        side_effect=parsed,
    ):
        violations = validate_runtime_url_configuration({
            "api": "api",
            "auth": "auth",
            "worker": "worker",
            "maintenance": "maintenance",
        })

    assert "runtime.config.login_reuse" in {item.code for item in violations}


def test_worker_asyncpg_connections_are_not_reused_across_celery_event_loops() -> None:
    source = DATABASE.read_text(encoding="utf-8")

    assert "worker_async_engine = create_async_engine" in source
    assert "settings.worker_database_url" in source
    assert "poolclass=NullPool" in source
    assert "WorkerAsyncSessionLocal" in source
    assert '"app.worker_id"' in source
    assert '"app.internal_maintenance"' in source


def test_background_sql_timeout_application_contract_is_explicit_and_wired() -> None:
    source = DATABASE.read_text(encoding="utf-8")

    assert "_BACKGROUND_STMT_TIMEOUT_MS = 15000" in source
    assert "_BACKGROUND_LOCK_TIMEOUT_MS = 2000" in source
    assert "_BACKGROUND_IDLE_TIMEOUT_MS = 30000" in source
    assert 'is_background = bool(context.get("internal_maintenance"))' in source
    assert "if is_background:" in source
    assert (
        'yield "statement_timeout", f"{_BACKGROUND_STMT_TIMEOUT_MS}ms"'
        in source
    )
    assert 'yield "lock_timeout", f"{_BACKGROUND_LOCK_TIMEOUT_MS}ms"' in source
    assert (
        'yield "idle_in_transaction_session_timeout", '
        'f"{_BACKGROUND_IDLE_TIMEOUT_MS}ms"'
        in source
    )

    assert "_API_STMT_TIMEOUT_MS = 5000" in source
    assert "_API_LOCK_TIMEOUT_MS = 500" in source
    assert "_API_IDLE_TIMEOUT_MS = 15000" in source
    assert 'yield "statement_timeout", f"{_API_STMT_TIMEOUT_MS}ms"' in source
    assert 'yield "lock_timeout", f"{_API_LOCK_TIMEOUT_MS}ms"' in source
    assert (
        'yield "idle_in_transaction_session_timeout", f"{_API_IDLE_TIMEOUT_MS}ms"'
        in source
    )
