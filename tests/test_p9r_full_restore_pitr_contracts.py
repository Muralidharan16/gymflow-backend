from __future__ import annotations

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "docs/architecture/p9_data_protection_recovery_matrix.json"
SCOPE = ROOT / "docs/architecture/P9R_FULL_RESTORE_PITR.md"
WORKFLOW = ROOT / ".github/workflows/p9r-full-restore-pitr.yml"

P9_BRANCH = "hardening/p9-data-protection-disaster-recovery"
P9B_PARENT = "75ba68db61b244b32f6d7f1b58f4bf53b1e3e0a2"
PREDECESSOR = "zj07d8e9f0a44"
HEAD = "zk07d8e9f0a45"
RPO_TARGET_MS = "10000"
RTO_TARGET_MS = "120000"

MARKERS = {
    "P9R_FULL_RESTORE_FINGERPRINT=PASS",
    "P9R_FULL_RESTORE_APP_READINESS=PASS",
    "P9R_LIVE_PROVIDER_EGRESS_BLOCKED=PASS",
    "P9R_PRE_TARGET_SURVIVES=PASS",
    "P9R_POST_TARGET_EXCLUDED=PASS",
    "P9R_PITR_APP_READINESS=PASS",
    "P9_FULL_RESTORE=PASS",
    "P9_PITR=PASS",
    "P9_RPO_MEASURED=PASS",
    "P9_RTO_MEASURED=PASS",
    "P9_DATA_LOSS_RECOVERY=PASS",
    "P9_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED",
}


def _matrix() -> dict:
    return json.loads(MATRIX.read_text(encoding="utf-8"))


def _workflow() -> dict:
    return yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_p9r_contract_freezes_numeric_ci_recovery_targets_before_certification() -> None:
    matrix = _matrix()
    restore = matrix["restore_and_pitr"]
    objectives = matrix["recovery_objectives"]

    assert restore["full_restore_into_isolated_cluster_required"] is True
    assert restore["restored_schema_validation_required"] is True
    assert restore["restored_business_invariant_validation_required"] is True
    assert restore["restored_security_rls_acl_validation_required"] is True
    assert restore["restored_application_readiness_required"] is True
    assert restore["restored_live_provider_egress_must_be_blocked"] is True
    assert restore["point_in_time_recovery_required"] is True
    assert restore["pre_loss_durable_record_must_survive"] is True
    assert restore["post_target_record_must_not_survive"] is True
    assert restore["recovery_timeline_evidence_required"] is True

    assert objectives["rpo_must_be_measured"] is True
    assert objectives["rto_must_be_measured"] is True
    assert objectives["measurement_clock_must_be_monotonic_for_duration"] is True
    assert objectives["targets_must_be_frozen_before_p9_r_certification"] is True
    assert objectives["measured_values_must_be_recorded_as_evidence"] is True
    assert objectives["synthetic_ci_rpo_target_ms"] == int(RPO_TARGET_MS)
    assert objectives["synthetic_ci_rto_target_ms"] == int(RTO_TARGET_MS)
    assert objectives["target_scope"] == "p9_r_synthetic_ci_certification_only_not_production_sla"


def test_p9r_scope_binds_exact_p9b_parent_and_recovery_authority() -> None:
    source = SCOPE.read_text(encoding="utf-8")
    for token in (
        P9B_PARENT,
        PREDECESSOR,
        HEAD,
        "10,000 ms",
        "120,000 ms",
        "not production or customer SLAs",
        "pg_basebackup --format=plain --wal-method=stream --checkpoint=fast",
        "pg_verifybackup",
        "pre_target",
        "post_target",
        "p9r_target",
        "recovery.signal",
        "recovery_target_name = 'p9r_target'",
        "time.monotonic_ns()",
        "/_system/ready",
        "env -i",
        "firewall",
        "deferred_fail_closed",
    ):
        assert token in source
    for marker in MARKERS:
        assert marker in source
    assert "P9_ROLLING_SCHEMA_COMPATIBILITY=PASS" not in source
    assert "P9_DEPLOYMENT_ROLLBACK=PASS" not in source
    assert "P9F_FINAL_SAME_HEAD_CERTIFICATION=PASS" not in source


def test_p9r_workflow_is_exact_head_real_pg16_private_and_synthetic() -> None:
    workflow = _workflow()
    source = WORKFLOW.read_text(encoding="utf-8")

    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["on"]["push"]["branches"] == [P9_BRANCH]
    assert "workflow_call" in workflow["on"]
    assert set(workflow["jobs"]) == {"recovery_rehearsal"}

    job = workflow["jobs"]["recovery_rehearsal"]
    env = job["env"]
    assert env["P9R_PREDECESSOR"] == PREDECESSOR
    assert env["P9R_HEAD"] == HEAD
    assert env["P9R_RPO_TARGET_MS"] == RPO_TARGET_MS
    assert env["P9R_RTO_TARGET_MS"] == RTO_TARGET_MS

    for token in (
        "ref: ${{ github.event.pull_request.head.sha || github.sha }}",
        "postgresql-16",
        "scripts/ci/bootstrap_cluster_roles.sh",
        "scripts/ci/verify_cluster_roles.sh",
        'upgrade "${P9R_PREDECESSOR}"',
        "p9m_seed_populated_predecessor.sql",
        'upgrade "${P9R_HEAD}"',
        "p9m_stable_snapshot.sql",
        "p9m_verify_head_capability.sql",
        "pg_basebackup",
        "--format=plain",
        "--wal-method=stream",
        "--checkpoint=fast",
        "--manifest-checksums=SHA256",
        "pg_verifybackup",
        "listen_addresses = ''",
        "p9r_ci.recovery_sentinels",
        "pg_create_restore_point('p9r_target')",
        "TRUNCATE p9r_ci.recovery_sentinels",
        "recovery.signal",
        "recovery_target_name = 'p9r_target'",
        "restore_command = 'cp /tmp/p9r-wal-archive/%f %p'",
        "time.monotonic_ns()",
        "/_system/ready",
        "sudo iptables -I OUTPUT",
        "sudo -u p9rapp /usr/bin/curl",
        "NOTIFICATION_EMAIL_PROVIDER_MODE=disabled",
        "SEARCH_PROVIDER_MODE=disabled",
        "PLATFORM_BILLING_PROVIDER_MODE=disabled",
        "tests/test_p9r_full_restore_pitr_contracts.py",
    ):
        assert token in source

    assert "alembic downgrade" not in source
    assert "ALLOW_DESTRUCTIVE_MIGRATIONS" not in source
    assert "production data" not in source.lower()
    assert "customer data" not in source.lower()


def test_p9r_full_restore_requires_fingerprint_capability_and_real_app_readiness() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    block = source.split("- name: Prove full restore business security and application readiness", 1)[1].split(
        "- name: Build destructive PITR timeline", 1
    )[0]

    for token in (
        'cmp "$EVIDENCE_DIR/source-stable.txt" "$EVIDENCE_DIR/full-restore-stable.txt"',
        "P9M_EXPECTED_CAPABILITY_DELTA=PASS",
        "SHOW listen_addresses",
        "/_system/ready",
        "P9R_FULL_RESTORE_FINGERPRINT=PASS",
        "P9R_FULL_RESTORE_APP_READINESS=PASS",
        "P9R_LIVE_PROVIDER_EGRESS_BLOCKED=PASS",
    ):
        assert token in block


def test_p9r_pitr_requires_named_target_and_pre_post_boundary() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    timeline = source.split("- name: Build destructive PITR timeline", 1)[1].split(
        "- name: Recover to named PITR target and measure RTO", 1
    )[0]
    recovery = source.split("- name: Recover to named PITR target and measure RTO", 1)[1].split(
        "- name: Produce machine-readable P9-R decision", 1
    )[0]

    for token in (
        "'pre_target'",
        "pg_create_restore_point('p9r_target')",
        "'post_target'",
        "TRUNCATE p9r_ci.recovery_sentinels",
        "P9R_RPO_TARGET_MS",
    ):
        assert token in timeline

    for token in (
        "recovery.signal",
        "recovery_target_name = 'p9r_target'",
        "recovery_target_action = 'promote'",
        "marker = 'pre_target'",
        "marker = 'post_target'",
        "P9R_PRE_TARGET_SURVIVES=PASS",
        "P9R_POST_TARGET_EXCLUDED=PASS",
        "P9R_RTO_TARGET_MS",
        "P9R_PITR_APP_READINESS=PASS",
    ):
        assert token in recovery


def test_p9r_provider_isolation_is_process_allowlist_plus_owner_firewall() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    block = source.split("- name: Prove full restore business security and application readiness", 1)[1].split(
        "- name: Build destructive PITR timeline", 1
    )[0]
    assert "env -i" in block
    assert "sudo iptables -I OUTPUT" in block
    assert "! -d 127.0.0.0/8 -j REJECT" in block
    assert "sudo -u p9rapp /usr/bin/curl" in block
    assert "NOTIFICATION_EMAIL_PROVIDER_MODE=disabled" in block
    assert "SEARCH_PROVIDER_MODE=disabled" in block
    assert "PLATFORM_BILLING_PROVIDER_MODE=disabled" in block


def test_p9r_app_workdir_is_entered_only_after_switching_to_p9rapp() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    assert "sudo install -d -m 0750 -o p9rapp -g p9rapp /tmp/p9r-app-work /tmp/p9r-app-home" in source
    safe_launcher = "/bin/bash -c 'cd /tmp/p9r-app-work && exec \"$PYTHON_BIN\" -m uvicorn app.main:app --host 127.0.0.1 --port \"$P9R_APP_PORT\"'"
    assert source.count(safe_launcher) == 2
    assert "\n            cd /tmp/p9r-app-work\n            exec sudo -u p9rapp env -i" not in source
    assert source.count("exec sudo -u p9rapp env -i") == 2


def test_p9r_app_runtime_is_staged_privately_instead_of_exposing_workspace() -> None:
    workflow = _workflow()
    source = WORKFLOW.read_text(encoding="utf-8")
    env = workflow["jobs"]["recovery_rehearsal"]["env"]
    assert env["P9R_APP_RUNTIME"] == "/tmp/p9r-app-runtime"
    assert 'sudo install -d -m 0750 -o p9rapp -g p9rapp "$P9R_APP_RUNTIME"' in source
    assert 'sudo cp -a "$GITHUB_WORKSPACE/app" "$GITHUB_WORKSPACE/security" "$P9R_APP_RUNTIME/"' in source
    assert 'sudo chown -R p9rapp:p9rapp "$P9R_APP_RUNTIME"' in source
    assert 'sudo chmod -R u=rwX,g=rX,o= "$P9R_APP_RUNTIME"' in source
    assert 'security/runtime_identity/process_profiles.v1.json' in source
    assert 'security/cluster_role_bootstrap/roles.v1.json' in source
    assert source.count('PYTHONPATH="$P9R_APP_RUNTIME"') == 2
    assert 'PYTHONPATH="$GITHUB_WORKSPACE"' not in source
    assert "chmod 0755" not in source
    assert "chmod -R 0755" not in source


def test_p9r_decision_and_artifact_keep_backup_bytes_ephemeral() -> None:
    workflow = _workflow()
    source = WORKFLOW.read_text(encoding="utf-8")
    job = workflow["jobs"]["recovery_rehearsal"]
    assert job["env"]["EVIDENCE_DIR"] == "p9r-evidence"
    assert "p9r-recovery-${{ github.event.pull_request.head.sha || github.sha }}" in source

    decision = source.split("- name: Produce machine-readable P9-R decision", 1)[1].split(
        "- name: Reprove inherited P9 and P8 boundaries", 1
    )[0]
    for token in (
        '"full_restore": True',
        '"pitr": True',
        '"rpo_target_ms"',
        '"measured_rpo_ms"',
        '"rto_target_ms"',
        '"measured_rto_ms"',
        '"provider_egress_blocked": True',
        '"refund_provider_execution": "deferred_fail_closed"',
    ):
        assert token in decision

    upload = source.split("- name: Upload P9-R recovery evidence", 1)[1]
    assert "path: p9r-evidence/" in upload
    assert "/tmp/p9r-physical-base" not in upload
    assert "/tmp/p9r-wal-archive" not in upload
