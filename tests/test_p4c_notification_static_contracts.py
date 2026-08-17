from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "docs" / "architecture" / "p4c-notification-delivery-contract.md"
MIGRATION = ROOT / "alembic" / "versions" / "w07d8e9f0a37_p4c_notification_delivery.py"
RECONCILIATION = ROOT / "alembic" / "versions" / "x07d8e9f0a38_p4c_notification_reconciliation.py"
CRASH_RECOVERY = ROOT / "alembic" / "versions" / "y07d8e9f0a39_p4c_notification_crash_recovery.py"
OPERATIONS = ROOT / "alembic" / "versions" / "z07d8e9f0a3a_p4c_notification_operations.py"
POLLER = ROOT / "app" / "tasks" / "branch_outbox_poller.py"
DELIVERY = ROOT / "app" / "services" / "notification_delivery_service.py"
WEBHOOK_ROUTE = ROOT / "app" / "routers" / "notification_webhooks.py"
MAIN = ROOT / "app" / "main.py"
REMINDERS = ROOT / "app" / "tasks" / "reminders.py"
DIGEST = ROOT / "app" / "tasks" / "daily_digest.py"

EXPECTED_P4_STATES = {
    "pending",
    "processing",
    "provider_accepted",
    "succeeded",
    "retry_pending",
    "dead_lettered",
    "cancelled",
    "superseded",
}


def _tuple_assignment(source: str, name: str) -> tuple[str, ...]:
    tree = ast.parse(source)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            continue
        value = ast.literal_eval(node.value)
        assert isinstance(value, tuple)
        return value
    raise AssertionError(f"assignment {name} not found")


def _normalized(source: str) -> str:
    return " ".join(source.split())


def test_p4c_contract_stacks_exactly_on_certified_p4b_and_preserves_truth_rule() -> None:
    source = CONTRACT.read_text(encoding="utf-8")
    normalized = _normalized(source)
    assert "351fe7680fcf0614bd651b49cd8aae11e689d5e8" in source
    assert "HTTP 2xx" in source
    assert "is **not** proof" in source
    assert "may become `succeeded` only from durable downstream" in normalized
    assert "must never be relabelled `succeeded`" in normalized
    assert "Queue payloads are data, never recipient or tenant authorization authority" in normalized
    assert "Operator input may identify a notification to recover" in normalized
    assert "may not supply an arbitrary destination or message body" in normalized


def test_p4c_migration_chain_is_append_only_after_certified_p4b() -> None:
    assert 'revision = "w07d8e9f0a37"' in MIGRATION.read_text(encoding="utf-8")
    assert 'down_revision = "v07d8e9f0a36"' in MIGRATION.read_text(encoding="utf-8")
    assert 'revision = "x07d8e9f0a38"' in RECONCILIATION.read_text(encoding="utf-8")
    assert 'down_revision = "w07d8e9f0a37"' in RECONCILIATION.read_text(encoding="utf-8")
    assert 'revision = "y07d8e9f0a39"' in CRASH_RECOVERY.read_text(encoding="utf-8")
    assert 'down_revision = "x07d8e9f0a38"' in CRASH_RECOVERY.read_text(encoding="utf-8")
    assert 'revision = "z07d8e9f0a3a"' in OPERATIONS.read_text(encoding="utf-8")
    assert 'down_revision = "y07d8e9f0a39"' in OPERATIONS.read_text(encoding="utf-8")


def test_p4c_uses_shared_p4_states_and_nonterminal_acceptance_evidence() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    for state in EXPECTED_P4_STATES:
        assert f"'{state}'" in source
    assert "provider_accepted_nonterminal" in source
    assert "definite_success" in source
    assert "ambiguous_outcome" in source


def test_p4c_storage_is_force_rls_and_runtime_roles_have_no_direct_crud() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    for relation in (
        "public.member_notification_preferences",
        "public.notification_commands",
        "public.notification_delivery_attempts",
        "public.notification_provider_events",
    ):
        assert relation in source
    assert "ENABLE ROW LEVEL SECURITY" in source
    assert "FORCE ROW LEVEL SECURITY" in source
    assert "REVOKE ALL ON TABLE {relation} FROM PUBLIC" in source
    assert "app_runtime,auth_runtime,worker_runtime,lifecycle_maintenance_runtime" in source
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (MIGRATION, RECONCILIATION, CRASH_RECOVERY, OPERATIONS)
    )
    assert "BYPASSRLS" not in combined
    assert "GRANT ALL" not in combined


def test_p4c_capabilities_are_security_definer_fenced_and_public_revoked() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    signatures = _tuple_assignment(source, "_FUNCTIONS")
    assert set(signatures) == {
        "app_secure.materialize_branch_member_notifications(uuid,uuid)",
        "app_secure.claim_notification_delivery(uuid,uuid)",
        "app_secure.acknowledge_notification_provider_acceptance(uuid,uuid,text,text,text)",
        "app_secure.record_notification_delivery_failure(uuid,uuid,text,text,text)",
        "app_secure.apply_resend_notification_event(text,text,text,timestamptz,text)",
    }
    assert source.count("SECURITY DEFINER") >= len(signatures)
    assert source.count("SET row_security=on") >= len(signatures)
    assert "REVOKE ALL ON FUNCTION {signature} FROM PUBLIC" in source


def test_lifecycle_fanout_is_deterministic_db_authoritative_and_not_false_delivery() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    fanout = source.split("CREATE FUNCTION app_secure.materialize_branch_member_notifications", 1)[1].split(
        "CREATE FUNCTION app_secure.claim_notification_delivery", 1
    )[0]
    assert "branch.member_notification" in fanout
    assert "public.branch_status_history" in fanout
    assert "public.members" in fanout
    assert "public.member_notification_preferences" in fanout
    assert "branch-lifecycle/'||v_correlation::text||'/'||m.id::text||'/email" in fanout
    assert "ON CONFLICT(idempotency_key) DO NOTHING" in fanout
    assert "o.payload" not in fanout
    assert "'notification.delivery'" in fanout
    assert "status='superseded'" in fanout
    assert "status='delivered'" not in fanout


def test_delivery_claim_rechecks_current_member_contact_and_suppression() -> None:
    source = CRASH_RECOVERY.read_text(encoding="utf-8")
    claim = source.split("CREATE FUNCTION app_secure.claim_notification_delivery_v2", 1)[1]
    assert "FROM public.members m" in claim
    assert "LEFT JOIN public.member_notification_preferences p" in claim
    assert "m.email" in claim
    assert "m.is_active IS TRUE" in claim
    assert "m.status::text='active'" in claim
    assert "p.email_suppressed_at IS NULL" in claim
    assert "recipient_or_channel_not_eligible" in claim
    assert "worker_lease_expired_commit_unknown" in claim
    assert "ambiguous_outcome" in claim


def test_provider_acceptance_is_nonterminal_and_terminal_success_requires_provider_evidence() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    acknowledge = source.split("CREATE FUNCTION app_secure.acknowledge_notification_provider_acceptance", 1)[1].split(
        "CREATE FUNCTION app_secure.record_notification_delivery_failure", 1
    )[0]
    assert "provider_accepted_nonterminal" in acknowledge
    assert "status='provider_accepted'" in acknowledge
    assert "status='succeeded'" not in acknowledge
    assert "status='delivered'" not in acknowledge

    webhook = RECONCILIATION.read_text(encoding="utf-8")
    assert "apply_resend_notification_event_v2" in webhook
    assert "email.delivered" in webhook
    assert "status='succeeded'" in webhook
    assert "delivery_outcome='delivered'" in webhook
    assert "recorded_after_delivery" in webhook
    assert "ignored_stale" in webhook
    assert "pending_reference" in webhook


def test_failure_and_reconciliation_paths_are_fenced_bounded_and_never_synthesize_success() -> None:
    base = MIGRATION.read_text(encoding="utf-8")
    failure = base.split("CREATE FUNCTION app_secure.record_notification_delivery_failure", 1)[1].split(
        "CREATE FUNCTION app_secure.apply_resend_notification_event", 1
    )[0]
    assert "p_outcome NOT IN ('permanent_rejection','retryable_failure','ambiguous_outcome')" in failure
    assert "v_command.attempt_count>=v_command.max_attempts" in failure
    assert "LEAST(1800" in failure
    assert "retry_pending" in failure
    assert "dead_lettered" in failure
    assert "status='succeeded'" not in failure

    reconcile = RECONCILIATION.read_text(encoding="utf-8")
    assert "FOR UPDATE SKIP LOCKED" in reconcile
    assert "notification.reconcile" in reconcile
    assert "provider_reference_id" in reconcile
    assert "record_notification_reconciliation_failure" in reconcile


def test_operator_replay_cannot_create_arbitrary_or_duplicate_external_effects() -> None:
    source = RECONCILIATION.read_text(encoding="utf-8")
    replay = source.split("CREATE FUNCTION app_secure.requeue_dead_lettered_notification", 1)[1].split(
        "CREATE FUNCTION app_secure.list_notification_dead_letters", 1
    )[0]
    assert "provider_reference_id IS NOT NULL" in replay
    assert "ambiguous_outcome" in replay
    assert "provider_accepted_nonterminal" in replay
    assert "external effect may already exist" in replay
    assert "FROM public.members m" in replay
    assert "p.email_suppressed_at IS NULL" in replay
    assert "notification_operator_actions" in replay
    assert "p_destination" not in replay
    assert "p_message" not in replay


def test_webhook_http_boundary_verifies_raw_body_before_database_evidence_application() -> None:
    route = WEBHOOK_ROUTE.read_text(encoding="utf-8")
    main = MAIN.read_text(encoding="utf-8")
    assert 'await request.body()' in route
    assert "verify_resend_webhook(" in route
    assert "apply_resend_notification_event_v2" in route
    assert route.index("verify_resend_webhook(") < route.index("apply_resend_notification_event_v2")
    assert 'EXEMPT_PATHS.add("/webhooks/notifications/resend")' in main
    # The provider callback can identify only its own event/message reference.
    # It has no executable tenant/destination/body parameters that could become
    # authorization or delivery authority.
    assert ":tenant_id" not in route
    assert "event.tenant_id" not in route
    assert ":destination" not in route
    assert "event.destination" not in route
    assert ":message_body" not in route


def test_p4c_lifecycle_notifications_are_active_but_refunds_and_legacy_scans_remain_fail_closed() -> None:
    poller = POLLER.read_text(encoding="utf-8")
    delivery = DELIVERY.read_text(encoding="utf-8")
    assert '_NOTIFICATION_EVENT_TYPES' in poller
    assert '"branch.member_notification"' in poller
    assert '"notification.delivery"' in poller
    assert '"notification.reconcile"' in poller
    assert "process_notification_event" in poller
    deferred = poller.split("_DEFERRED_EXTERNAL_EVENT_TYPES", 1)[1].split("}", 1)[0]
    assert '"branch.refund_required"' in deferred
    assert '"branch.member_notification"' not in deferred
    assert "claim_notification_delivery_v2" in delivery
    assert "acknowledge_notification_provider_acceptance" in delivery
    assert "record_notification_delivery_failure" in delivery

    reminders = REMINDERS.read_text(encoding="utf-8")
    digest = DIGEST.read_text(encoding="utf-8")
    assert "tenant-bound durable notification outbox" in reminders
    assert "raise RuntimeError(_DISABLED_MESSAGE)" in reminders
    assert "tenant-bound durable digest dispatcher" in digest
    assert "raise RuntimeError(_DISABLED_MESSAGE)" in digest


def test_p4c_downgrades_refuse_loss_of_provider_reconciliation_operator_and_crash_evidence() -> None:
    base = MIGRATION.read_text(encoding="utf-8")
    assert "downgrade refuses loss of live/provider-backed notification state" in base
    assert "provider_reference_id IS NOT NULL" in base
    reconcile = RECONCILIATION.read_text(encoding="utf-8")
    assert "downgrade refuses loss of notification operator audit evidence" in reconcile
    assert "downgrade refuses loss of live notification reconciliation/replay state" in reconcile
    crash = CRASH_RECOVERY.read_text(encoding="utf-8")
    assert "downgrade refuses loss of crash-recovery ambiguity evidence" in crash


def test_operational_snapshot_is_pii_free_and_maintenance_only() -> None:
    source = OPERATIONS.read_text(encoding="utf-8")
    assert "notification_operational_snapshot" in source
    assert "pending_count" in source
    assert "provider_accepted_count" in source
    assert "dead_letter_count" in source
    assert "oldest_pending_age_seconds" in source
    assert "GRANT EXECUTE ON FUNCTION {_SNAPSHOT} TO lifecycle_maintenance_runtime" in source
    for pii in ("email", "member_name", "destination", "template_data"):
        assert pii not in source
