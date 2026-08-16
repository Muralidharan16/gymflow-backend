from __future__ import annotations

import ast
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "docs" / "architecture" / "P4_EXTERNAL_EFFECTS_CONTRACT.md"
INVENTORY_PATH = ROOT / "docs" / "architecture" / "p4_external_effect_inventory.json"
POLLER_PATH = ROOT / "app" / "tasks" / "branch_outbox_poller.py"
LIFECYCLE_SERVICE_PATH = ROOT / "app" / "services" / "branch_lifecycle_service.py"

SEARCH_EVENTS = {"branch.search_index", "branch.search_deindex"}
DEFERRED_EVENTS = {"branch.member_notification", "branch.refund_required"}
ALL_EXTERNAL_EVENTS = SEARCH_EVENTS | DEFERRED_EVENTS


def _literal_assignment(source: str, name: str) -> set[str]:
    tree = ast.parse(source)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            return set(ast.literal_eval(node.value))
    raise AssertionError(f"assignment {name} not found")


def _function_source(source: str, name: str, next_name: str | None = None) -> str:
    marker = f"async def {name}"
    assert marker in source
    body = source.split(marker, 1)[1]
    if next_name is not None:
        next_marker = f"async def {next_name}"
        assert next_marker in body
        body = body.split(next_marker, 1)[0]
    return body


def test_p4b_preserves_the_p4a_external_event_inventory_as_an_explicit_partition() -> None:
    inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    inventoried = {entry["event_type"] for entry in inventory["lifecycle_external_events"]}
    poller = POLLER_PATH.read_text(encoding="utf-8")
    search = _literal_assignment(poller, "_SEARCH_EVENT_TYPES")
    deferred = _literal_assignment(poller, "_DEFERRED_EXTERNAL_EVENT_TYPES")

    assert inventoried == ALL_EXTERNAL_EVENTS
    assert search == SEARCH_EVENTS
    assert deferred == DEFERRED_EVENTS
    assert search.isdisjoint(deferred)
    assert search | deferred == inventoried


def test_p4b_evolves_only_search_events_to_provider_execution() -> None:
    source = POLLER_PATH.read_text(encoding="utf-8")
    search_handler = _function_source(source, "_process_search_event", "_fail_event")
    deferred_handler = _function_source(source, "_process_deferred_external_event", "_process_event")
    router = _function_source(source, "_process_event", "_poll_outbox")

    assert "OpenSearchProvider.from_settings()" in search_handler
    assert "await provider.apply(" in search_handler
    assert "await _acknowledge_search_effect(" in search_handler
    assert "await _record_search_failure(" in search_handler
    assert "await _repair_search_drift(" in search_handler
    assert "_mark_delivered(" not in search_handler

    assert "No production handler is configured" in deferred_handler
    assert "return await _fail_event(" in deferred_handler
    assert "if event_type in _SEARCH_EVENT_TYPES:" in router
    assert "return await _process_search_event(event, worker_id)" in router
    assert "if event_type in _DEFERRED_EXTERNAL_EVENT_TYPES:" in router
    assert "return await _process_deferred_external_event(event, worker_id)" in router


def test_p4a_attempt_is_not_success_rule_remains_governing_in_p4b() -> None:
    inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    contract = CONTRACT_PATH.read_text(encoding="utf-8").lower()
    poller = POLLER_PATH.read_text(encoding="utf-8")
    search_handler = _function_source(poller, "_process_search_event", "_fail_event")

    assert inventory["governing_rule"] == "local_attempt_is_not_external_success"
    assert "local command" in contract
    assert "external business effect succeeded" in contract
    assert "await provider.apply(" in search_handler
    assert "await _acknowledge_search_effect(" in search_handler
    assert search_handler.index("await provider.apply(") < search_handler.index(
        "await _acknowledge_search_effect("
    )


def test_p4b_supersedes_marker_only_reconciliation_without_fabricating_success() -> None:
    source = LIFECYCLE_SERVICE_PATH.read_text(encoding="utf-8")
    marker = "async def run_reconciliation_sweep"
    assert marker in source
    reconciliation = source.split(marker, 1)[1]

    assert "app_secure.enqueue_branch_search_reconciliation" in reconciliation
    assert "search_last_synced_at =" not in reconciliation
    assert "search_visibility_version = search_visibility_version + 1" not in reconciliation
    assert "return int(enqueued_count or 0)" in reconciliation


def test_p4b_keeps_p4a_reliability_and_security_semantics_frozen() -> None:
    inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    required = inventory["required_semantics"]
    contract = CONTRACT_PATH.read_text(encoding="utf-8").lower()

    assert "ambiguous_outcome" in required["provider_outcomes"]
    assert "dead_lettered" in required["command_states"]
    assert "lease_or_fence" in required["durable_evidence_fields"]
    assert "acknowledgement_or_evidence" in required["durable_evidence_fields"]

    for phrase in (
        "idempotency",
        "lease and fencing",
        "ambiguous outcome",
        "reconciliation contract",
        "dead-letter and operator recovery",
        "no rls weakening",
        "no `bypassrls`",
    ):
        assert phrase in contract
