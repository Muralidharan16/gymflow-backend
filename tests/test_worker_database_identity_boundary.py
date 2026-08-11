from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "app/core/config.py"
DATABASE = ROOT / "app/core/database.py"


def test_production_requires_distinct_worker_database_identity() -> None:
    source = CONFIG.read_text(encoding="utf-8")

    assert "WORKER_DATABASE_URL: str" in source
    assert 'if self.ENVIRONMENT == "production"' in source
    assert 'if not self.WORKER_DATABASE_URL:' in source
    assert "self.WORKER_DATABASE_URL in" in source
    assert "self.DATABASE_URL" in source
    assert "self.AUTH_DATABASE_URL" in source
    assert "must use a distinct production database identity" in source


def test_worker_asyncpg_connections_are_not_reused_across_celery_event_loops() -> None:
    source = DATABASE.read_text(encoding="utf-8")

    assert "worker_async_engine = create_async_engine" in source
    assert "settings.worker_database_url" in source
    assert "poolclass=NullPool" in source
    assert "WorkerAsyncSessionLocal" in source
    assert '"app.worker_id"' in source
    assert '"app.internal_maintenance"' in source
    assert "_WORKER_STMT_TIMEOUT_MS = 15000" in source
    assert "_WORKER_LOCK_TIMEOUT_MS = 2000" in source
    assert "_WORKER_IDLE_TIMEOUT_MS = 30000" in source
