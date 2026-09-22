from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

import pytest

from app.payment_certification.post_launch import (
    PostLaunchEvidence,
    canonical_evidence_manifest_sha256,
    certify_post_launch,
)


def good_payload() -> dict:
    payload = {
        "schema_version": 1,
        "evidence_class": "production_live",
        "environment": "live",
        "activation_stage": 1,
        "activation_authorization_id": "PAY22-STAGE1-TEST",
        "activation_authorization_sha": "b" * 40,
        "window_start": "2026-09-22T00:00:00+00:00",
        "window_end": "2026-09-23T00:00:00+00:00",
        "read_only_snapshot": True,
        "reduced_read_role": True,
        "contains_raw_customer_or_provider_identifiers": False,
        "payments": {
            "provider_count": 12,
            "provider_amount_minor": 120000,
            "finance_count": 12,
            "finance_amount_minor": 120000,
        },
        "settlements": {
            "provider_count": 2,
            "provider_amount_minor": 118000,
            "finance_count": 2,
            "finance_amount_minor": 118000,
        },
        "refunds": {
            "provider_count": 1,
            "provider_amount_minor": 5000,
            "finance_count": 1,
            "finance_amount_minor": 5000,
        },
        "allocations_invoices": {
            "allocation_count": 12,
            "joined_allocation_count": 12,
            "allocation_amount_minor": 120000,
            "joined_invoice_allocation_amount_minor": 120000,
            "invoice_state_mismatch_count": 0,
            "orphaned_invoice_count": 0,
        },
        "ledger": {
            "posted_debit_minor": 243000,
            "posted_credit_minor": 243000,
            "unbalanced_entry_count": 0,
        },
        "subscriptions": {
            "expected_activation_count": 10,
            "actual_activation_count": 10,
            "activation_mismatch_count": 0,
            "unexplained_entitlement_grants": 0,
        },
        "platform_billing": {
            "checked_state_count": 3,
            "invalid_state_count": 0,
            "open_reconciliation_discrepancy_count": 0,
        },
        "accounting_closure": {
            "live_closed_run_count": 1,
            "expected_object_count": 15,
            "observed_object_count": 15,
            "resolved_object_count": 15,
            "mismatch_count": 0,
            "retry_count": 0,
            "manual_review_count": 0,
            "incident_count": 0,
        },
        "anomalies": {
            "unexplained_duplicate_charges": 0,
            "duplicate_refunds": 0,
            "lost_payments": 0,
            "orphaned_invoices": 0,
            "unexplained_entitlement_grants": 0,
            "unresolved_cross_tenant_anomalies": 0,
        },
        "evidence_manifest_sha256": "",
    }
    payload["evidence_manifest_sha256"] = canonical_evidence_manifest_sha256(payload)
    return payload


def rehash(payload: dict) -> dict:
    payload["evidence_manifest_sha256"] = canonical_evidence_manifest_sha256(payload)
    return payload


def decision(payload: dict):
    return certify_post_launch(PostLaunchEvidence.from_dict(payload))


def test_nonzero_live_reconciled_window_certifies():
    result = decision(good_payload())
    assert result.certified is True
    assert result.failures == ()


def test_stage0_zero_activity_cannot_vacuously_certify():
    payload = good_payload()
    payload["activation_stage"] = 0
    for key in ("payments", "settlements"):
        payload[key]["provider_count"] = 0
        payload[key]["provider_amount_minor"] = 0
        payload[key]["finance_count"] = 0
        payload[key]["finance_amount_minor"] = 0

    result = decision(payload)
    assert result.certified is False
    assert "pay23.activation_stage.not_live" in result.failures
    assert "pay23.payments.no_live_activity" in result.failures
    assert "pay23.settlements.no_live_activity" in result.failures


@pytest.mark.parametrize(
    ("path", "value", "expected"),
    [
        (("payments", "finance_count"), 11, "pay23.payments.count_mismatch"),
        (("payments", "finance_amount_minor"), 119999, "pay23.payments.amount_mismatch"),
        (("settlements", "finance_count"), 1, "pay23.settlements.count_mismatch"),
        (("refunds", "finance_amount_minor"), 4999, "pay23.refunds.amount_mismatch"),
        (
            ("allocations_invoices", "joined_allocation_count"),
            11,
            "pay23.allocations.invoice_join_count_mismatch",
        ),
        (
            ("ledger", "posted_credit_minor"),
            242999,
            "pay23.ledger.aggregate_unbalanced",
        ),
        (
            ("subscriptions", "actual_activation_count"),
            9,
            "pay23.subscriptions.activation_count_mismatch",
        ),
        (
            ("platform_billing", "open_reconciliation_discrepancy_count"),
            1,
            "pay23.platform_billing.open_reconciliation_discrepancy",
        ),
        (
            ("accounting_closure", "incident_count"),
            1,
            "pay23.accounting_closure.incident_count_nonzero",
        ),
        (
            ("anomalies", "unresolved_cross_tenant_anomalies"),
            1,
            "pay23.anomaly.unresolved_cross_tenant_anomalies",
        ),
    ],
)
def test_each_reconciliation_or_integrity_failure_blocks_certification(path, value, expected):
    payload = good_payload()
    payload[path[0]][path[1]] = value
    result = decision(payload)
    assert result.certified is False
    assert expected in result.failures


def test_all_six_required_anomaly_counters_are_zero_gates():
    for name in good_payload()["anomalies"]:
        payload = good_payload()
        payload["anomalies"][name] = 1
        if name == "orphaned_invoices":
            payload["allocations_invoices"]["orphaned_invoice_count"] = 1
        if name == "unexplained_entitlement_grants":
            payload["subscriptions"]["unexplained_entitlement_grants"] = 1
        result = decision(payload)
        assert result.certified is False
        assert f"pay23.anomaly.{name}" in result.failures


def test_refunds_may_legitimately_be_zero_if_both_sides_match():
    payload = good_payload()
    payload["refunds"] = {
        "provider_count": 0,
        "provider_amount_minor": 0,
        "finance_count": 0,
        "finance_amount_minor": 0,
    }
    payload["anomalies"]["duplicate_refunds"] = 0
    rehash(payload)
    assert decision(payload).certified is True


def test_evidence_must_be_live_read_only_reduced_and_identifier_safe():
    variants = [
        ("evidence_class", "synthetic", "pay23.evidence_class.not_production_live"),
        ("environment", "test", "pay23.environment.not_live"),
        ("read_only_snapshot", False, "pay23.snapshot.not_read_only"),
        ("reduced_read_role", False, "pay23.snapshot.role_not_reduced"),
        (
            "contains_raw_customer_or_provider_identifiers",
            True,
            "pay23.snapshot.raw_identifiers_forbidden",
        ),
    ]
    for key, value, expected in variants:
        payload = good_payload()
        payload[key] = value
        result = decision(payload)
        assert result.certified is False
        assert expected in result.failures


def test_window_is_bounded_and_timezone_aware():
    payload = good_payload()
    payload["window_end"] = "2026-09-22T00:30:00+00:00"
    assert "pay23.window.too_short" in decision(payload).failures

    payload = good_payload()
    payload["window_end"] = "2026-11-01T00:00:00+00:00"
    assert "pay23.window.too_large" in decision(payload).failures

    payload = good_payload()
    payload["window_start"] = "2026-09-22T00:00:00"
    with pytest.raises(ValueError):
        PostLaunchEvidence.from_dict(payload)


def test_manifest_hash_is_mandatory_and_binds_the_evidence_body():
    payload = good_payload()
    payload["evidence_manifest_sha256"] = "not-a-hash"
    result = decision(payload)
    assert result.certified is False
    assert "pay23.evidence_manifest_sha256.invalid" in result.failures

    payload = good_payload()
    payload["payments"]["finance_amount_minor"] += 1
    result = decision(payload)
    assert result.certified is False
    assert "pay23.evidence_manifest_sha256.mismatch" in result.failures


def test_counter_disagreement_is_itself_a_failure():
    payload = good_payload()
    payload["anomalies"]["orphaned_invoices"] = 1
    result = decision(payload)
    assert "pay23.anomaly.orphaned_invoice_counter_disagrees" in result.failures

    payload = good_payload()
    payload["anomalies"]["unexplained_entitlement_grants"] = 1
    result = decision(payload)
    assert "pay23.anomaly.entitlement_counter_disagrees" in result.failures


def test_activation_authorization_identity_and_stage_range_are_required():
    payload = good_payload()
    payload["activation_authorization_id"] = ""
    rehash(payload)
    assert "pay23.activation_authorization.id_missing" in decision(payload).failures

    payload = good_payload()
    payload["activation_authorization_sha"] = "not-a-sha"
    rehash(payload)
    assert "pay23.activation_authorization.sha_invalid" in decision(payload).failures

    payload = good_payload()
    payload["activation_stage"] = 6
    rehash(payload)
    assert "pay23.activation_stage.invalid" in decision(payload).failures


def test_accounting_closure_must_cover_at_least_all_provider_money_objects():
    payload = good_payload()
    payload["accounting_closure"]["expected_object_count"] = 14
    payload["accounting_closure"]["observed_object_count"] = 14
    payload["accounting_closure"]["resolved_object_count"] = 14
    rehash(payload)
    result = decision(payload)
    assert result.certified is False
    assert "pay23.accounting_closure.coverage_too_small" in result.failures
