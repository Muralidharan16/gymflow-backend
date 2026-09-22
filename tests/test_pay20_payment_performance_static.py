from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "docs/architecture/pay20_payment_performance_contract_v1.json"
DOC = ROOT / "docs/architecture/PAY20_PAYMENT_PERFORMANCE.md"
HARNESS = ROOT / "scripts/ci/pay20_finance_performance.py"
WORKFLOW = ROOT / ".github/workflows/pay20-payment-performance.yml"


def contract() -> dict:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def test_pay20_exact_predecessor_and_unchanged_migration_head() -> None:
    c = contract()
    assert c["phase"] == "PAY-20"
    assert c["predecessor"] == {
        "phase": "PAY-19",
        "sha": "39d4bcb486f797dbabcb1ea934db29db6ea8c012",
        "tree": "61cce864dd6208451f79b68318507802b5a53313",
        "alembic_head": "zz37d8e9f0a63",
    }
    assert c["alembic_head"] == "zz37d8e9f0a63"
    assert c["no_new_money_authority"] is True
    assert c["terminal_marker"] == "PAY20_PAYMENT_PERFORMANCE=PASS"


def test_pay20_covers_every_required_financial_surface() -> None:
    assert set(contract()["load_surfaces"]) == {
        "checkout",
        "webhooks",
        "payment_application",
        "ledger_posting",
        "invoice_generation",
        "subscription_activation",
        "refund_creation",
        "settlement_reconciliation",
    }


def test_pay20_requires_hot_account_and_multi_tenant_pressure() -> None:
    c = contract()
    assert "invoice_series_hot_lock" in c["contention"]
    assert "same_subscription_activation_two_contenders" in c["contention"]
    assert "multi_tenant_http_8_tenants" in c["contention"]
    assert c["production_container_lane"]["tenants"] == 8
    assert c["production_container_lane"]["load_concurrency"] == 24


def test_pay20_finance_soak_is_five_minutes_and_samples_required_pressure() -> None:
    c = contract()
    assert c["finance_soak"]["duration_seconds"] >= 300
    assert c["finance_soak"]["sample_interval_seconds"] <= 5
    tracked = set(c["finance_soak_tracks"])
    assert {
        "database_deadlocks",
        "database_connections",
        "process_rss",
        "redis_memory",
        "redis_rejected_connections",
        "webhook_latency",
        "finance_outbox_backlog",
        "oldest_finance_outbox_age",
        "payment_unknown_total",
        "reconciliation_open_total",
        "settlement_reconciliation_latency",
    } <= tracked


def test_pay20_inherits_frozen_p10_budgets_instead_of_loosening_them() -> None:
    c = contract()
    inherited = c["inherited_p10_budgets"]
    assert inherited["artifact"] == "docs/architecture/p10_performance_budgets.v1.json"
    assert inherited["digest_file"] == "docs/architecture/p10_performance_budgets.v1.sha256"
    assert inherited["apply_write_p95_to_finance_operations"] is True
    assert inherited["apply_write_p99_to_finance_operations"] is True

    source = HARNESS.read_text(encoding="utf-8")
    assert "p10_performance_budgets.v1.json" in source
    assert 'max_write_p95_ms' in source
    assert 'max_write_p99_ms' in source
    assert "candidate_rate < base_rate * 0.60" in source
    assert "float(summary[\"p95_ms\"]) > base_p95 * 2.0" in source


def test_finance_harness_uses_certified_real_finance_paths_and_no_live_provider() -> None:
    source = HARNESS.read_text(encoding="utf-8")
    for token in (
        "orchestrate(",
        "record_verified_webhook(",
        "process_claimed_webhook(",
        "complete_claimed_webhook(",
        "apply_gate(",
        "reconcile_payment(",
        "create_refund_intent(",
        "issued_invoice(",
        "pay5._claim(",
        "pay5._consume(",
        "pay5._ack(",
        "Pay20RazorpayClient",
    ):
        assert token in source

    assert '"live_provider": False' in source
    assert "rzp_live_" not in source
    assert "import requests" not in source
    assert "from requests" not in source
    assert "import httpx" not in source
    assert "from httpx" not in source


def test_finance_harness_hard_fails_on_correctness_drift() -> None:
    source = HARNESS.read_text(encoding="utf-8")
    for token in (
        "unbalanced_posted_ledgers",
        "duplicate_provider_payment_refs",
        "duplicate_invoice_numbers",
        "unknown_payments",
        "unexpected PostgreSQL deadlocks",
        "payment unknown state appeared under load",
        "open accounting reconciliation appeared under load",
        "subscription exact-once drift",
        "provider-call cardinality drift",
    ):
        assert token in source


def test_workflow_requires_real_pg16_redis_multi_tenant_and_two_soaks() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    for token in (
        "scripts/ci/install_pg16_test_stack.sh",
        "redis:7-alpine",
        "pay20_finance_performance.py",
        "--mode calibration",
        "--mode load",
        "--mode soak",
        "--duration-seconds 300",
        "run_p10b_baseline_calibration.sh",
        "p10l_verify_representative_load.py",
        "run_p10s_soak.sh",
        "P10_SOAK=PASS",
        "tests/finance_core/test_pay17_fault_injection_concurrency.py",
        "scripts/ci/pay20_prepare_system_pg16.sh",
        "tests/platform_billing/test_pay17_payment_lifecycle_races.py",
    ):
        assert token in source


def test_pay20_terminal_gate_keeps_money_and_release_safety_locked() -> None:
    c = contract()
    assert c["safety"] == {
        "synthetic_only": True,
        "live_provider": "disabled",
        "production_credentials": "disabled",
        "live_money_movement": "disabled",
        "production_runtime_binding": "disabled",
        "merge": "not_authorized",
        "release": "not_authorized",
        "deployment": "not_authorized",
    }
    workflow = WORKFLOW.read_text(encoding="utf-8")
    for marker in (
        "PAY20_FINANCE_CALIBRATION=PASS",
        "PAY20_FINANCE_LOAD=PASS",
        "PAY20_FINANCE_SOAK=PASS",
        "PAY20_MULTI_TENANT_LOAD=PASS",
        "PAY20_SYSTEM_SOAK=PASS",
        "PAY20_PAYMENT_CORRECTNESS_UNDER_LOAD=PASS",
        "PAY20_PAYMENT_PERFORMANCE=PASS",
        "PAY20_LIVE_PROVIDER=DISABLED",
        "PAY20_PRODUCTION_CREDENTIALS=DISABLED",
        "PAY20_LIVE_MONEY_MOVEMENT=DISABLED",
        "PAY20_MERGE=NOT_AUTHORIZED",
        "PAY20_RELEASE=NOT_AUTHORIZED",
        "PAY20_DEPLOYMENT=NOT_AUTHORIZED",
    ):
        assert marker in workflow


def test_pay20_document_forbids_correctness_weakening_for_performance() -> None:
    text = DOC.read_text(encoding="utf-8")
    assert "Performance failure may never disable RLS" in text
    assert "exactly one activation" in text
    assert "five-minute finance soak" in text.lower()
    assert "production-container" in text



def test_pay20_system_setup_is_current_head_no_migration_setup_not_risk_bypass() -> None:
    source = (ROOT / "scripts/ci/pay20_prepare_system_pg16.sh").read_text(
        encoding="utf-8"
    )
    assert "scripts/ci/install_pg16_test_stack.sh" in source
    assert "scripts/ci/bootstrap_cluster_roles.sh" in source
    assert "scripts/verify_alembic_graph.py" in source
    assert 'python -s -m alembic -c alembic.ini upgrade head' in source
    assert 'PAY20_SYSTEM_PG16_READY=PASS' in source
    assert "migration_semantics_gate.py" not in source

    # PAY-20 exact scope contains no Alembic revision, so bypassing a migration
    # scan is structurally impossible; this helper only prepares current-head
    # runtime databases for load.
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert '"scripts/ci/pay20_prepare_system_pg16.sh"' in workflow
    assert "bash scripts/ci/pay20_prepare_system_pg16.sh" in workflow
    assert "alembic/versions/" not in {
        line.strip()
        for line in workflow.splitlines()
        if line.strip().startswith('"')
    }
