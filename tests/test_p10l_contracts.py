from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/p10l-representative-load-concurrency.yml"
VERIFIER = ROOT / "scripts/ci/p10l_verify_representative_load.py"
BINDER = ROOT / "scripts/ci/p10l_bind_same_head_baseline.py"
BUDGETS = ROOT / "docs/architecture/p10_performance_budgets.v1.json"


def test_p10l_workflow_binds_frozen_budget_and_real_dependencies():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    for fragment in (
        "P10-L Representative Load and Concurrency Stress",
        "scripts/ci/verify_p10_performance_budgets.py",
        "scripts/ci/p10l_bind_same_head_baseline.py",
        "scripts/ci/p10l_verify_representative_load.py",
        "actions: read",
        "P10L_SAME_HEAD_BASELINE_BOUND=PASS",
        "p10l-evidence",
        "P10_REPRESENTATIVE_LOAD=PASS",
        "P10_CONCURRENCY_STRESS=PASS",
        "P10_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED",
        "Install terminal pytest",
        "pytest==9.1.1",
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


def test_p10l_binds_canonical_same_head_load_instead_of_retrying_or_loosened_budget():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    binder = BINDER.read_text(encoding="utf-8")
    assert "run_p10b_baseline_calibration.sh" not in workflow
    assert "P10B_CPU_LIMIT:" not in workflow
    assert "p10b-baseline-calibration.yml" in binder
    assert "head_sha" in binder
    assert "candidate_sha" in binder
    assert "P10L_SAME_HEAD_BASELINE_BOUND=PASS" in binder
    assert "retry" not in workflow.lower()
    assert "min_throughput_rps" in VERIFIER.read_text(encoding="utf-8")
