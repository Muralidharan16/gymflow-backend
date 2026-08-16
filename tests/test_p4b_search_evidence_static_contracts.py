from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "u07d8e9f0a35_p4b_search_external_evidence.py"
POLLER = ROOT / "app" / "tasks" / "branch_outbox_poller.py"
SERVICE = ROOT / "app" / "services" / "branch_lifecycle_service.py"


def test_p4b_migration_is_append_only_on_certified_p3e_head() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'revision = "u07d8e9f0a35"' in source
    assert 'down_revision = "t07d8e9f0a34"' in source


def test_search_evidence_table_is_force_rls_and_runtime_has_no_direct_grant() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert "CREATE TABLE public.branch_search_effect_attempts" in source
    assert "ENABLE ROW LEVEL SECURITY" in source
    assert "FORCE ROW LEVEL SECURITY" in source
    for forbidden in (
        "GRANT SELECT ON TABLE public.branch_search_effect_attempts TO worker_runtime",
        "GRANT INSERT ON TABLE public.branch_search_effect_attempts TO worker_runtime",
        "GRANT UPDATE ON TABLE public.branch_search_effect_attempts TO worker_runtime",
        "GRANT SELECT ON TABLE public.branch_search_effect_attempts TO lifecycle_maintenance_runtime",
        "GRANT INSERT ON TABLE public.branch_search_effect_attempts TO lifecycle_maintenance_runtime",
    ):
        assert forbidden not in source


def test_worker_and_maintenance_capabilities_are_separated() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert (
        "app_secure.claim_branch_search_projection(uuid,uuid) "
        "TO worker_runtime"
    ) in " ".join(source.split())
    assert "acknowledge_branch_search_effect" in source
    assert "record_branch_search_failure" in source
    assert (
        "app_secure.enqueue_branch_search_reconciliation(integer) "
        "TO lifecycle_maintenance_runtime"
    ) in " ".join(source.split())
    assert "leaked global reconciliation capability to worker_runtime" in source


def test_queue_event_label_is_not_search_authority() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    claim = source.split("CREATE FUNCTION app_secure.claim_branch_search_projection", 1)[1]
    claim = claim.split("CREATE FUNCTION app_secure.acknowledge_branch_search_effect", 1)[0]
    assert "event_type IN ('branch.search_index', 'branch.search_deindex')" in claim
    assert "WHEN s.is_operational AND s.is_public AND s.deleted_at IS NULL" in claim
    assert "THEN 'index'::text" in claim
    assert "ELSE 'delete'::text" in claim
    assert "search_visibility_version" in claim


def test_success_requires_live_lease_current_version_and_provider_evidence() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    ack = source.split("CREATE FUNCTION app_secure.acknowledge_branch_search_effect", 1)[1]
    ack = ack.split("CREATE FUNCTION app_secure.record_branch_search_failure", 1)[0]
    assert "leased_until > pg_catalog.clock_timestamp()" in ack
    assert "v_current_version = p_desired_version" in ack
    assert "v_current_operation = p_operation" in ack
    assert "provider_evidence_sha256" in ack
    assert "search_provider_ack_version = p_desired_version" in ack
    assert "search_last_synced_at = pg_catalog.clock_timestamp()" in ack
    assert "CASE WHEN v_applied THEN 'delivered' ELSE 'superseded' END" in ack


def test_projection_version_changes_only_from_searchable_state_or_branch_fields() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert "BEFORE UPDATE OF status, is_operational, is_public, deleted_at" in source
    assert (
        "AFTER UPDATE OF branch_name, internal_slug, timezone, region_code, country_code"
        in source
    )
    assert "NEW.search_visibility_version := OLD.search_visibility_version + 1" in source
    assert "search_visibility_version = search_visibility_version + 1" in source


def test_reconciliation_enqueues_work_and_never_marks_local_sync_success() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    reconcile = source.split("CREATE FUNCTION app_secure.enqueue_branch_search_reconciliation", 1)[1]
    reconcile = reconcile.split("CREATE FUNCTION app_secure.bump_branch_search_version_from_state", 1)[0]
    assert "FOR UPDATE SKIP LOCKED" in reconcile
    assert "p_batch_size < 1 OR p_batch_size > 100" in reconcile
    assert "INSERT INTO public.branch_outbox_events" in reconcile
    assert "search_provider_ack_version IS DISTINCT FROM s.search_visibility_version" in reconcile
    assert "search_last_synced_at =" not in reconcile
    assert "search_provider_ack_version =" not in reconcile


def test_pre_p4b_false_sync_implementation_is_still_visible_for_replacement() -> None:
    source = SERVICE.read_text(encoding="utf-8")
    reconciliation = source.split("async def run_reconciliation_sweep", 1)[1]
    assert "search_last_synced_at = :now" in reconciliation
    assert "search_visibility_version = search_visibility_version + 1" in reconciliation


def test_search_handler_remains_fail_closed_until_provider_adapter_is_added() -> None:
    source = POLLER.read_text(encoding="utf-8")
    handler = source.split("async def _process_external_event", 1)[1].split(
        "async def _process_event", 1
    )[0]
    assert "No production handler is configured" in handler
    assert "_mark_delivered(" not in handler
