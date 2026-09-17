from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/p9r-full-restore-pitr.yml"


def test_p9r_recovery_uses_source_compatible_settings_and_preserves_startup_diagnostics() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    source_capture = source.split("- name: Create recovery-only sentinel boundary and source fingerprint", 1)[1].split(
        "- name: Create and verify physical recovery base backup", 1
    )[0]
    full = source.split("- name: Boot isolated full restore", 1)[1].split(
        "- name: Prove full restore business security and application readiness", 1
    )[0]
    recovery = source.split("- name: Recover to named PITR target and measure RTO", 1)[1].split(
        "- name: Produce machine-readable P9-R decision", 1
    )[0]
    decision = source.split("- name: Produce machine-readable P9-R decision", 1)[1].split(
        "- name: Reprove inherited P9 and P8 boundaries", 1
    )[0]

    assert "P9R_SOURCE_RECOVERY_COMPATIBILITY_CAPTURE=PASS" in source_capture
    assert "source-recovery-compatible-settings.txt" in source_capture
    for setting in (
        "max_connections",
        "max_prepared_transactions",
        "max_locks_per_transaction",
        "max_wal_senders",
        "max_worker_processes",
    ):
        assert f"current_setting('{setting}')" in source_capture

    for token in (
        "P9R_SOURCE_MAX_CONNECTIONS",
        "P9R_SOURCE_MAX_PREPARED_TRANSACTIONS",
        "P9R_SOURCE_MAX_LOCKS_PER_TRANSACTION",
        "P9R_SOURCE_MAX_WAL_SENDERS",
        "P9R_SOURCE_MAX_WORKER_PROCESSES",
    ):
        assert token in source_capture
        assert token in full
        assert token in recovery

    assert "max_connections = 50" not in full
    assert "max_connections = 50" not in recovery
    assert "full-recovery-compatible-settings.txt" in full
    assert 'cmp "$EVIDENCE_DIR/source-recovery-compatible-settings.txt" "$EVIDENCE_DIR/full-recovery-compatible-settings.txt"' in full
    assert "P9R_FULL_RECOVERY_COMPATIBLE_SETTINGS=PASS" in full

    assert "pitr-recovery-compatible-settings.txt" in recovery
    assert 'cmp "$EVIDENCE_DIR/source-recovery-compatible-settings.txt" "$EVIDENCE_DIR/pitr-recovery-compatible-settings.txt"' in recovery
    assert "P9R_PITR_RECOVERY_COMPATIBLE_SETTINGS=PASS" in recovery
    assert "if ! sudo -u postgres /usr/lib/postgresql/16/bin/pg_ctl" in recovery
    assert "pitr-start-failure.log" in recovery
    assert "sudo cat /tmp/p9r-pitr-postgres.log" in recovery

    # The compatibility repair must not weaken PITR semantics.
    assert "recovery_target_name = 'p9r_target'" in recovery
    assert "recovery_target_action = 'promote'" in recovery
    assert "restore_command = 'cp /tmp/p9r-wal-archive/%f %p'" in recovery
    assert '"recovery_compatible_settings": True' in decision
    assert '"recovery_compatible_settings": true' in decision
