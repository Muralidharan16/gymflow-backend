from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
POLLER = ROOT / "app" / "tasks" / "branch_outbox_poller.py"
NOTIFICATION_SERVICE = ROOT / "app" / "services" / "notification_delivery_service.py"
REMINDERS = ROOT / "app" / "tasks" / "reminders.py"
DIGEST = ROOT / "app" / "tasks" / "daily_digest.py"
SEARCH_PROVIDER = ROOT / "app" / "services" / "search_provider.py"
P4C_CONTRACT = ROOT / "docs" / "architecture" / "p4c-notification-delivery-contract.md"

EXPECTED_SHARED_STATES = {
    "pending",
    "processing",
    "provider_accepted",
    "succeeded",
    "retry_pending",
    "dead_lettered",
    "cancelled",
    "superseded",
}


def _set_assignment(source: str, name: str) -> set[str]:
    tree = ast.parse(source)
    for node in tree.body:
        target_name = None
        value_node = None
        if isinstance(node, ast.Assign):
            if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                target_name = node.targets[0].id
                value_node = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target_name = node.target.id
            value_node = node.value
        if target_name == name and value_node is not None:
            value = ast.literal_eval(value_node)
            return set(value)
    raise AssertionError(f"assignment {name!r} not found")


def test_p4c_preserves_shared_external_effect_state_vocabulary() -> None:
    contract = P4C_CONTRACT.read_text(encoding="utf-8")
    for state in EXPECTED_SHARED_STATES:
        assert f"`{state}`" in contract or f"'{state}'" in contract
    assert "provider_accepted" in contract
    assert "succeeded" in contract
    assert "HTTP 2xx" in contract
    assert "is **not** proof" in contract


def test_p4b_search_effects_remain_on_real_provider_path() -> None:
    poller = POLLER.read_text(encoding="utf-8")
    search = SEARCH_PROVIDER.read_text(encoding="utf-8")
    assert _set_assignment(poller, "_SEARCH_EVENT_TYPES") == {
        "branch.search_deindex",
        "branch.search_index",
    }
    assert "_process_search_event" in poller
    assert "OpenSearchProvider.from_settings()" in poller
    assert "claim_branch_search_projection" in poller
    assert "acknowledge_branch_search_effect" in poller
    assert "record_branch_search_failure" in poller
    assert "repair_branch_search_provider_drift" in poller
    assert "version_type" in search
    assert "external_gte" in search
    assert "search_last_synced_at" not in poller


def test_p4c_intentionally_admits_lifecycle_notifications_but_keeps_refunds_deferred() -> None:
    poller = POLLER.read_text(encoding="utf-8")
    notification_types = _set_assignment(poller, "_NOTIFICATION_EVENT_TYPES")
    deferred_types = _set_assignment(poller, "_DEFERRED_EXTERNAL_EVENT_TYPES")

    assert notification_types == {
        "branch.member_notification",
        "notification.delivery",
        "notification.reconcile",
    }
    assert deferred_types == {"branch.refund_required"}
    assert "branch.member_notification" not in deferred_types
    assert "branch.refund_required" not in notification_types
    assert "process_notification_event(event, worker_id)" in poller


def test_p4c_notification_execution_uses_fenced_db_authority_not_queue_recipient_payloads() -> None:
    service = NOTIFICATION_SERVICE.read_text(encoding="utf-8")
    assert "materialize_branch_member_notifications" in service
    assert "claim_notification_delivery_v2" in service
    assert "acknowledge_notification_provider_acceptance" in service
    assert "record_notification_delivery_failure" in service
    assert "claim_notification_reconciliation" in service
    assert "complete_notification_reconciliation" in service
    assert "event[\"payload\"][\"email\"]" not in service
    assert "event[\"payload\"][\"destination\"]" not in service


def test_unsafe_legacy_global_notification_scans_remain_fail_closed() -> None:
    reminders = REMINDERS.read_text(encoding="utf-8")
    digest = DIGEST.read_text(encoding="utf-8")
    assert "tenant-bound durable notification outbox" in reminders
    assert "raise RuntimeError(_DISABLED_MESSAGE)" in reminders
    assert "tenant-bound durable digest dispatcher" in digest
    assert "raise RuntimeError(_DISABLED_MESSAGE)" in digest


def test_p4c_does_not_reintroduce_attempt_equals_success_or_local_provider_truth() -> None:
    poller = POLLER.read_text(encoding="utf-8")
    service = NOTIFICATION_SERVICE.read_text(encoding="utf-8")
    assert "provider_accepted" in poller
    assert "provider_accepted" in service
    assert "succeeded" not in service
    assert "search_last_synced_at" not in poller
    assert "search_visibility_version" not in poller
