from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/p10f-final-same-head-certification.yml"
DOC = ROOT / "docs/architecture/P10F_FINAL_CERTIFICATION.md"

P9_BASE = "33abd2bad81c65ac998b91726cc314ab54080010"
P9_TREE = "a6a0a87ebbaa251482c67f1bc4cbd9dc8ab7d1e0"
ALEMBIC_HEAD = "zk07d8e9f0a45"
BUDGET_DIGEST = "88705c91dfefc80d835e3c0faed55a0d8becfa46e0df710d82b3baf14f919008"

P10_SLICES = (
    ".github/workflows/p10-governance-performance-security.yml",
    ".github/workflows/p10b-performance-budget-freeze.yml",
    ".github/workflows/p10l-representative-load-concurrency.yml",
    ".github/workflows/p10q-queue-backlog-recovery.yml",
    ".github/workflows/p10d-query-performance.yml",
    ".github/workflows/p10s-long-soak.yml",
    ".github/workflows/p10x-security-scans.yml",
    ".github/workflows/p10h-container-hardening.yml",
)

P9_REUSABLE = (
    ".github/workflows/p9-governance-data-protection-recovery.yml",
    ".github/workflows/p9m-populated-predecessor-head.yml",
    ".github/workflows/p9l-lock-rewrite-analysis.yml",
    ".github/workflows/p9b-backup-integrity.yml",
    ".github/workflows/p9r-full-restore-pitr.yml",
    ".github/workflows/p9c-rolling-schema-compatibility.yml",
    ".github/workflows/p9d-bad-deployment-rollback.yml",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_p10f_document_binds_exact_baseline_and_non_release_boundary():
    text = _read(DOC)
    for token in (P9_BASE, P9_TREE, ALEMBIC_HEAD, BUDGET_DIGEST):
        assert token in text
    assert "authorizes no release or deployment" in text
    assert "Integration remains a separate explicitly authorized lifecycle step" in text


def test_p10f_runs_only_on_exact_branch_push_and_is_read_only():
    text = _read(WORKFLOW)
    assert "hardening/p10-performance-security-final-production-certification" in text
    assert "pull_request:" not in text.split("jobs:", 1)[0]
    assert "contents: read" in text
    assert "actions: read" in text
    assert "contents: write" not in text
    assert "actions: write" not in text


def test_p10f_reexecutes_p9_inherited_topology_and_all_p9_slices():
    text = _read(WORKFLOW)
    assert "P10F_INHERITED_JOB_COUNT: '53'" in text
    for path in P9_REUSABLE:
        assert f"uses: ./{path}" in text
    for required in (
        ".github/workflows/p3e-certification.yml",
        ".github/workflows/p4b-opensearch-live.yml",
        ".github/workflows/p4d-refund-authority-pg16.yml",
        ".github/workflows/p5w2-worker-crash-redelivery-pg16.yml",
        ".github/workflows/p5r-race-deadlock-pg16.yml",
        ".github/workflows/p7o-production-like-orchestration.yml",
        ".github/workflows/p8o-production-like-observability.yml",
    ):
        assert f"uses: ./{required}" in text
    assert "if len(results) != int(os.environ[\"P10F_INHERITED_JOB_COUNT\"])" in text


def test_p10f_binds_all_p10_slices_and_required_artifacts():
    text = _read(WORKFLOW)
    for path in P10_SLICES:
        assert f'"{path}"' in text
    for label in ("P10-L", "P10-Q", "P10-D", "P10-S", "P10-X", "P10-H"):
        assert f'"{label}"' in text
    assert "artifacts_url" in text
    assert "artifact_evidence" in text
    assert "budget_digest" in text


def test_p10f_emits_terminal_markers_and_has_no_integration_action():
    text = _read(WORKFLOW)
    for marker in (
        "P10_P1_P9_INHERITED=PASS",
        "P10_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED",
        "P10_FINAL_PRODUCTION_CERTIFICATION=PASS",
    ):
        assert marker in text
    for forbidden in (
        "gh pr merge",
        "git push origin main",
        "git tag ",
        "gh release",
        "kubectl apply",
        "helm upgrade",
        "continue-on-error",
    ):
        assert forbidden not in text
