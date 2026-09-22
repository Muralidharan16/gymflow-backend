from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "docs/architecture/pay19_financial_recovery_contract_v1.json"
DOC = ROOT / "docs/architecture/PAY19_FINANCIAL_RECOVERY.md"
SEED = ROOT / "scripts/ci/pay19_seed_financial_state.py"
FINGERPRINT = ROOT / "scripts/ci/pay19_financial_fingerprint.sql"
REPLAY = ROOT / "scripts/ci/pay19_recovery_replay_guards.py"
WORKFLOW = ROOT / ".github/workflows/pay19-financial-recovery.yml"

REQUIRED_PROOFS = {
    "checksummed_logical_backup_catalog_readable_and_restoreable",
    "verified_physical_base_backup",
    "wal_archive_present",
    "full_physical_restore",
    "named_restore_point_pitr_with_pre_target_survival_and_post_target_exclusion",
    "finance_table_integrity",
    "invoice_numbering_integrity",
    "ledger_balance_integrity",
    "provider_reference_integrity",
    "refund_command_integrity",
    "idempotency_integrity",
    "outbox_integrity",
    "subscription_binding_integrity",
    "payment_application_integrity",
    "provider_reconciliation_after_pitr",
}


def _contract() -> dict:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def test_pay19_is_exactly_stacked_on_frozen_pay18_without_new_migration() -> None:
    contract = _contract()
    assert contract["phase"] == "PAY-19"
    assert contract["predecessor"] == {
        "phase": "PAY-18",
        "sha": "5de9b1f0997c4e5a6df5af7a2acf5ba1f1df1eee",
        "tree": "f17dbc3316a15b7a91b4deb877ce89df67ed75d7",
        "alembic_head": "zz37d8e9f0a63",
    }
    assert contract["alembic_head"] == "zz37d8e9f0a63"
    assert contract["terminal_marker"] == "PAY19_FINANCIAL_RECOVERY=PASS"


def test_pay19_contract_covers_every_required_recovery_and_integrity_gate() -> None:
    contract = _contract()
    assert set(contract["required_proofs"]) == REQUIRED_PROOFS
    assert set(contract["post_restore_zero_duplicate_gates"]) == {
        "duplicate_provider_operation",
        "reused_invoice_number",
        "repeated_refund",
        "repeated_payment_application",
    }
    assert contract["authority"]["durable_financial_truth"] == "postgresql"
    assert contract["authority"]["backup_artifact_is_business_authority"] is False
    assert contract["authority"]["restored_environment_live_provider_access"] == "forbidden"
    assert contract["authority"]["production_data_in_ci"] == "forbidden"
    assert contract["reconciliation"]["exact_recovered_database_required"] is True
    assert contract["reconciliation"]["second_provider_call_allowed"] is False


def test_financial_fingerprint_explicitly_checks_all_money_integrity_domains() -> None:
    source = FINGERPRINT.read_text(encoding="utf-8")
    for token in (
        "finance.invoices",
        "finance.invoice_series",
        "finance.ledger_entries",
        "finance.ledger_entry_lines",
        "finance.provider_operations",
        "finance.payments",
        "finance.refund_execution_commands",
        "finance.idempotency_keys",
        "finance.monetary_commands",
        "finance.outbox_events",
        "finance.member_subscription_checkout_bindings",
        "finance.payment_application_records",
        "public.member_subscriptions_v2",
    ):
        assert token in source

    for invariant in (
        "invoice_integrity=",
        "ledger_integrity=",
        "provider_integrity=",
        "refund_integrity=",
        "idempotency_integrity=",
        "outbox_integrity=",
        "subscription_binding_integrity=",
        "payment_application_integrity=",
        "stable_digest=",
    ):
        assert invariant in source

    assert "sum(l.debit_amount) <> sum(l.credit_amount)" in source
    assert "official_invoice_number='VS/2425/00001'" in source
    assert "provider_object_id='order_test_1'" in source


def test_seed_uses_certified_services_and_real_refund_subscription_capabilities() -> None:
    source = SEED.read_text(encoding="utf-8")
    for token in (
        "orchestrate(command(idempotency_key=\"pay19-checkout\")",
        "event_type=\"payment.captured\"",
        "apply_gate(",
        "_record_member_subscription_checkout_binding(",
        "_allocate(amount=\"80.00\")",
        "_resolve()",
        "command_materialized",
    ):
        assert token in source
    assert '"live_provider": False' in source
    assert "PAY19_SYNTHETIC_FINANCIAL_STATE=PASS" in source


def test_post_restore_replay_guards_require_zero_duplicate_money_effects() -> None:
    source = REPLAY.read_text(encoding="utf-8")
    assert "if client.requests:" in source
    assert "duplicate provider operation" in source
    assert "repeated payment application record" in source
    assert "repeated payment allocation" in source
    assert "repeated payment application ledger effect" in source
    assert "repeated refund execution command" in source
    assert 'official_invoice_number != "VS/2425/00002"' in source
    assert "PAY19_NO_DUPLICATE_PROVIDER_OPERATION=PASS" in source
    assert "PAY19_NO_REUSED_INVOICE_NUMBER=PASS" in source
    assert "PAY19_NO_REPEATED_REFUND=PASS" in source
    assert "PAY19_NO_REPEATED_PAYMENT_APPLICATION=PASS" in source


def test_workflow_requires_real_pg16_backup_restore_pitr_and_reconciliation() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    for token in (
        "scripts/ci/install_pg16_test_stack.sh",
        "/usr/lib/postgresql/16/bin/pg_basebackup",
        "/usr/lib/postgresql/16/bin/pg_verifybackup",
        "pg_dump --format=custom",
        "pg_restore --list",
        "pg_basebackup",
        "pg_verifybackup",
        "archive_mode = 'on'",
        "archive_command",
        "pg_create_restore_point('pay19_financial_target')",
        "recovery_target_name = 'pay19_financial_target'",
        "recovery_target_action = 'promote'",
        "pre_target",
        "post_target",
        "pay19_financial_fingerprint.sql",
        "cmp \"$EVIDENCE_DIR/source-finance.txt\"",
        "pay19_recovery_replay_guards.py",
        "test_provider_success_then_process_death_reconciles_without_second_provider_call",
    ):
        assert token in source

    # Catastrophic post-target corruption must affect real Finance authority,
    # not only a throwaway sentinel table.
    for token in (
        "UPDATE finance.invoice_series",
        "DELETE FROM finance.payment_application_records",
        "DELETE FROM finance.refund_execution_commands",
    ):
        assert token in source


def test_workflow_never_injects_live_provider_credentials_or_authority() -> None:
    source = WORKFLOW.read_text(encoding="utf-8").lower()
    forbidden = (
        "rzp_live_",
        "live_provider_enabled=true",
        "finance_live_provider_enabled=true",
        "production_credentials=enabled",
    )
    for token in forbidden:
        assert token not in source

    contract = _contract()
    assert contract["safety"] == {
        "live_provider": "disabled",
        "production_credentials": "disabled",
        "live_money_movement": "disabled",
        "production_runtime_binding": "disabled",
        "merge": "not_authorized",
        "release": "not_authorized",
        "deployment": "not_authorized",
    }


def test_document_freezes_accounting_reconstruction_and_reconciliation_semantics() -> None:
    text = DOC.read_text(encoding="utf-8").lower()
    for phrase in (
        "logical restore",
        "physical restore",
        "pitr",
        "invoice-series cursor",
        "balanced posted payment-allocation ledger entry",
        "member-subscription checkout binding",
        "materialized refund execution command",
        "zero",
        "provider reconciliation",
        "pay19_financial_recovery=pass",
    ):
        assert phrase in text
