from __future__ import annotations

import ast
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "docs" / "architecture" / "P4_EXTERNAL_EFFECTS_CONTRACT.md"
INVENTORY_PATH = ROOT / "docs" / "architecture" / "p4_external_effect_inventory.json"
LIFECYCLE_POLLER = ROOT / "app" / "tasks" / "branch_outbox_poller.py"
LIFECYCLE_SERVICE = ROOT / "app" / "services" / "branch_lifecycle_service.py"
REMINDERS = ROOT / "app" / "tasks" / "reminders.py"
DAILY_DIGEST = ROOT / "app" / "tasks" / "daily_digest.py"
FINANCE_FOUNDATION = ROOT / "app" / "finance_core" / "models" / "foundation.py"

EXPECTED_LIFECYCLE_EXTERNAL_EVENTS = {
    "branch.search_index",
    "branch.search_deindex",
    "branch.member_notification",
    "branch.refund_required",
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


def test_contract_is_bound_to_certified_p3e_base_and_forbids_attempt_equals_success() -> None:
    contract = CONTRACT_PATH.read_text(encoding="utf-8")
    inventory = _inventory()

    assert inventory["certified_base_sha"] == "882406537584861da2b2b6d44fd37b016a9f8462"
    assert inventory["governing_rule"] == "local_attempt_is_not_external_success"
    assert "A local command, task attempt" in contract
    assert "is not proof that the external business effect succeeded" in contract
    assert "No state machine may transition directly" in contract
    assert "terminal success" in contract


def test_inventory_exactly_tracks_current_lifecycle_external_events() -> None:
    inventory = _inventory()
    inventoried = {
        entry["event_type"] for entry in inventory["lifecycle_external_events"]
    }
    source = LIFECYCLE_POLLER.read_text(encoding="utf-8")
    runtime = _assignment_string_set(source, "_EXTERNAL_EVENT_TYPES")

    assert inventoried == EXPECTED_LIFECYCLE_EXTERNAL_EVENTS
    assert runtime == EXPECTED_LIFECYCLE_EXTERNAL_EVENTS

    for entry in inventory["lifecycle_external_events"]:
        assert entry["current_status"] == "fail_closed_pending_real_provider"
        assert entry["terminal_success_requires"]


def test_p4a_external_handler_remains_fail_closed_until_real_provider_stage() -> None:
    source = LIFECYCLE_POLLER.read_text(encoding="utf-8")

    assert "async def _process_external_event" in source
    assert "No production handler is configured" in source
    assert "Logging is not delivery" in source
    assert "return await _fail_event(" in source

    # P4A must not create a false-delivery shortcut around the fail-closed handler.
    external_handler = source.split("async def _process_external_event", 1)[1].split(
        "async def _process_event", 1
    )[0]
    assert "_mark_delivered(" not in external_handler


def test_legacy_global_notification_entrypoints_remain_fail_closed_in_p4a() -> None:
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


def test_inventory_records_marker_only_search_reconciliation_as_p4b_gap() -> None:
    inventory = _inventory()
    gaps = {entry["id"]: entry for entry in inventory["known_p4_gaps"]}
    gap = gaps["search_marker_only_reconciliation"]
    assert gap["p4_stage"] == "P4B"
    assert "downstream provider evidence" in gap["risk"]

    source = LIFECYCLE_SERVICE.read_text(encoding="utf-8")
    reconciliation = source.split("async def run_reconciliation_sweep", 1)[1]
    assert "search_last_synced_at = :now" in reconciliation
    assert "search_visibility_version = search_visibility_version + 1" in reconciliation


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
    assert '"branch.refund_required"' in poller
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
