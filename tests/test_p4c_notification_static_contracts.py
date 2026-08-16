from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "docs" / "architecture" / "p4c-notification-delivery-contract.md"
MIGRATION = ROOT / "alembic" / "versions" / "w07d8e9f0a37_p4c_notification_delivery.py"
POLLER = ROOT / "app" / "tasks" / "branch_outbox_poller.py"
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


def test_p4c_contract_stacks_exactly_on_certified_p4b_and_preserves_truth_rule() -> None:
    source = CONTRACT.read_text(encoding="utf-8")
    assert "351fe7680fcf0614bd651b49cd8aae11e689d5e8" in source
    assert "HTTP 2xx" in source
    assert "is **not** proof" in source
    assert "may become `succeeded` only from durable downstream" in source
    assert "must never be relabelled `succeeded`" in source
    assert "Queue payloads are data, never recipient or tenant authorization authority" in source
    assert "Operator input may identify a notification to recover" in source
    assert "may not supply an arbitrary destination or message body" in source


def test_p4c_migration_is_append_only_after_p4b_and_uses_shared_p4_states() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'revision = "w07d8e9f0a37"' in source
    assert 'down_revision = "v07d8e9f0a36"' in source
    command_states = {
        "pending",
        "processing",
        "provider_accepted",
        "succeeded",
        "retry_pending",
        "dead_lettered",
        "cancelled",
        "superseded",
    }
    for state in command_states:
        assert f"'{state}'" in source
    assert command_states == EXPECTED_P4_STATES
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
    assert "BYPASSRLS" not in source
    assert "ALTER ROLE" not in source
    assert "GRANT ALL" not in source


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
    assert "GRANT EXECUTE ON FUNCTION {signature} TO worker_runtime" in source
    assert "GRANT EXECUTE ON FUNCTION {_FUNCTIONS[4]} TO app_runtime" in source
    assert "lifecycle_maintenance_runtime" not in source.split("def _create_functions", 1)[1].split("def _post_install_proof", 1)[0].split("GRANT EXECUTE")[-1]


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
    assert "event_type,'notification.delivery'" not in fanout  # guard against brittle string reconstruction
    assert "'notification.delivery'" in fanout
    assert "status='superseded'" in fanout
    assert "status='delivered'" not in fanout


def test_delivery_claim_rechecks_current_member_contact_and_suppression() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    claim = source.split("CREATE FUNCTION app_secure.claim_notification_delivery", 1)[1].split(
        "CREATE FUNCTION app_secure.acknowledge_notification_provider_acceptance", 1
    )[0]
    assert "FROM public.members m" in claim
    assert "LEFT JOIN public.member_notification_preferences p" in claim
    assert "m.email" in claim
    assert "m.is_active IS TRUE" in claim
    assert "m.status::text='active'" in claim
    assert "p.email_suppressed_at IS NULL" in claim
    assert "recipient_or_channel_not_eligible" in claim
    assert "notification_recipient_suppressed" in claim
    assert "RETURN QUERY" in claim


def test_provider_acceptance_is_nonterminal_and_terminal_success_requires_provider_event() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    acknowledge = source.split("CREATE FUNCTION app_secure.acknowledge_notification_provider_acceptance", 1)[1].split(
        "CREATE FUNCTION app_secure.record_notification_delivery_failure", 1
    )[0]
    assert "provider_accepted_nonterminal" in acknowledge
    assert "status='provider_accepted'" in acknowledge
    assert "status='succeeded'" not in acknowledge
    assert "status='delivered'" not in acknowledge

    webhook = source.split("CREATE FUNCTION app_secure.apply_resend_notification_event", 1)[1]
    assert "email.delivered" in webhook
    assert "status='succeeded'" in webhook
    assert "delivery_outcome='delivered'" in webhook
    assert "status='delivered'" in webhook
    assert "email.bounced" in webhook
    assert "email.complained" in webhook
    assert "status='dead_lettered'" in webhook
    assert "pending_reference" in webhook
    assert "ignored_stale" in webhook


def test_failure_path_is_fenced_bounded_and_never_synthesizes_success() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    failure = source.split("CREATE FUNCTION app_secure.record_notification_delivery_failure", 1)[1].split(
        "CREATE FUNCTION app_secure.apply_resend_notification_event", 1
    )[0]
    assert "p_outcome NOT IN ('permanent_rejection','retryable_failure','ambiguous_outcome')" in failure
    assert "v_command.attempt_count>=v_command.max_attempts" in failure
    assert "LEAST(1800" in failure
    assert "retry_pending" in failure
    assert "dead_lettered" in failure
    assert "status='succeeded'" not in failure
    assert "status='delivered'" not in failure


def test_p4c_downgrade_refuses_provider_backed_state_and_restores_predecessor_contract() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    downgrade = source.split("def downgrade()", 1)[1]
    assert "downgrade refuses loss of live/provider-backed notification state" in downgrade
    assert "provider_reference_id IS NOT NULL" in downgrade
    assert "provider_evidence_sha256 IS NOT NULL" in downgrade
    assert "_set_outbox_statuses(_PREDECESSOR_STATUSES)" in downgrade


def test_refund_and_legacy_notification_jobs_remain_fail_closed_during_foundation_slice() -> None:
    poller = POLLER.read_text(encoding="utf-8")
    assert '"branch.refund_required"' in poller
    assert '"branch.member_notification"' in poller  # still deferred until worker wiring lands
    assert "No production handler is configured" in poller

    reminders = REMINDERS.read_text(encoding="utf-8")
    digest = DIGEST.read_text(encoding="utf-8")
    assert "tenant-bound durable notification outbox" in reminders
    assert "raise RuntimeError(_DISABLED_MESSAGE)" in reminders
    assert "tenant-bound durable digest dispatcher" in digest
    assert "raise RuntimeError(_DISABLED_MESSAGE)" in digest
