from __future__ import annotations

from pathlib import Path

import pytest

from app.core.api_resources import close_api_runtime_resources, dispose_api_database_engines


ROOT = Path(__file__).resolve().parents[1]
MAIN_SOURCE = (ROOT / "app/main.py").read_text(encoding="utf-8")
RESOURCE_SOURCE = (ROOT / "app/core/api_resources.py").read_text(encoding="utf-8")


class _AsyncEngine:
    def __init__(self, events: list[str], *, fail: bool = False) -> None:
        self.events = events
        self.fail = fail

    async def dispose(self) -> None:
        self.events.append("async_db")
        if self.fail:
            raise RuntimeError("async dispose failed")


class _SyncEngine:
    def __init__(self, events: list[str], *, fail: bool = False) -> None:
        self.events = events
        self.fail = fail

    def dispose(self) -> None:
        self.events.append("sync_db")
        if self.fail:
            raise RuntimeError("sync dispose failed")


@pytest.mark.asyncio
async def test_api_database_engines_dispose_independently() -> None:
    events: list[str] = []
    errors = await dispose_api_database_engines(_AsyncEngine(events), _SyncEngine(events))
    assert errors == ()
    assert events == ["async_db", "sync_db"]


@pytest.mark.asyncio
async def test_runtime_cleanup_closes_redis_then_both_api_database_pools() -> None:
    events: list[str] = []

    async def close_redis() -> None:
        events.append("redis")

    async_engine = _AsyncEngine(events)
    sync_engine = _SyncEngine(events)
    errors = await close_api_runtime_resources(
        redis_closer=close_redis,
        async_db_engine=async_engine,
        sync_db_engine=sync_engine,
    )
    assert errors == ()
    assert events == ["redis", "async_db", "sync_db"]

    # All three underlying APIs are idempotent; a repeated shutdown remains safe.
    errors = await close_api_runtime_resources(
        redis_closer=close_redis,
        async_db_engine=async_engine,
        sync_db_engine=sync_engine,
    )
    assert errors == ()
    assert events == [
        "redis",
        "async_db",
        "sync_db",
        "redis",
        "async_db",
        "sync_db",
    ]


@pytest.mark.asyncio
async def test_one_cleanup_failure_does_not_skip_remaining_api_resources() -> None:
    events: list[str] = []

    async def failing_redis() -> None:
        events.append("redis")
        raise RuntimeError("redis close failed")

    errors = await close_api_runtime_resources(
        redis_closer=failing_redis,
        async_db_engine=_AsyncEngine(events, fail=True),
        sync_db_engine=_SyncEngine(events),
    )
    assert events == ["redis", "async_db", "sync_db"]
    assert len(errors) == 2
    assert {str(error) for error in errors} == {"redis close failed", "async dispose failed"}


def test_lifespan_cleanup_is_in_finally_after_supervisor_scope() -> None:
    assert "from app.core.api_resources import close_api_runtime_resources" in MAIN_SOURCE
    assert "async with platform_lifespan():" in MAIN_SOURCE
    assert "finally:\n        await close_api_runtime_resources()" in MAIN_SOURCE
    assert MAIN_SOURCE.index("async with platform_lifespan():") < MAIN_SOURCE.index(
        "await close_api_runtime_resources()"
    )


def test_api_cleanup_does_not_claim_worker_or_maintenance_engine_ownership() -> None:
    forbidden_imports = (
        "worker_async_engine",
        "worker_sync_engine",
        "maintenance_async_engine",
        "MaintenanceAsyncSessionLocal",
        "WorkerAsyncSessionLocal",
    )
    for name in forbidden_imports:
        assert f"from app.core.database import {name}" not in RESOURCE_SOURCE
    assert "from app.core.database import async_engine as api_async_engine" in RESOURCE_SOURCE
    assert "from app.core.database import sync_engine as api_sync_engine" in RESOURCE_SOURCE
