from __future__ import annotations

import pathlib


ROOT = pathlib.Path(__file__).resolve().parents[1]
SUPERVISOR = ROOT / "app" / "core" / "supervisor.py"
CELERY = ROOT / "app" / "core" / "celery_app.py"
TASKS = ROOT / "app" / "tasks" / "platform_maintenance.py"
MIGRATION = (
    ROOT
    / "alembic"
    / "versions"
    / "af5b6c7d8e9f_platform_maintenance_control_plane.py"
)


def _source(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def test_fastapi_supervisor_owns_no_database_global_maintenance_loops() -> None:
    source = _source(SUPERVISOR)
    forbidden = (
        "zombie_reclaim",
        "anchor_key_archive",
        "maps_stale_sweep",
        "maps_retry_sweep",
        "places_cache_cleanup",
        "maps_verification_status",
        "maps_next_retry_at",
        "active_idempotency_keys",
        "google_places_cache",
    )
    assert all(token not in source for token in forbidden)
    assert 'start_worker("lock_registry_sweep"' in source
    assert 'start_worker("kms_bulkhead_sweep"' in source
    assert 'start_worker("wfq_dispatcher"' in source


def test_platform_maintenance_tasks_use_only_maintenance_database_identity() -> None:
    source = _source(TASKS)
    assert "maintenance_async_session_maker" in source
    assert 'internal_maintenance=_PLATFORM_MAINTENANCE_CONTEXT' in source
    assert '_PLATFORM_MAINTENANCE_CONTEXT = "platform"' in source
    assert "AsyncSessionLocal" not in source
    assert "worker_async_session_maker" not in source
    assert "settings.DATABASE_URL" not in source
    assert "app_secure.reclaim_stale_idempotency_keys" in source
    assert "app_secure.archive_expired_idempotency_keys" in source
    assert "app_secure.claim_due_geocoding_reverification" in source
    assert "app_secure.cleanup_expired_places_cache" in source
    assert "geocode_address_task.delay" in source


def test_all_platform_maintenance_tasks_are_routed_to_isolated_maintenance_queue() -> None:
    source = _source(CELERY)
    tasks = (
        "app.tasks.platform_maintenance.reclaim_stale_idempotency",
        "app.tasks.platform_maintenance.archive_expired_idempotency",
        "app.tasks.platform_maintenance.geocoding_reverification",
        "app.tasks.platform_maintenance.cleanup_places_cache",
    )
    for task in tasks:
        assert task in source
    assert 'MAINTENANCE_QUEUE = "lifecycle-maintenance"' in source
    assert '"options": {"queue": MAINTENANCE_QUEUE}' in source


def test_platform_maintenance_migration_never_grants_api_or_worker_global_dml() -> None:
    source = _source(MIGRATION)
    assert "SECURITY DEFINER" in source
    assert "SET search_path = pg_catalog" in source
    assert "SET row_security = on" in source
    assert "app.internal_maintenance" in source
    assert "'platform'" in source
    assert "TO lifecycle_maintenance_runtime" in source
    assert "TO app_security_owner" in source
    assert "GRANT EXECUTE" in source
    assert "TO app_runtime" not in source
    assert "TO worker_runtime" not in source
    assert "BYPASSRLS" not in source.upper()
    assert "GRANT ALL" not in source.upper()


def test_geocoding_reverification_uses_current_state_model_not_legacy_maps_columns() -> None:
    source = _source(MIGRATION)
    assert "branch_geolocation_state" in source
    assert "validation_status" in source
    assert "geocode_attempts" in source
    assert "next_retry_at" in source
    assert "geocoded_at" in source
    assert "maps_verification_status" not in source
    assert "maps_next_retry_at" not in source
    assert "maps_retry_count" not in source


def test_platform_maintenance_helpers_are_bounded_and_context_gated() -> None:
    source = _source(MIGRATION)
    assert "p_batch_size < 1" in source
    assert "p_batch_size > 5000" in source
    assert "p_batch_size > 500" in source
    assert "LIMIT p_batch_size" in source
    assert "FOR UPDATE SKIP LOCKED" in source
    assert "invalid platform idempotency reclaim command" in source
    assert "invalid platform idempotency archive command" in source
    assert "invalid platform geocoding claim command" in source
    assert "invalid platform places-cache cleanup command" in source
