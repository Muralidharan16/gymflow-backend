from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "u07d8e9f0a35_p4b_search_external_evidence.py"
POLLER = ROOT / "app" / "tasks" / "branch_outbox_poller.py"
SERVICE = ROOT / "app" / "services" / "branch_lifecycle_service.py"


def _op_execute_statements(source: str) -> set[str]:
    """Return constant SQL passed to op.execute, normalized for whitespace."""

    statements: set[str] = set()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        function = node.func
        if not (
            isinstance(function, ast.Attribute)
            and function.attr == "execute"
            and isinstance(function.value, ast.Name)
            and function.value.id == "op"
        ):
            continue
        try:
            value = ast.literal_eval(node.args[0])
        except (ValueError, TypeError):
            continue
        if isinstance(value, str):
            statements.add(" ".join(value.split()))
    return statements


def _literal_assignment(source: str, name: str) -> tuple[str, ...]:
    tree = ast.parse(source)
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == name:
            value = ast.literal_eval(node.value)
            assert isinstance(value, tuple)
            assert all(isinstance(item, str) for item in value)
            return value
    raise AssertionError(f"missing literal assignment {name}")


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


def test_predecessor_security_owner_acl_is_preserved_and_disjoint_from_p4b_delta() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    branch_inherited = set(_literal_assignment(source, "_BRANCH_INHERITED_SELECT_COLUMNS"))
    branch_owned = set(_literal_assignment(source, "_BRANCH_SELECT_COLUMNS"))
    state_inherited = set(_literal_assignment(source, "_STATE_INHERITED_SELECT_COLUMNS"))
    state_owned = set(_literal_assignment(source, "_STATE_SELECT_COLUMNS"))
    outbox_inherited_select = set(
        _literal_assignment(source, "_OUTBOX_INHERITED_SELECT_COLUMNS")
    )
    outbox_inherited_insert = set(
        _literal_assignment(source, "_OUTBOX_INHERITED_INSERT_COLUMNS")
    )

    assert branch_inherited == {"id", "org_id"}
    assert state_inherited == {"branch_id", "deleted_at", "is_active"}
    assert branch_inherited.isdisjoint(branch_owned)
    assert state_inherited.isdisjoint(state_owned)
    assert outbox_inherited_select == {
        "outbox_id",
        "tenant_id",
        "branch_id",
        "event_type",
        "status",
        "leased_by",
        "leased_until",
        "correlation_id",
    }
    assert outbox_inherited_insert == {
        "outbox_id",
        "tenant_id",
        "branch_id",
        "event_type",
        "payload",
        "created_at",
        "process_after",
        "status",
        "attempt_count",
        "max_attempts",
        "correlation_id",
        "leased_by",
        "leased_until",
    }
    assert "_require_inherited_direct_grants(bind)" in source
    assert '"GRANT SELECT (" + ",".join(_BRANCH_SELECT_COLUMNS)' in source
    assert '"REVOKE SELECT (" + ",".join(_BRANCH_SELECT_COLUMNS)' in source
    assert '"GRANT SELECT (" + ",".join(_STATE_SELECT_COLUMNS)' in source
    assert '"REVOKE SELECT (" + ",".join(_STATE_SELECT_COLUMNS)' in source


def test_worker_and_maintenance_capabilities_are_separated() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    statements = _op_execute_statements(source)

    assert (
        "GRANT EXECUTE ON FUNCTION "
        "app_secure.claim_branch_search_projection(uuid,uuid) TO worker_runtime"
        in statements
    )
    assert (
        "GRANT EXECUTE ON FUNCTION "
        "app_secure.acknowledge_branch_search_effect(uuid,uuid,bigint,text,text,text,text,text,bigint,text,text) "
        "TO worker_runtime"
        in statements
    )
    assert (
        "GRANT EXECUTE ON FUNCTION "
        "app_secure.record_branch_search_failure(uuid,uuid,bigint,text,text,text,text,text) "
        "TO worker_runtime"
        in statements
    )
    assert (
        "GRANT EXECUTE ON FUNCTION "
        "app_secure.enqueue_branch_search_reconciliation(integer) "
        "TO lifecycle_maintenance_runtime"
        in statements
    )
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
