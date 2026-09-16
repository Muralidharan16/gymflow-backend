from __future__ import annotations

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "docs/architecture/p9_data_protection_recovery_matrix.json"
SCOPE = ROOT / "docs/architecture/P9B_BACKUP_INTEGRITY.md"
WORKFLOW = ROOT / ".github/workflows/p9b-backup-integrity.yml"

P9_BRANCH = "hardening/p9-data-protection-disaster-recovery"
PREDECESSOR = "zj07d8e9f0a44"
HEAD = "zk07d8e9f0a45"
P9L_PARENT = "52042bf520360c076d7277ebf99a876fb11cbd94"

MARKERS = {
    "P9B_LOGICAL_CATALOG_READABLE=PASS",
    "P9B_CORRUPT_LOGICAL_BACKUP_REJECTED=PASS",
    "P9B_LOGICAL_RESTORE_FINGERPRINT=PASS",
    "P9_LOGICAL_BACKUP_INTEGRITY=PASS",
    "P9B_WAL_ARCHIVE=PASS",
    "P9B_PHYSICAL_MANIFEST_VERIFIED=PASS",
    "P9B_PHYSICAL_CLONE_STARTUP=PASS",
    "P9B_PHYSICAL_RESTORE_FINGERPRINT=PASS",
    "P9_PHYSICAL_BASE_BACKUP=PASS",
    "P9_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED",
}


def _matrix() -> dict:
    return json.loads(MATRIX.read_text(encoding="utf-8"))


def _workflow() -> dict:
    return yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_p9b_contract_freezes_required_backup_authority_without_claiming_pitr() -> None:
    contract = _matrix()["backup_integrity"]
    assert contract["logical_backup_required"] is True
    assert contract["logical_backup_checksum_required"] is True
    assert contract["logical_backup_catalog_validation_required"] is True
    assert contract["physical_base_backup_required_for_pitr"] is True
    assert contract["wal_archive_required_for_pitr"] is True
    assert contract["backup_restoreability_is_required_for_success"] is True
    assert contract["backup_creation_alone_is_not_success"] is True
    assert contract["real_customer_data_in_ci_forbidden"] is True

    source = SCOPE.read_text(encoding="utf-8")
    for token in (
        P9L_PARENT,
        PREDECESSOR,
        HEAD,
        "pg_dump --format=custom",
        "SHA-256",
        "pg_restore --list",
        "deliberately truncated copy",
        "fresh disposable database",
        "pg_basebackup --format=plain --wal-method=stream --checkpoint=fast",
        "pg_verifybackup",
        "private Unix socket",
        "listen_addresses=''",
        "P9-R remains responsible",
    ):
        assert token in source
    for marker in MARKERS:
        assert marker in source
    assert "P9_PITR=PASS" not in source
    assert "P9_RPO_MEASURED=PASS" not in source
    assert "P9_RTO_MEASURED=PASS" not in source


def test_p9b_workflow_is_exact_head_real_pg16_and_uses_only_synthetic_source() -> None:
    workflow = _workflow()
    source = WORKFLOW.read_text(encoding="utf-8")
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["on"]["push"]["branches"] == [P9_BRANCH]
    assert "workflow_call" in workflow["on"]
    assert set(workflow["jobs"]) == {"backup_integrity"}

    job = workflow["jobs"]["backup_integrity"]
    env = job["env"]
    assert env["P9B_PREDECESSOR"] == PREDECESSOR
    assert env["P9B_HEAD"] == HEAD
    assert env["P9B_SOURCE_DB"] == "gymflow_p9b_source"
    assert env["P9B_LOGICAL_RESTORE_DB"] == "gymflow_p9b_logical_restore"
    assert env["P9B_PHYSICAL_PORT"] == "55433"

    for token in (
        "ref: ${{ github.event.pull_request.head.sha || github.sha }}",
        "postgresql-16",
        "scripts/ci/bootstrap_cluster_roles.sh",
        "scripts/ci/verify_cluster_roles.sh",
        'upgrade "${P9B_PREDECESSOR}"',
        "p9m_seed_populated_predecessor.sql",
        'upgrade "${P9B_HEAD}"',
        "p9m_stable_snapshot.sql",
        "p9m_verify_head_capability.sql",
        "ALTER SYSTEM SET archive_mode = 'on'",
        "ALTER SYSTEM SET archive_command",
        "pg_switch_wal()",
        "pg_dump --format=custom",
        "sha256sum",
        "pg_restore --list",
        "truncate -s 128",
        "CREATE DATABASE ${P9B_LOGICAL_RESTORE_DB}",
        "pg_restore --exit-on-error",
        "pg_basebackup",
        "--format=plain",
        "--wal-method=stream",
        "--checkpoint=fast",
        "--manifest-checksums=SHA256",
        "pg_verifybackup",
        "backup_manifest",
        "listen_addresses = ''",
        "archive_mode=off",
        "pg_ctl",
        'cmp "$EVIDENCE_DIR/source-stable.txt" "$EVIDENCE_DIR/logical-restore-stable.txt"',
        'cmp "$EVIDENCE_DIR/source-stable.txt" "$EVIDENCE_DIR/physical-restore-stable.txt"',
        "tests/test_p9b_backup_integrity_contracts.py",
        "tests/test_p9l_lock_rewrite_contracts.py",
        "tests/test_p9m_populated_migration_contracts.py",
        "tests/test_p8_governance_contracts.py",
        "tests/test_migration_app_secure_owner_context_boundary.py",
    ):
        assert token in source

    assert "--no-owner" not in source
    assert "--no-acl" not in source
    assert "alembic downgrade" not in source
    assert "ALLOW_DESTRUCTIVE_MIGRATIONS" not in source
    assert "production data" not in source.lower()


def test_p9b_logical_backup_requires_catalog_checksum_corruption_rejection_and_restore() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    logical_block = source.split("- name: Create checksum and validate logical backup catalog", 1)[1].split(
        "- name: Restore logical backup into fresh isolated database", 1
    )[0]
    for token in (
        "pg_dump --format=custom",
        "sha256sum",
        "pg_restore --list",
        "truncate -s 128",
        "if sudo -u postgres pg_restore --list",
        "P9B_LOGICAL_CATALOG_READABLE=PASS",
        "P9B_CORRUPT_LOGICAL_BACKUP_REJECTED=PASS",
    ):
        assert token in logical_block

    restore_block = source.split("- name: Restore logical backup into fresh isolated database", 1)[1].split(
        "- name: Prove WAL archive evidence", 1
    )[0]
    assert "template0" in restore_block
    assert "pg_restore --exit-on-error" in restore_block
    assert "alembic_version" in restore_block
    assert "p9m_verify_head_capability.sql" in restore_block
    assert "P9_LOGICAL_BACKUP_INTEGRITY=PASS" in restore_block


def test_p9b_physical_backup_requires_wal_manifest_verifybackup_and_private_clone() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    for token in (
        "P9B_WAL_ARCHIVE=PASS",
        "pg_stat_archiver",
        "wal-archive.sha256",
        "pg_basebackup",
        "--manifest-checksums=SHA256",
        "pg_verifybackup",
        "physical-backup-manifest.sha256",
        "physical-backup-size.txt",
        "listen_addresses = ''",
        "local all all trust",
        "archive_mode=off",
        "P9B_PHYSICAL_CLONE_STARTUP=PASS",
        "P9B_PHYSICAL_RESTORE_FINGERPRINT=PASS",
        "P9_PHYSICAL_BASE_BACKUP=PASS",
    ):
        assert token in source


def test_p9b_wal_evidence_keeps_archive_private_and_reads_it_as_postgres() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    wal_block = source.split("- name: Prove WAL archive evidence", 1)[1].split(
        "- name: Create and verify physical base backup", 1
    )[0]
    assert 'sudo install -d -m 0700 -o postgres -g postgres "$P9B_WAL_ARCHIVE"' in source
    assert 'sudo -u postgres find "$P9B_WAL_ARCHIVE"' in wal_block
    assert "sudo -u postgres bash -c" in wal_block
    assert 'find "$1" -maxdepth 1 -type f -print0' in wal_block
    assert '| xargs -0 sha256sum' not in wal_block


def test_p9b_physical_clone_detaches_server_log_from_workflow_pipe() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    clone_block = source.split("- name: Boot private physical clone and prove restore fingerprint", 1)[1].split(
        "- name: Produce machine-readable P9-B evidence", 1
    )[0]
    assert "-l /tmp/p9b-physical-clone-postgres.log" in clone_block
    assert "sudo -u postgres test -s /tmp/p9b-physical-clone-postgres.log" in clone_block
    assert "sudo cat /tmp/p9b-physical-clone-postgres.log" in clone_block
    assert '> "$EVIDENCE_DIR/physical-clone-start.log"' in clone_block
    assert '2>&1 | tee "$EVIDENCE_DIR/physical-clone-start.log"' not in clone_block


def test_p9b_evidence_upload_excludes_backup_bytes_and_retains_machine_readable_decision() -> None:
    workflow = _workflow()
    source = WORKFLOW.read_text(encoding="utf-8")
    job = workflow["jobs"]["backup_integrity"]
    assert job["env"]["EVIDENCE_DIR"] == "p9b-evidence"
    assert "p9b-backup-integrity-${{ github.event.pull_request.head.sha || github.sha }}" in source
    assert "p9b-evidence/" in source
    assert "/tmp/p9b-logical.dump" in source
    assert "/tmp/p9b-physical-base" in source
    assert '"logical_backup_integrity": true' in source
    assert '"physical_base_backup": true' in source
    assert '"refund_provider_execution": "deferred_fail_closed"' in source
    for marker in MARKERS:
        assert marker in source
