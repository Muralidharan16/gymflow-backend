from __future__ import annotations

import ast
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "docs" / "architecture" / "P4_EXTERNAL_EFFECTS_CONTRACT.md"
INVENTORY_PATH = ROOT / "docs" / "architecture" / "p4_external_effect_inventory.json"
LIFECYCLE_POLLER = ROOT / "app" / "tasks" / "branch_outbox_poller.py"
LIFECYCLE_SERVICE = ROOT / "app" / "services" / "branch_lifecycle_service.py"
NOTIFICATION_SERVICE = ROOT / "app" / "services" / "notification_delivery_service.py"
REMINDERS = ROOT / "app" / "tasks" / "reminders.py"
DAILY_DIGEST = ROOT / "app" / "tasks" / "daily_digest.py"
FINANCE_FOUNDATION = ROOT / "app" / "finance_core" / "models" / "foundation.py"

EXPECTED_SEARCH_LIFECYCLE_EVENTS = {
    "branch.search_index",
    "branch.search_deindex",
}
EXPECTED_NOTIFICATION_LIFECYCLE_EVENTS = {"branch.member_notification"}
EXPECTED_DEFERRED_LIFECYCLE_EVENTS = {"branch.refund_required"}
EXPECTED_LIFECYCLE_EXTERNAL_EVENTS = (
    EXPECTED_SEARCH_LIFECYCLE_EVENTS
    | EXPECTED_NOTIFICATION_LIFECYCLE_EVENTS
    | EXPECTED_DEFERRED_LIFECYCLE_EVENTS
)
EXPECTED_INTERNAL_NOTIFICATION_EVENTS = {
    "notification.delivery",
    "notification.reconcile",
}
EXPECTED_PROVIDER_OUTCOMES = {
    "definite_success",
    "provider_accepted_nonterminal",
    "permanent_rejection",
    "retryable_failure",
    "ambiguous_outcome",
}
EXPECTED_COMMAND_STATES = {
    "pending",
    "processing",
    "provider_accepted",
    "succeeded",
    "retry_pending",
    "dead_lettered",
    "cancelled",
    "superseded",
}
EXPECTED_EVIDENCE_FIELDS = {
    "internal_effect_id",
    "tenant_or_organization_id",
    "aggregate_id",
    "effect_type",
    "idempotency_key",
    "desired_state_or_version",
    "request_hash",
    "status",
    "attempt_count",
    "next_attempt_at",
    "lease_or_fence",
    "provider_code",
    "provider_reference_id",
    "provider_event_id",
    "acknowledgement_or_evidence",
    "attempted_at",
    "acknowledged_at",
    "completed_at",
    "last_error",
    "correlation_id",
    "dead_letter_reason",
}


def _inventory() -> dict:
    return json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))


def _assignment_literal(source: str, name: str):
    tree = ast.parse(source)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            continue
        return ast.literal_eval(node.value)
    raise AssertionError(f"assignment {name} not found")


def _assignment_string_set(source: str, name: str) -> set[str]:
    return set(_assignment_literal(source, name))


def _function_source(source: str, name: str, next_name: str | None = None) -> str:
    marker = f"async def {name}"
    assert marker in source
    body = source.split(marker, 1)[1]
    if next_name is not None:
        next_marker = f"async def {next_name}"
        assert next_marker in body
        body = body.split(next_marker, 1)[0]
    return body


def test_contract_is_bound_to_certified_p3e_base_and_forbids_attempt_equals_success() -> None:
    contract = CONTRACT_PATH.read_text(encoding="utf-8")
    inventory = _inventory()

    assert inventory["certified_base_sha"] == "882406537584861da2b2b6d44fd37b016a9f8462"
    assert inventory["governing_rule"] == "local_attempt_is_not_external_success"
    assert "A local command, task attempt" in contract
    assert "is not proof that the external business effect succeeded" in contract
    assert "No state machine may transition directly" in contract
    assert "terminal success" in contract


def test_inventory_exactly_tracks_current_lifecycle_external_events_by_domain() -> None:
    inventory = _inventory()
    lifecycle_entries = inventory["lifecycle_external_events"]
    inventoried = {entry["event_type"] for entry in lifecycle_entries}
    by_event = {entry["event_type"]: entry for entry in lifecycle_entries}
    source = LIFECYCLE_POLLER.read_text(encoding="utf-8")

    search_runtime = _assignment_string_set(source, "_SEARCH_EVENT_TYPES")
    notification_runtime = _assignment_string_set(source, "_NOTIFICATION_EVENT_TYPES")
    deferred_runtime = _assignment_string_set(source, "_DEFERRED_EXTERNAL_EVENT_TYPES")
    internal_notifications = {
        entry["event_type"] for entry in inventory["internal_notification_events"]
    }

    assert inventoried == EXPECTED_LIFECYCLE_EXTERNAL_EVENTS
    assert search_runtime == EXPECTED_SEARCH_LIFECYCLE_EVENTS
    assert notification_runtime & inventoried == EXPECTED_NOTIFICATION_LIFECYCLE_EVENTS
    assert internal_notifications == EXPECTED_INTERNAL_NOTIFICATION_EVENTS
    assert internal_notifications <= notification_runtime
    assert internal_notifications.isdisjoint(inventoried)
    assert deferred_runtime == EXPECTED_DEFERRED_LIFECYCLE_EVENTS
    assert search_runtime | (notification_runtime & inventoried) | deferred_runtime == inventoried

    assert by_event["branch.search_index"]["current_status"] == "provider_backed_p4b_certified"
    assert by_event["branch.search_deindex"]["current_status"] == "provider_backed_p4b_certified"
    assert by_event["branch.member_notification"]["current_status"] == "implemented_p4c_candidate_not_certified"
    assert by_event["branch.member_notification"]["certification_status"] == "candidate"
    assert by_event["branch.refund_required"]["current_status"] == "fail_closed_deferred_to_p4d"


def test_current_poller_routes_search_notification_and_refund_without_success_shortcuts() -> None:
    source = LIFECYCLE_POLLER.read_text(encoding="utf-8")
    router = _function_source(source, "_process_event", "_poll_outbox")
    search_handler = _function_source(source, "_process_search_event", "_fail_event")
    deferred_handler = _function_source(source, "_process_deferred_external_event", "_process_event")

    assert "if event_type in _SEARCH_EVENT_TYPES:" in router
    assert "return await _process_search_event(event, worker_id)" in router
    assert "if event_type in _NOTIFICATION_EVENT_TYPES:" in router
    assert "return await process_notification_event(event, worker_id)" in router
    assert "if event_type in _DEFERRED_EXTERNAL_EVENT_TYPES:" in router
    assert "return await _process_deferred_external_event(event, worker_id)" in router
    assert "Unsupported lifecycle outbox event type" in router

    assert "OpenSearchProvider.from_settings()" in search_handler
    assert "await provider.apply(" in search_handler
    assert "await _acknowledge_search_effect(" in search_handler
    assert "provider_evidence_sha256" in source
    assert "_mark_delivered(" not in search_handler

    assert "No production handler is configured" in deferred_handler
    assert "return await _fail_event(" in deferred_handler
    assert "_mark_delivered(" not in deferred_handler


def test_p4c_notification_candidate_routing_uses_durable_fenced_machinery() -> None:
    source = NOTIFICATION_SERVICE.read_text(encoding="utf-8")
    router = _function_source(source, "process_notification_event")

    assert 'event_type == "branch.member_notification"' in router
    assert "return await materialize_member_notifications(event, worker_id)" in router
    assert 'event_type == "notification.delivery"' in router
    assert "return await process_delivery(event, worker_id)" in router
    assert 'event_type == "notification.reconcile"' in router
    assert "return await process_reconciliation(event, worker_id)" in router
    assert "unsupported P4C notification event" in router

    assert "claim_notification_delivery_v2" in source
    assert "acknowledge_notification_provider_acceptance" in source
    assert "provider_accepted" in source
    assert "complete_notification_reconciliation" in source
    assert 'event["payload"]["destination"]' not in source
    assert 'event["payload"]["email"]' not in source


def test_legacy_global_notification_entrypoints_remain_fail_closed() -> None:
    inventory = _inventory()
    tasks = {entry["task"] for entry in inventory["legacy_notification_entrypoints"]}
    assert tasks == {
        "send_daily_reminders",
        "send_birthday_wishes",
        "app.tasks.daily_digest.run",
    }

    reminders = REMINDERS.read_text(encoding="utf-8")
    digest = DAILY_DIGEST.read_text(encoding="utf-8")
    reminder_message = _assignment_literal(reminders, "_DISABLED_MESSAGE")
    digest_message = _assignment_literal(digest, "_DISABLED_MESSAGE")

    assert "tenant-bound durable notification outbox" in reminder_message
    assert "tenant-bound durable digest dispatcher" in digest_message
    assert "raise RuntimeError(_DISABLED_MESSAGE)" in reminders
    assert "raise RuntimeError(_DISABLED_MESSAGE)" in digest


def test_inventory_records_search_gap_resolved_and_refund_gap_still_open() -> None:
    inventory = _inventory()
    resolved = {entry["id"]: entry for entry in inventory["resolved_p4_gaps"]}
    gaps = {entry["id"]: entry for entry in inventory["known_p4_gaps"]}

    search_gap = resolved["search_marker_only_reconciliation"]
    assert search_gap["resolution_status"] == "resolved_by_certified_p4b"
    assert "enqueue_branch_search_reconciliation" in search_gap["resolution"]

    source = LIFECYCLE_SERVICE.read_text(encoding="utf-8")
    reconciliation = source.split("async def run_reconciliation_sweep", 1)[1]
    assert "app_secure.enqueue_branch_search_reconciliation" in reconciliation
    assert "search_last_synced_at = :now" not in reconciliation
    assert "search_visibility_version = search_visibility_version + 1" not in reconciliation

    refund_gap = gaps["lifecycle_refund_provider_deferred"]
    assert refund_gap["p4_stage"] == "P4D"
    assert "fail-closed" in refund_gap["risk"]


def test_finance_refund_foundation_exists_without_claiming_lifecycle_completion() -> None:
    inventory = _inventory()
    finance = inventory["finance_foundation"]
    source = FINANCE_FOUNDATION.read_text(encoding="utf-8")

    assert finance["refund_terminal_success_state"] == "succeeded"
    assert "class FinanceRefund(Base):" in source
    assert "'requested', 'approved', 'rejected', 'processing', 'succeeded', 'failed', 'cancelled'" in source
    assert "class FinancePaymentEvent(Base):" in source
    assert "class FinanceIdempotencyKey(Base):" in source
    assert "class FinanceOutboxEvent(Base):" in source

    poller = LIFECYCLE_POLLER.read_text(encoding="utf-8")
    deferred = _assignment_string_set(poller, "_DEFERRED_EXTERNAL_EVENT_TYPES")
    assert deferred == {"branch.refund_required"}
    assert "No production handler is configured" in poller


def test_inventory_freezes_shared_p4_semantics() -> None:
    required = _inventory()["required_semantics"]
    assert set(required["command_states"]) == EXPECTED_COMMAND_STATES
    assert set(required["provider_outcomes"]) == EXPECTED_PROVIDER_OUTCOMES
    assert set(required["durable_evidence_fields"]) == EXPECTED_EVIDENCE_FIELDS


def test_contract_requires_idempotency_fencing_reconciliation_dlq_and_webhook_security() -> None:
    contract = CONTRACT_PATH.read_text(encoding="utf-8").lower()
    for phrase in (
        "idempotency",
        "lease and fencing",
        "ambiguous outcome",
        "reconciliation contract",
        "dead-letter and operator recovery",
        "webhook/callback contract",
        "provider event",
        "replay",
        "no rls weakening",
        "no `bypassrls`",
    ):
        assert phrase in contract
