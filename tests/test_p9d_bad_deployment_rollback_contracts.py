from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "docs/architecture/p9_data_protection_recovery_matrix.json"
WORKFLOW_PATH = ROOT / ".github/workflows/p9d-bad-deployment-rollback.yml"
SCRIPT_PATH = ROOT / "scripts/ci/run_p9d_bad_deployment_rollback.sh"
HARNESS_PATH = ROOT / "scripts/ci/p9d_durable_work_harness.py"
PG16_INSTALLER_PATH = ROOT / "scripts/ci/install_pg16_test_stack.sh"

BRANCH = "hardening/p9-data-protection-disaster-recovery"
LAST_KNOWN_GOOD_SHA = "d8e422aafe061e179bd15cb22aeb8eacf110b5be"
HEAD = "zk07d8e9f0a45"


def _workflow() -> dict:
    return yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def _source() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in (WORKFLOW_PATH, SCRIPT_PATH, HARNESS_PATH)
    )


def test_p9d_matrix_requires_real_bad_release_rollback_and_durable_integrity() -> None:
    rollback = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))["deployment_rollback"]
    assert rollback == {
        "bad_release_injection_required": True,
        "readiness_failure_or_runtime_failure_detection_required": True,
        "traffic_return_to_last_known_good_required": True,
        "database_downgrade_must_not_be_first_line_rollback": True,
        "inflight_and_durable_work_recovery_required": True,
        "post_rollback_integrity_validation_required": True,
        "no_duplicate_financial_effects_required": True,
        "no_lost_durable_work_required": True,
    }


def test_p9d_workflow_is_exact_lkg_real_pg16_and_read_only_permission_bound() -> None:
    workflow = _workflow()
    source = _source()
    job = workflow["jobs"]["bad_deployment_rollback"]
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["on"]["push"]["branches"] == [BRANCH]
    assert "workflow_call" in workflow["on"]
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["env"]["P9D_LAST_KNOWN_GOOD_SHA"] == LAST_KNOWN_GOOD_SHA
    assert job["env"]["P9D_HEAD"] == HEAD
    assert "scripts/ci/install_pg16_test_stack.sh" in source
    pg16_installer = PG16_INSTALLER_PATH.read_text(encoding="utf-8")
    assert "postgresql-16" in pg16_installer
    assert "postgresql-client-16" in pg16_installer
    assert "nginx" in source.lower()
    assert "git worktree add --detach" in source
    assert "/_system/live" in source
    assert "/_system/ready" in source


def test_p9d_injects_readiness_failure_and_returns_real_router_to_lkg() -> None:
    source = _source()
    for required in (
        "P9D_BAD_RELEASE_INJECTED=PASS",
        "P9D_READINESS_FAILURE_DETECTED=PASS",
        "P9D_TRAFFIC_RETURNED_TO_LAST_KNOWN_GOOD=PASS",
        "candidate-router-ready-body.json",
        "rollback-router-ready.json",
        "P9D_BAD_REDIS_PORT",
    ):
        assert required in source
    assert "nginx -p" in source
    assert "-s reload" in source


def test_p9d_recovers_candidate_claim_with_exact_lkg_worker_and_single_effect() -> None:
    source = _source()
    assert "before_db_commit" in source
    assert "P9D_DURABLE_WORK_CLAIM_SURVIVED_BAD_RELEASE=PASS" in source
    assert "P9D_DURABLE_WORK_RECOVERED_BY_LAST_KNOWN_GOOD=PASS" in source
    assert "P9D_NO_LOST_DURABLE_WORK=PASS" in source
    assert "P9D_SINGLE_TERMINAL_EFFECT=PASS" in source
    assert 'module.ROOT = Path(os.environ["P9D_LKG_SOURCE"]).resolve()' in source
    assert 'assert duplicate["claimed"] == 0' in source
    assert 'module._assert_single_terminal_effect(seed, fence=2)' in source


def test_p9d_never_uses_database_downgrade_and_keeps_schema_at_head() -> None:
    source = _source().lower()
    assert "alembic downgrade" not in source
    assert "'database_downgrade_used': false" in source
    assert "p9d_database_remained_at_head=pass" in source


def test_p9d_proves_integrity_finance_immutability_and_provider_fail_closed() -> None:
    source = _source()
    for required in (
        "p9m_stable_snapshot.sql",
        "p9m_verify_head_capability.sql",
        "finance-before-rollback.sql",
        "finance-after-rollback.sql",
        "P9D_POST_ROLLBACK_INTEGRITY=PASS",
        "P9D_NO_FINANCIAL_STATE_CHANGE=PASS",
        "test_p4d_refund_authority_static_contracts.py",
        "test_concurrent_materialization_creates_exactly_one_logical_command",
        "test_same_worker_expired_lease_reclaim_rotates_fence_and_rejects_stale_fence",
        "NOTIFICATION_EMAIL_PROVIDER_MODE=disabled",
        "SEARCH_PROVIDER_MODE=disabled",
        "PLATFORM_BILLING_PROVIDER_MODE=disabled",
        "P9_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED",
        "iptables",
    ):
        assert required in source
    assert "P9D_RUNNER_UID" not in source


def test_p9d_emits_machine_readable_evidence_and_terminal_markers() -> None:
    source = _source()
    assert "decision.json" in source
    assert "'decision': 'PASS'" in source
    assert "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02" in source
    assert "P9_DEPLOYMENT_ROLLBACK=PASS" in source
    assert "P9_BAD_DEPLOYMENT_RECOVERY=PASS" in source
