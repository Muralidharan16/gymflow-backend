from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/p10l-representative-load-concurrency.yml"
VERIFIER = ROOT / "scripts/ci/p10l_verify_representative_load.py"
BUDGETS = ROOT / "docs/architecture/p10_performance_budgets.v1.json"


def test_p10l_workflow_binds_frozen_budget_and_real_dependencies():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    for fragment in (
        "P10-L Representative Load and Concurrency Stress",
        "scripts/ci/verify_p10_performance_budgets.py",
        "scripts/ci/run_p10b_baseline_calibration.sh",
        "scripts/ci/p10l_verify_representative_load.py",
        "scripts/ci/prepare_p3e_pg16.sh",
        "redis:7-alpine",
        "p10l-evidence",
        "P10_REPRESENTATIVE_LOAD=PASS",
        "P10_CONCURRENCY_STRESS=PASS",
        "P10_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED",
        "Install terminal pytest",
        "pytest==9.1.1",
        "P10B_CPU_LIMIT: '1.20'",
    ):
        assert fragment in workflow


def test_p10l_reuses_real_p5r_contention_with_current_head():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "uses: ./.github/workflows/p5r-race-deadlock-pg16.yml" in workflow
    assert "certification_head: zk07d8e9f0a45" in workflow
    assert "needs: [representative_load, concurrency_stress]" in workflow
    assert "if: always()" in workflow


def test_p10l_verifier_enforces_every_frozen_http_resource_budget():
    verifier = VERIFIER.read_text(encoding="utf-8")
    budgets = json.loads(BUDGETS.read_text(encoding="utf-8"))["budgets"]["representative_http"]
    for key in budgets:
        assert key in verifier
    assert "rejected_connections" in verifier
    assert "postgres_activity_count" in verifier
    assert "DEFERRED_FAIL_CLOSED" in verifier


def test_p10l_applies_stricter_cpu_control_than_frozen_cpu_ceiling():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    harness = (ROOT / "scripts/ci/run_p10b_baseline_calibration.sh").read_text(encoding="utf-8")
    assert "P10B_CPU_LIMIT: '1.20'" in workflow
    assert 'DOCKER_RESOURCE_ARGS+=(--cpus "${P10B_CPU_LIMIT}")' in harness
    assert "P10B_CPU_LIMIT must be in (0, 1.25] cores" in harness
    assert "'cpu_limit_cores':" in harness
