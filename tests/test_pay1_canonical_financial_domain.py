from decimal import Decimal
import json
from pathlib import Path

import pytest

from app.finance_core.domain.canonical_financial_model import (
    ADJUSTMENT_TRANSITIONS,
    ALLOCATION_TRANSITIONS,
    CHARGEBACK_TRANSITIONS,
    CREDIT_NOTE_TRANSITIONS,
    CURRENT_INVOICE_STATUS_COMPATIBILITY,
    CURRENT_PAYMENT_STATUS_COMPATIBILITY,
    CURRENT_PLATFORM_PROVIDER_OPERATION_COMPATIBILITY,
    DISPUTE_TRANSITIONS,
    INVOICE_TRANSITIONS,
    PAYMENT_ATTEMPT_TRANSITIONS,
    PAYMENT_TRANSITIONS,
    PAY1_HARD_INVARIANTS,
    PROVIDER_OPERATION_TRANSITIONS,
    REFUND_EXECUTION_TRANSITIONS,
    REFUND_TRANSITIONS,
    SETTLEMENT_TRANSITIONS,
    AdjustmentStatus,
    AllocationStatus,
    ChargebackStatus,
    CreditNoteStatus,
    DisputeStatus,
    InvalidFinancialTransition,
    InvoiceStatus,
    Money,
    MoneyValidationError,
    PaymentAttemptStatus,
    PaymentStatus,
    ProviderOperationStatus,
    RefundExecutionStatus,
    RefundStatus,
    SettlementStatus,
    TransitionAction,
    transition_action,
)

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "docs/architecture/pay1_canonical_financial_domain_v1.json"
SCOPE = ROOT / "docs/architecture/PAY1_CANONICAL_FINANCIAL_DOMAIN_MODEL.md"
ACCEPTANCE = ROOT / "docs/architecture/PAY1_ACCEPTANCE_MATRIX.md"
WORKFLOW = ROOT / ".github/workflows/pay1-canonical-financial-domain.yml"

BASE_SHA = "a9fa1c521bcd1685ee822d332eeaf87c7cf5554a"
BASE_TREE = "724671aecdcc2d322c405855a26d93be1cd8968c"
BRANCH = "hardening/pay1-canonical-financial-domain-model"
ALEMBIC_HEAD = "zk07d8e9f0a45"


def _contract():
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def test_pay1_exact_certified_pay0_baseline_is_frozen():
    data = _contract()
    assert data["phase"] == "PAY-1"
    assert data["branch"] == BRANCH
    assert data["inherited_pay0"]["commit"] == BASE_SHA
    assert data["inherited_pay0"]["tree"] == BASE_TREE
    assert data["inherited_pay0"]["alembic_head"] == ALEMBIC_HEAD
    assert data["inherited_pay0"]["certification"] == "PAY0_FINAL=PASS"


def test_payment_acquisition_is_separate_from_refund_and_settlement():
    assert {item.value for item in PaymentStatus} == {
        "created", "pending", "requires_action", "authorized", "captured",
        "failed", "cancelled", "expired",
    }
    assert "settled" not in {item.value for item in PaymentStatus}
    assert "refunded" not in {item.value for item in PaymentStatus}
    assert "partially_refunded" not in {item.value for item in PaymentStatus}
    assert "disputed" not in {item.value for item in PaymentStatus}
    assert "charged_back" not in {item.value for item in PaymentStatus}
    assert PAYMENT_TRANSITIONS[PaymentStatus.CAPTURED] == frozenset()


def test_payment_transition_graph_rejects_backward_or_cross_machine_mutation():
    assert transition_action(
        PaymentStatus.PENDING, PaymentStatus.CAPTURED, PAYMENT_TRANSITIONS
    ) == TransitionAction.APPLY
    assert transition_action(
        PaymentStatus.CAPTURED, PaymentStatus.CAPTURED, PAYMENT_TRANSITIONS
    ) == TransitionAction.NOOP
    with pytest.raises(InvalidFinancialTransition):
        transition_action(
            PaymentStatus.CAPTURED, PaymentStatus.PENDING, PAYMENT_TRANSITIONS
        )
    with pytest.raises(InvalidFinancialTransition):
        transition_action(
            PaymentStatus.PENDING, InvoiceStatus.PAID, PAYMENT_TRANSITIONS
        )


def test_unknown_attempt_requires_evidence_resolution_not_blind_retry():
    allowed = PAYMENT_ATTEMPT_TRANSITIONS[PaymentAttemptStatus.UNKNOWN]
    assert PaymentAttemptStatus.SUBMITTING not in allowed
    assert PaymentAttemptStatus.CAPTURED in allowed
    assert PaymentAttemptStatus.FAILED_FINAL in allowed
    assert PAYMENT_ATTEMPT_TRANSITIONS[PaymentAttemptStatus.FAILED_RETRYABLE] == frozenset()


def test_invoice_overdue_is_derived_not_lifecycle_state():
    assert "overdue" not in {item.value for item in InvoiceStatus}
    target, mode = CURRENT_INVOICE_STATUS_COMPATIBILITY["overdue"]
    assert target is None
    assert mode == "derive_due_state_from_due_date_and_outstanding_balance"


def test_issued_document_and_credit_note_terminal_contracts():
    assert InvoiceStatus.VOIDED not in INVOICE_TRANSITIONS[InvoiceStatus.ISSUED]
    assert CREDIT_NOTE_TRANSITIONS[CreditNoteStatus.ISSUED] == frozenset()
    assert CREDIT_NOTE_TRANSITIONS[CreditNoteStatus.VOIDED] == frozenset()


def test_allocation_history_is_append_only_with_bounded_reversal_states():
    assert ALLOCATION_TRANSITIONS[AllocationStatus.APPLIED] == frozenset({
        AllocationStatus.PARTIALLY_REVERSED,
        AllocationStatus.REVERSED,
    })
    assert ALLOCATION_TRANSITIONS[AllocationStatus.REVERSED] == frozenset()


def test_settlement_reversal_is_a_new_record_not_a_status_rewrite():
    assert "reversed" not in {item.value for item in SettlementStatus}
    assert SETTLEMENT_TRANSITIONS[SettlementStatus.RECONCILED] == frozenset()


def test_refund_business_state_is_separate_from_execution_state():
    assert "unknown" not in {item.value for item in RefundStatus}
    assert RefundExecutionStatus.UNKNOWN.value == "unknown"
    assert RefundExecutionStatus.RETRY_PENDING.value == "retry_pending"
    assert RefundExecutionStatus.PROVIDER_ACCEPTED.value == "provider_accepted"
    assert RefundStatus.PROCESSING in REFUND_TRANSITIONS[RefundStatus.APPROVED]
    assert RefundExecutionStatus.PROCESSING in REFUND_EXECUTION_TRANSITIONS[RefundExecutionStatus.PENDING]


def test_dispute_and_chargeback_have_independent_terminal_truth():
    assert DISPUTE_TRANSITIONS[DisputeStatus.CLOSED] == frozenset()
    assert CHARGEBACK_TRANSITIONS[ChargebackStatus.REVERSED] == frozenset()
    assert CHARGEBACK_TRANSITIONS[ChargebackStatus.FINAL_LOSS] == frozenset()


def test_manual_adjustment_post_is_immutable():
    assert AdjustmentStatus.POSTED in ADJUSTMENT_TRANSITIONS[AdjustmentStatus.APPROVED]
    assert ADJUSTMENT_TRANSITIONS[AdjustmentStatus.POSTED] == frozenset()


def test_provider_operation_unknown_is_reconcilable_but_not_directly_restarted():
    allowed = PROVIDER_OPERATION_TRANSITIONS[ProviderOperationStatus.UNKNOWN]
    assert ProviderOperationStatus.IN_PROGRESS not in allowed
    assert ProviderOperationStatus.SUCCEEDED in allowed
    assert ProviderOperationStatus.FAILED_FINAL in allowed


def test_money_uses_decimal_and_integer_minor_units_only():
    value = Money(Decimal("123.45"), "INR")
    assert value.minor_units == 12345
    assert Money.from_minor_units(12345, "INR") == value

    with pytest.raises(MoneyValidationError):
        Money(123.45, "INR")  # type: ignore[arg-type]
    with pytest.raises(MoneyValidationError):
        Money(Decimal("1.001"), "INR")
    with pytest.raises(MoneyValidationError):
        Money(Decimal("1.00"), "usd")
    with pytest.raises(MoneyValidationError):
        Money(Decimal("1.00"), "USD")
    with pytest.raises(MoneyValidationError):
        Money.from_minor_units(True, "INR")  # type: ignore[arg-type]


def test_money_currency_and_positive_amount_guards():
    one = Money(Decimal("1.00"), "INR")
    zero = Money(Decimal("0.00"), "INR")
    assert one.require_positive() is one
    assert zero.require_nonnegative() is zero
    with pytest.raises(MoneyValidationError):
        zero.require_positive(label="Payment")
    with pytest.raises(MoneyValidationError):
        one.require_same_currency(object())  # type: ignore[arg-type]


def test_current_payment_overloads_have_explicit_safe_compatibility_mapping():
    assert CURRENT_PAYMENT_STATUS_COMPATIBILITY["settled"] == (
        PaymentStatus.CAPTURED,
        "derive_settlement_separately",
    )
    assert CURRENT_PAYMENT_STATUS_COMPATIBILITY["partially_refunded"] == (
        PaymentStatus.CAPTURED,
        "derive_refund_projection_separately",
    )
    assert CURRENT_PAYMENT_STATUS_COMPATIBILITY["refunded"] == (
        PaymentStatus.CAPTURED,
        "derive_refund_projection_separately",
    )


def test_current_platform_failed_operation_requires_evidence_classification():
    target, mode = CURRENT_PLATFORM_PROVIDER_OPERATION_COMPATIBILITY["failed"]
    assert target is None
    assert mode == "requires_evidence_classification"


def test_contract_and_python_invariants_are_exactly_bound():
    data = _contract()
    assert set(data["hard_invariants"]) == set(PAY1_HARD_INVARIANTS)
    assert data["money"]["business_representation"] == "Decimal"
    assert data["money"]["provider_representation"] == "integer_minor_units"
    assert data["money"]["float_forbidden"] is True
    assert data["money"]["production_currency_allowlist"] == {"INR": 2}


def test_pay1_is_not_wired_into_existing_runtime_paths():
    import subprocess

    output = subprocess.check_output(
        ["git", "grep", "-l", "canonical_financial_model"],
        cwd=ROOT,
        text=True,
    ).splitlines()
    assert set(output) <= {
        ".github/workflows/pay1-canonical-financial-domain.yml",
        "docs/architecture/PAY1_CANONICAL_FINANCIAL_DOMAIN_MODEL.md",
        "tests/test_pay1_canonical_financial_domain.py",
    }


def test_pay1_scope_keeps_migrations_and_live_money_disabled():
    data = _contract()["pay1_scope"]
    assert data == {
        "application_behavior_wiring": False,
        "migration_mutation": False,
        "database_acl_rls_change": False,
        "live_provider_activation": False,
        "live_money_movement": False,
        "refund_provider_execution": "DEFERRED_FAIL_CLOSED",
    }


def test_pay1_documents_and_workflow_bind_terminal_markers_and_migration_gates():
    combined = SCOPE.read_text(encoding="utf-8") + ACCEPTANCE.read_text(encoding="utf-8")
    for marker in (
        "PAY1_CANONICAL_FINANCIAL_MODEL=PASS",
        "PAY1_STATE_MACHINE_SEPARATION=PASS",
        "PAY1_MONEY_CONTRACT=PASS",
        "PAY1_COMPATIBILITY_MAPPING=PASS",
        "PAY1_MIGRATION_BASELINE=PASS",
        "PAY1_PAY0_INHERITED=PASS",
        "PAY1_LIVE_MONEY_MOVEMENT=DISABLED",
        "PAY1_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED",
        "PAY1_FINAL=PASS",
    ):
        assert marker in combined

    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert BASE_SHA in workflow
    assert BASE_TREE in workflow
    assert ALEMBIC_HEAD in workflow
    assert "scripts/verify_alembic_graph.py" in workflow
    assert "migration-lifecycle-ci.yml" in workflow
    assert "migration-data-preservation-ci.yml" in workflow
    assert "migration-adversarial-safety-ci.yml" in workflow
    assert "migration-semantics-inventory.yml" in workflow
    assert "finance-hardening-ci.yml" in workflow
