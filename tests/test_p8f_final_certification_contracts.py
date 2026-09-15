from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs/architecture/P8F_FINAL_CERTIFICATION.md"
WORKFLOW = ROOT / ".github/workflows/p8f-final-same-head-certification.yml"

P8O_HEAD = "d8c22bd6b26f0da861b7d8957fd9d9d81bc0fcf6"
P8O_TREE = "426b57b351d9ab85df17cc49b151186e18faf4b5"
ALEMBIC_HEAD = "zk07d8e9f0a45"
CERT_BRANCH = "hardening/p8f-final-certification-temp"

REQUIRED_JOBS = (
    "contract",
    "architecture",
    "migration_hardening",
    "finance",
    "migration_lifecycle",
    "migration_preservation",
    "migration_adversarial",
    "migration_contracts",
    "migration_semantics",
    "p3",
    "p3a_general",
    "lifecycle_maintenance",
    "platform_maintenance",
    "p4b_opensearch",
    "p4b_evidence",
    "p4b_drift",
    "p4c_general",
    "p4c_notification",
    "p4d_refund",
    "p4e_contract",
    "p4e_operational",
    "p5_governance",
    "p5_worker_fencing",
    "p5_worker_crash_redelivery",
    "p5_provider_ack",
    "p5_dependency_loss",
    "p5_race_deadlock",
    "p5_compensation",
    "p6_governance",
    "p6_redis",
    "p6_broker",
    "p6_worker",
    "p6_poison",
    "p6_scheduler",
    "p7_governance",
    "p7_system",
    "p7_drain",
    "p7_resource",
    "p7_worker",
    "p7_orchestration",
    "p8_governance",
    "p8_logging",
    "p8_metrics",
    "p8_alerts",
    "p8_runbooks",
    "p8_production_like",
)

P8_WORKFLOWS = (
    ".github/workflows/p8-governance-observability.yml",
    ".github/workflows/p8l-structured-logging-redaction.yml",
    ".github/workflows/p8m-runtime-metrics.yml",
    ".github/workflows/p8a-alerts-slos.yml",
    ".github/workflows/p8r-operational-runbooks.yml",
    ".github/workflows/p8o-production-like-observability.yml",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_final_document_freezes_exact_p8o_parent_and_safety_boundary() -> None:
    text = _read(DOC)
    assert P8O_HEAD in text
    assert P8O_TREE in text
    assert ALEMBIC_HEAD in text
    assert CERT_BRANCH in text
    assert "exactly **46 prerequisite jobs**" in text
    assert "merge to `main`" in text
    assert "refund-provider execution remains deferred and fail-closed" in text
    assert "Integration remains a separate explicitly authorized lifecycle step" in text


def test_workflow_is_bound_to_exact_p8o_parent_and_current_migration_head() -> None:
    text = _read(WORKFLOW)
    assert f"P8O_CERTIFIED_HEAD: {P8O_HEAD}" in text
    assert f"P8O_CERTIFIED_TREE: {P8O_TREE}" in text
    assert f"P8_CURRENT_ALEMBIC_HEAD: {ALEMBIC_HEAD}" in text
    assert f"- {CERT_BRANCH}" in text
    assert 'git merge-base --is-ancestor "${P8O_CERTIFIED_HEAD}" HEAD' in text
    assert 'test "$(git rev-parse "${P8O_CERTIFIED_HEAD}^{tree}")" = "${P8O_CERTIFIED_TREE}"' in text
    assert 'test "$(git rev-parse HEAD)" = "${GITHUB_SHA}"' in text
    assert "git diff --exit-code" in text
    assert "git diff --cached --exit-code" in text


def test_final_topology_has_all_46_prerequisite_jobs_and_terminal_count_guard() -> None:
    text = _read(WORKFLOW)
    assert len(REQUIRED_JOBS) == 46
    for job in REQUIRED_JOBS:
        assert f"      - {job}\n" in text, f"terminal needs missing {job}"
    assert "if: ${{ always() }}" in text
    assert "if len(results) != 46:" in text
    assert "Final P8 prerequisite gates passed: {len(results)}" in text


def test_every_p8_slice_is_reexecuted_as_reusable_workflow() -> None:
    text = _read(WORKFLOW)
    for path in P8_WORKFLOWS:
        assert f"uses: ./{path}" in text
        reusable = _read(ROOT / path)
        assert "workflow_call:" in reusable


def test_final_workflow_reproves_inherited_p7_and_p6_boundaries() -> None:
    text = _read(WORKFLOW)
    required_calls = (
        "./.github/workflows/p7-governance-api-runtime.yml",
        "./.github/workflows/p7s-system-control.yml",
        "./.github/workflows/p7d-request-drain.yml",
        "./.github/workflows/p7r-api-resource-shutdown.yml",
        "./.github/workflows/p7w-worker-termination-inheritance.yml",
        "./.github/workflows/p7o-production-like-orchestration.yml",
        "./.github/workflows/p6r-redis-production-contract.yml",
        "./.github/workflows/p6b-broker-reconnect-pg16.yml",
        "./.github/workflows/p6w-worker-shutdown-redelivery-pg16.yml",
        "./.github/workflows/p6p-poison-message-pg16.yml",
        "./.github/workflows/p6s-scheduler-ownership-pg16.yml",
    )
    for call in required_calls:
        assert f"uses: {call}" in text


def test_final_markers_preserve_non_authority_actionability_and_refund_fail_closed() -> None:
    text = _read(WORKFLOW)
    markers = (
        'echo "P8_EVERY_CRITICAL_FAILURE_VISIBLE_ACTIONABLE=PASS"',
        'echo "P8_PRODUCTION_LIKE_OBSERVABILITY=PASS"',
        'echo "P8_OBSERVABILITY_NON_AUTHORITATIVE=PASS"',
        'echo "P8_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED"',
        'echo "P8F_FINAL_SAME_HEAD_CERTIFICATION=PASS"',
    )
    for marker in markers:
        assert marker in text


def test_certification_workflow_has_no_soft_failure_or_integration_action() -> None:
    text = _read(WORKFLOW)
    assert "continue-on-error" not in text
    assert "pulls: write" not in text
    assert "contents: write" not in text
    assert "gh pr merge" not in text
    assert "git push origin main" not in text
    assert "git tag " not in text
    assert "gh release" not in text
    assert "kubectl apply" not in text
    assert "helm upgrade" not in text


def test_contract_reproves_complete_p8_contract_surface() -> None:
    text = _read(WORKFLOW)
    required_tests = (
        "tests/test_p8_governance_contracts.py",
        "tests/test_p8_logging_redaction_contracts.py",
        "tests/test_p8_metrics_contracts.py",
        "tests/test_p8_alert_slo_contracts.py",
        "tests/test_p8_runbook_contracts.py",
        "tests/test_p8_production_like_observability_contracts.py",
        "tests/test_p8f_final_certification_contracts.py",
    )
    for test_path in required_tests:
        assert test_path in text


def test_migration_sensitive_inherited_gates_target_current_p8_head() -> None:
    text = _read(WORKFLOW)
    assert "certification_head: zk07d8e9f0a45" in text
    assert "test \"$(python -s -m alembic -c alembic.ini heads | awk '{print $1}')\" = \"${P8_CURRENT_ALEMBIC_HEAD}\"" in text
