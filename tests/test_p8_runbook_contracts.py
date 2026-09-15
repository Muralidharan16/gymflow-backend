from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GOVERNANCE_PATH = ROOT / "docs/architecture/p8_observability_matrix.json"
ALERT_CONTRACT_PATH = ROOT / "docs/architecture/p8_alert_slo_contract.json"
RUNBOOK_CONTRACT_PATH = ROOT / "docs/architecture/p8_runbook_contract.json"
DOC_PATH = ROOT / "docs/architecture/P8R_OPERATIONAL_RUNBOOKS.md"

P8A_CERTIFIED_HEAD = "b19714b6c0a0d2b50ca862e35bf096d822014b71"
P8A_CERTIFIED_TREE = "104621750b126c302f890adbf27c13460df5f3a4"


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _section(text: str, heading: str) -> str:
    match = re.search(
        rf"^## {re.escape(heading)}\s*$\n(.*?)(?=^## |\Z)",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match is not None, heading
    return match.group(1).strip()


def test_p8r_is_exactly_bound_to_certified_p8a_parent() -> None:
    contract = _json(RUNBOOK_CONTRACT_PATH)
    assert contract["schema_version"] == 1
    assert contract["phase"] == "P8-R"
    assert contract["parent_certified_head"] == P8A_CERTIFIED_HEAD
    assert contract["parent_certified_tree"] == P8A_CERTIFIED_TREE


def test_every_frozen_critical_alert_has_exactly_one_matching_runbook() -> None:
    governance = _json(GOVERNANCE_PATH)
    alerts = _json(ALERT_CONTRACT_PATH)["critical_alerts"]
    contract = _json(RUNBOOK_CONTRACT_PATH)

    frozen = set(governance["critical_failure_modes"])
    assert set(alerts) == frozen
    assert set(contract["runbooks"]) == frozen
    assert len(set(contract["runbooks"].values())) == len(frozen) == 14

    for failure_mode, relative in contract["runbooks"].items():
        assert relative == alerts[failure_mode]["runbook"]
        path = ROOT / relative
        assert path.is_file(), relative
        assert path.stat().st_size >= 1200, relative


def test_every_runbook_contains_required_operator_sections_and_identity() -> None:
    alerts = _json(ALERT_CONTRACT_PATH)["critical_alerts"]
    contract = _json(RUNBOOK_CONTRACT_PATH)
    required = contract["required_sections"]

    for failure_mode, relative in contract["runbooks"].items():
        text = (ROOT / relative).read_text(encoding="utf-8")
        for heading in required:
            assert f"## {heading}" in text, (relative, heading)

        alert = alerts[failure_mode]
        ownership = _section(text, "Alert and ownership")
        assert f"`{alert['alert']}`" in ownership
        assert f"`{alert['owner']}`" in ownership
        assert f"`{failure_mode}`" in ownership
        assert alert["customer_impact"].split()[0].lower() in _section(
            text, "Customer impact"
        ).lower()


def test_runbooks_have_ordered_diagnosis_safe_actions_forbidden_actions_and_escalation() -> None:
    contract = _json(RUNBOOK_CONTRACT_PATH)

    for relative in contract["runbooks"].values():
        text = (ROOT / relative).read_text(encoding="utf-8")
        diagnosis = _section(text, "Diagnosis")
        safe = _section(text, "Safe first actions")
        forbidden = _section(text, "Forbidden actions")
        escalation = _section(text, "Escalation")
        recovery = _section(text, "Recovery verification")

        assert len(re.findall(r"^\d+\. ", diagnosis, flags=re.MULTILINE)) >= 5, relative
        assert len(re.findall(r"^- ", safe, flags=re.MULTILINE)) >= 2, relative
        forbidden_lines = re.findall(r"^- (.+)$", forbidden, flags=re.MULTILINE)
        assert len(forbidden_lines) >= 2, relative
        assert all("never" in line.lower() for line in forbidden_lines), relative
        assert len(escalation) >= 80, relative
        assert len(recovery) >= 120, relative


def test_finance_and_provider_ambiguity_runbooks_forbid_unsafe_retries_and_terminal_edits() -> None:
    paths = [
        ROOT / "docs/runbooks/p8/finance-reconciliation-mismatch.md",
        ROOT / "docs/runbooks/p8/provider-ack-ambiguity.md",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8").lower() for path in paths)

    assert "blind" in combined
    assert "authoritative evidence" in combined
    assert "replacement" in combined
    assert "idempotency" in combined
    assert "refund-provider execution" in combined
    assert "manual" in combined or "manually" in combined
    assert "terminal" in combined or "successful" in combined


def test_runtime_runbooks_preserve_p5_p6_p7_safety_boundaries() -> None:
    worker = (ROOT / "docs/runbooks/p8/worker-unavailable-redelivery.md").read_text(
        encoding="utf-8"
    ).lower()
    redis = (ROOT / "docs/runbooks/p8/redis-broker-unavailable.md").read_text(
        encoding="utf-8"
    ).lower()
    scheduler = (ROOT / "docs/runbooks/p8/scheduler-ownership-risk.md").read_text(
        encoding="utf-8"
    ).lower()
    database = (ROOT / "docs/runbooks/p8/database-disconnect-storm.md").read_text(
        encoding="utf-8"
    ).lower()
    api = (ROOT / "docs/runbooks/p8/api-slo-burn.md").read_text(encoding="utf-8").lower()

    assert "late acknowledgements" in worker or "late-ack" in worker
    assert "postgresql" in redis and "coordination" in redis
    assert "ownership fencing" in scheduler
    assert "tls" in database and "runtime principal" in database
    assert "graceful drain" in api
    assert "does not authorize a release or deployment" in api


def test_backup_and_observability_runbooks_cannot_fabricate_health() -> None:
    backup = (ROOT / "docs/runbooks/p8/backup-failure-stale.md").read_text(
        encoding="utf-8"
    ).lower()
    pipeline = (ROOT / "docs/runbooks/p8/observability-pipeline-failure.md").read_text(
        encoding="utf-8"
    ).lower()

    assert "infrastructure-owned backup system" in backup
    assert "fabricated" in backup or "fabricate" in backup
    assert "missing metrics" in pipeline
    assert "unknown" in pipeline or "blind" in pipeline
    assert "never infer success" in pipeline
    assert "business authority" in pipeline


def test_global_runbook_contract_preserves_authority_and_recovery_evidence() -> None:
    contract = _json(RUNBOOK_CONTRACT_PATH)
    safety = contract["global_safety"]
    assert safety == {
        "postgresql_remains_business_authority": True,
        "observability_never_authorizes_business_mutation": True,
        "preserve_incident_evidence_before_repair": True,
        "use_certified_replay_or_reconciliation_paths_only": True,
        "manual_terminal_success_without_authoritative_evidence_forbidden": True,
        "blind_external_effect_retry_forbidden": True,
        "disable_fencing_or_idempotency_forbidden": True,
        "refund_provider_execution": "deferred_fail_closed",
    }
    assert all(contract["recovery_verification"].values())
    assert set(contract["hard_stops"]) == {
        "refund_provider_activation",
        "live_money_movement",
        "release",
        "deployment",
        "manual_database_terminal_success_without_authoritative_evidence",
        "blind_provider_retry_after_ambiguous_outcome",
        "disable_worker_scheduler_or_idempotency_fencing_to_clear_alert",
        "delete_dead_letter_or_incident_evidence_to_make_metric_green",
    }


def test_p8r_document_emits_only_runbook_slice_markers() -> None:
    doc = DOC_PATH.read_text(encoding="utf-8")
    for marker in (
        "P8_RUNBOOK_COVERAGE=PASS",
        "P8_ALERT_RUNBOOK_BINDING=PASS",
        "P8_SAFE_FIRST_RESPONSE=PASS",
        "P8_RECOVERY_VERIFICATION=PASS",
        "P8_OBSERVABILITY_NON_AUTHORITATIVE=PASS",
        "P8_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED",
    ):
        assert marker in doc

    assert "does **not** emit `P8_PRODUCTION_LIKE_OBSERVABILITY=PASS`" in doc
    assert "`P8_EVERY_CRITICAL_FAILURE_VISIBLE_ACTIONABLE=PASS`" in doc
    assert "final P8-F" in doc
