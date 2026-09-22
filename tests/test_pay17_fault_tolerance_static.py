from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "docs/architecture/pay17_fault_injection_matrix_v1.json"


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_pay17_fault_matrix_covers_every_required_attack():
    matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
    assert matrix["version"] == 1
    assert matrix["phase"] == "PAY-17"
    assert matrix["predecessor_sha"] == (
        "fd619cca8234dcdc3ff75382ce8f32bde1af4c1f"
    )
    assert matrix["predecessor_tree"] == (
        "45214ab3fba96eafecbb6a3bca3ad49f8486acac"
    )
    assert matrix["alembic_head"] == "zz27d8e9f0a62"
    assert matrix["policy"] == "fail_closed"

    assert set(matrix["faults"]) == {
        "process_dies_before_db_commit",
        "process_dies_after_db_commit",
        "process_dies_before_provider_call",
        "provider_succeeds_then_process_dies",
        "provider_times_out",
        "provider_returns_malformed_response",
        "webhook_arrives_before_checkout_response",
        "duplicate_webhook",
        "100_concurrent_callbacks",
        "two_refunds_race",
        "payment_and_cancellation_race",
        "payment_and_renewal_race",
        "payment_and_expiry_race",
        "settlement_and_refund_race",
        "db_deadlock",
        "db_restart",
        "network_partition",
        "redis_loss",
        "celery_redelivery",
        "worker_sigkill",
        "lease_expiry",
        "clock_skew",
        "provider_clock_skew",
    }
    for fault in matrix["faults"].values():
        assert fault["proof"]
        assert fault["required_outcome"]
    assert matrix["terminal_marker"] == "PAY17_FAULT_TOLERANCE=PASS"
    assert set(matrix["inherited_same_head_workflows"]) == {
        ".github/workflows/p5w2-worker-crash-redelivery-pg16.yml",
        ".github/workflows/p5e-provider-ack-ambiguity-pg16.yml",
        ".github/workflows/p5d-dependency-loss-pg16.yml",
        ".github/workflows/p5r-race-deadlock-pg16.yml",
        ".github/workflows/p6b-broker-reconnect-pg16.yml",
        ".github/workflows/p6w-worker-shutdown-redelivery-pg16.yml",
    }


def test_pay17_hard_gates_are_zero_tolerance():
    matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
    assert matrix["hard_gates"] == {
        "duplicate_financial_effects": 0,
        "lost_successful_payments": 0,
        "lost_refund_obligations": 0,
        "impossible_terminal_states": 0,
        "stuck_unrecoverable_commands": 0,
        "unauthorized_entitlements": 0,
    }


def test_early_signed_webhook_is_deferred_not_dead_lettered():
    domain = _source(
        "app/finance_core/domain/provider_capture_confirmation.py"
    )
    service = _source(
        "app/finance_core/services/provider_capture_confirmation.py"
    )
    route = _source("app/finance_core/api/payment_boundary.py")

    assert "class FinanceProviderEvidenceDeferredError" in domain
    assert '"P4D provider evidence payment not found" in message' in service
    assert "FinanceProviderEvidenceDeferredError(" in service

    deferred = route.index(
        "except FinanceProviderEvidenceDeferredError"
    )
    invalid = route.index("except FinanceProviderEvidenceError as exc")
    assert deferred < invalid
    block = route[deferred:invalid]
    assert 'error_code="provider_binding_pending"' in block
    assert "retryable=True" in block
    assert 'return {"status": "queued"}' in block


def test_pay17_finance_runtime_attacks_money_boundaries():
    source = _source(
        "tests/finance_core/test_pay17_fault_injection_concurrency.py"
    )
    for test_name in (
        "test_100_concurrent_identical_callbacks_converge_to_one_financial_effect",
        "test_process_rollback_before_commit_is_reclaimable_without_duplicate_money",
        "test_death_after_claim_before_provider_call_becomes_unknown_then_reconciles_not_found",
        "test_provider_success_then_process_death_reconciles_without_second_provider_call",
        "test_webhook_before_checkout_response_is_deferred_then_recovers_payment",
        "test_settlement_and_refund_race_serializes_without_lost_obligation",
        "test_provider_clock_skew_outside_window_is_rejected_before_durable_inbox_write",
    ):
        assert f"async def {test_name}" in source

    assert "range(100)" in source
    assert "asyncio.Semaphore(20)" in source
    assert "async with db_slots" in source
    assert "await session.rollback()" in source
    assert "lease_fence == first_claim.lease_fence + 1" in source
    assert "FinanceProviderEvidenceDeferredError" in source
    assert "asyncio.gather(" in source


def test_two_refund_race_is_real_concurrent_finance_runtime():
    source = _source(
        "tests/finance_core/test_phase5j_refund_credit_note_reversal.py"
    )
    assert (
        "test_concurrent_refund_intents_serialize_on_payment_and_cannot_over_refund"
        in source
    )
    assert "results = await asyncio.gather" in source
    assert 'amount="700.00"' in source
    assert "sum(amount)" in source


def test_provider_timeout_and_invalid_response_are_reconciliation_safe():
    source = _source(
        "tests/finance_core/test_pay10_refund_provider_adapter.py"
    )
    assert (
        "test_pay10_b_provider_response_mismatch_is_unknown_and_requires_reconciliation"
        in source
    )
    assert (
        "test_pay10_b_connect_before_request_is_retryable_but_timeout_is_unknown"
        in source
    )
    assert "requires_reconciliation is True" in source
    assert "automatic_retry_allowed is False" in source
    assert "RAZORPAY_REFUND_AMOUNT_MISMATCH" in source
    assert "RAZORPAY_REFUND_CURRENCY_MISMATCH" in source
    assert "RAZORPAY_REFUND_STATUS_INVALID" in source


def test_payment_lifecycle_races_are_real_expected_version_concurrency_proofs():
    source = _source(
        "tests/platform_billing/test_pay17_payment_lifecycle_races.py"
    )
    for test_name in (
        "test_payment_and_cancellation_race_has_one_commit_then_preserves_cancellation_intent",
        "test_payment_and_renewal_race_converges_to_one_paid_renewed_period",
        "test_payment_and_expiry_race_never_loses_confirmed_payment_or_grants_from_stale_expiry",
    ):
        assert f"async def {test_name}" in source
    assert "asyncio.gather(" in source
    assert source.count("AND version=:expected_version") >= 4
    assert "version=version+1" in source


def test_payment_races_cannot_directly_overwrite_entitlement_authority():
    pay9 = _source(
        "alembic/versions/zs07d8e9f0a53_pay9_payment_application_entitlement.py"
    ).lower()
    finance_route = _source(
        "app/finance_core/api/payment_boundary.py"
    ).lower()

    assert "without making provider capture, settlement, the" in pay9
    assert "browser, redis or celery entitlement authority" in pay9
    assert "member_subscription_finance_bindings" in pay9
    assert "platform_subscriptions" not in pay9
    assert "activate_subscription" not in finance_route
    assert "deactivate_subscription" not in finance_route
    assert "entitlement_projection" not in finance_route


def test_money_worker_lease_time_comes_from_database_clock():
    pay8 = _source(
        "alembic/versions/zr07d8e9f0a52_pay8_durable_checkout_webhooks.py"
    )
    pay10 = _source(
        "alembic/versions/zu07d8e9f0a55_pay10_refund_execution_capabilities.py"
    )
    assert "pg_catalog.clock_timestamp()+interval '30 seconds'" in pay8
    assert "lease_until<=pg_catalog.clock_timestamp()" in pay8
    assert "clock_timestamp()" in pay10
    assert "lease_fence" in pay10


def test_provider_clock_skew_is_bounded_on_the_current_head():
    source = _source(
        "app/finance_core/services/razorpay_webhooks.py"
    )
    test_source = _source(
        "tests/finance_core/test_pay16_security_abuse.py"
    )
    assert "MAX_RAZORPAY_FUTURE_SKEW_SECONDS = 300" in source
    assert (
        "provider_event_timestamp > int(time.time()) "
        "+ MAX_RAZORPAY_FUTURE_SKEW_SECONDS"
        in source
    )
    assert (
        "test_webhook_rejects_future_timestamp_beyond_skew_but_allows_old_provider_retry"
        in test_source
    )


def test_inherited_destructive_workflows_are_reusable_same_head_gates():
    workflows = {
        "p5w2": ".github/workflows/p5w2-worker-crash-redelivery-pg16.yml",
        "p5e": ".github/workflows/p5e-provider-ack-ambiguity-pg16.yml",
        "p5d": ".github/workflows/p5d-dependency-loss-pg16.yml",
        "p5r": ".github/workflows/p5r-race-deadlock-pg16.yml",
        "p6b": ".github/workflows/p6b-broker-reconnect-pg16.yml",
        "p6w": ".github/workflows/p6w-worker-shutdown-redelivery-pg16.yml",
    }
    for path in workflows.values():
        source = _source(path)
        assert "workflow_call:" in source
        assert "certification_head:" in source

    p5w2_runtime = _source(
        "tests/test_p5w2_worker_crash_redelivery_runtime.py"
    ).lower()
    p5d_runtime = _source(
        "tests/test_p5d_dependency_loss_runtime.py"
    ).lower()
    p5r_runtime = _source(
        "tests/test_p5r_race_deadlock_runtime.py"
    ).lower()

    assert "sigkill" in p5w2_runtime
    assert "redelivery" in p5w2_runtime
    assert "redis" in p5d_runtime
    assert "postgres" in p5d_runtime
    assert "network" in p5d_runtime
    assert "deadlock" in p5r_runtime
    assert "lease" in p5r_runtime
    assert "finance" in p5r_runtime

def test_pay17_workflow_requires_dedicated_finance_and_platform_runtime_lanes():
    workflow = _source(
        ".github/workflows/pay17-fault-injection-concurrency.yml"
    )
    assert "finance_fault_runtime:" in workflow
    assert "platform:" in workflow
    assert "PAY17_RECON_DATABASE_URL" in workflow
    assert "pay17_recon_test" in workflow
    assert "tests/finance_core/test_pay17_fault_injection_concurrency.py" in workflow
    assert "tests/platform_billing/test_pay17_payment_lifecycle_races.py" in workflow
    assert "PAY17_FAULT_TOLERANCE=PASS" in workflow


def test_pay17_refund_crash_fixture_restores_pay16_immutable_audit_guard():
    source = _source(
        "tests/test_pay10_refund_worker_crash_recovery_runtime.py"
    )
    assert "_reset_state as _base_reset_state" in source
    assert '"test" not in str(admin.database or "").lower()' in source
    assert "DISABLE TRIGGER trg_pay16_security_audit_immutable" in source
    assert "ENABLE TRIGGER trg_pay16_security_audit_immutable" in source
    assert "finally:" in source
    assert 'assert row == ("O",)' in source


def test_pay17_closes_inherited_app_secure_inventory_over_pay16():
    source = _source(
        "tests/migration_app_secure_owner_context_boundary_baseline.py"
    )
    assert (
        'PAY16 = VERSIONS / "zz27d8e9f0a62_pay16_security_abuse_hardening.py"'
        in source
    )
    assert "PAY16.name" in source
    assert '{"grant_schema", "revoke_schema"}' in source
