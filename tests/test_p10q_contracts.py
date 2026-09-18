from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/p10q-queue-backlog-recovery.yml"
PROBE = ROOT / "scripts/ci/p10q_backlog_recovery.py"


def test_p10q_requires_real_durable_backlog_worker_replacement_and_timeseries():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    probe = PROBE.read_text(encoding="utf-8")
    for fragment in (
        "P10-Q Queue Throughput and Backlog Recovery",
        "scripts/ci/p10q_backlog_recovery.py",
        "scripts/ci/prepare_p3e_pg16.sh",
        "redis:7-alpine",
        "p10q-evidence",
        "P10_QUEUE_BACKLOG_RECOVERY=PASS",
        "P10_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED",
        "Install terminal pytest",
        "pytest==9.1.1",
    ):
        assert fragment in workflow
    for fragment in (
        "queue_depth_samples",
        "durable_backlog_at_stop",
        "replacement_hostname",
        "durable_business_authority",
        '"postgresql"',
        "DEFERRED_FAIL_CLOSED",
        "items_per_second",
        "deadline_seconds",
        "expire_processing_leases",
        "ci_expired_processing_leases",
        "lease_expiry_fault_injection",
    ):
        assert fragment in probe


def test_p10q_reuses_p5w2_real_crash_redelivery_on_current_schema_head():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "uses: ./.github/workflows/p5w2-worker-crash-redelivery-pg16.yml" in workflow
    assert "certification_head: zk07d8e9f0a45" in workflow
    assert "needs: [backlog_recovery, crash_redelivery]" in workflow
    assert "if: always()" in workflow
