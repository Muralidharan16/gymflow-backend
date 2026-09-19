from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.finance_core.domain.monetary_command import (
    MonetaryCommandError,
    MonetaryCommandIdentity,
    MonetaryCommandStatus,
    PAY3_INVARIANTS,
    actor_reference_hash,
    canonical_monetary_request_hash,
)

ROOT=Path(__file__).resolve().parents[1]
MIGRATION=ROOT/"alembic/versions/zm07d8e9f0a47_pay3_monetary_command_protocol.py"
REPAIR=ROOT/"alembic/versions/zn07d8e9f0a48_pay3_monetary_command_ambiguity_repair.py"
CONTRACT=ROOT/"docs/architecture/pay3_monetary_command_protocol_v1.json"
BASELINE=ROOT/"tests/migration_app_secure_owner_context_boundary_baseline.py"


def test_pay3_hash_is_canonical_and_rejects_float():
    left=canonical_monetary_request_hash({"amount":Decimal("100.00"),"nested":{"b":2,"a":1}})
    right=canonical_monetary_request_hash({"nested":{"a":1,"b":2},"amount":Decimal("100.0")})
    assert left==right
    with pytest.raises(MonetaryCommandError):
        canonical_monetary_request_hash({"amount":100.0})


def test_pay3_actor_reference_is_hashed_not_stored_raw():
    value=actor_reference_hash("user:123")
    assert len(value)==64
    identity=MonetaryCommandIdentity(
        organization_id=__import__("uuid").UUID("33000000-0000-4000-8000-000000000001"),
        scope="finance.payment.capture",
        idempotency_key="doers:payment:subscription_123:attempt_1",
        request_hash_sha256="a"*64,
        business_reference="subscription_123",
        correlation_id=__import__("uuid").UUID("33000000-0000-4000-8000-000000000002"),
        actor_type="member",
        actor_ref_sha256=value,
    )
    assert identity.actor_ref_sha256==value
    assert not hasattr(identity,"actor_reference")


def test_pay3_states_do_not_call_unknown_terminal():
    assert set(MonetaryCommandStatus)=={
        MonetaryCommandStatus.PROCESSING,
        MonetaryCommandStatus.UNKNOWN,
        MonetaryCommandStatus.SUCCEEDED,
        MonetaryCommandStatus.FAILED_DETERMINISTIC,
    }


def test_pay3_migrations_are_additive_and_preserve_pushed_predecessor():
    first=MIGRATION.read_text(encoding="utf-8")
    repair=REPAIR.read_text(encoding="utf-8")
    assert 'revision = "zm07d8e9f0a47"' in first
    assert 'down_revision = "zl07d8e9f0a46"' in first
    assert 'revision = "zn07d8e9f0a48"' in repair
    assert 'down_revision = "zm07d8e9f0a47"' in repair
    assert "CREATE TABLE finance.monetary_commands" in first
    assert "ADD COLUMN ambiguity_code" in repair
    assert "ADD COLUMN unknown_at" in repair
    assert "PAY-3 repair downgrade blocked: durable ambiguity evidence exists" in repair
    assert "PAY-3 repair cannot infer ambiguity evidence for pre-existing unknown command" in repair
    for source in (first,repair):
        upper=source.upper()
        for forbidden in (
            "UPDATE FINANCE.PAYMENTS",
            "UPDATE FINANCE.INVOICES",
            "ALTER TABLE FINANCE.PAYMENTS",
            "ALTER TABLE FINANCE.INVOICES",
            "DELETE FROM FINANCE.PAYMENTS",
            "TRUNCATE TABLE FINANCE.",
        ):
            assert forbidden not in upper


def test_pay3_functions_are_reduced_owner_public_revoked_and_runtime_table_blind():
    first=MIGRATION.read_text(encoding="utf-8")
    repair=REPAIR.read_text(encoding="utf-8")
    for name in (
        "reserve_finance_monetary_command",
        "complete_finance_monetary_command",
        "mark_finance_monetary_command_unknown",
        "fail_finance_monetary_command",
    ):
        assert f"CREATE FUNCTION app_secure.{name}" in first
    assert first.count("SECURITY DEFINER") >= 4
    assert "GRANT EXECUTE ON FUNCTION" not in first
    assert "PAY-3 finance_runtime received direct monetary table" in first
    assert "CREATE OR REPLACE FUNCTION app_secure.reserve_finance_monetary_command" in repair
    assert "CREATE OR REPLACE FUNCTION app_secure.mark_finance_monetary_command_unknown" in repair
    assert "REVOKE ALL ON FUNCTION" in repair
    assert "actor_ref_sha256::text IS DISTINCT FROM p_actor_ref_sha256" in repair
    assert "ambiguity_code=p_error_code" in repair
    assert "unknown_at=pg_catalog.clock_timestamp()" in repair


def test_pay3_historical_owner_context_baseline_is_not_mutated_by_pay3():
    source=BASELINE.read_text(encoding="utf-8")
    assert "pay3_monetary_command" not in source


def test_pay3_invariants_and_machine_contract_match():
    data=json.loads(CONTRACT.read_text(encoding="utf-8"))
    assert data["phase"]=="PAY-3"
    assert data["alembic"]=={
        "predecessor":"zl07d8e9f0a46",
        "initial":"zm07d8e9f0a47",
        "head":"zn07d8e9f0a48",
    }
    assert data["protocol"]["persistent_no_automatic_expiry"] is True
    assert data["protocol"]["unknown"]=="blocks_replacement_effect_until_reconciled"
    assert data["protocol"]["unknown_reason_persists_after_reconciliation"] is True
    assert data["protocol"]["same_command_actor_identity_immutable"] is True
    assert data["protocol"]["concurrent_same_key_database_fenced"] is True
    assert data["protocol"]["crash_before_commit_retry_safe"] is True
    assert data["protocol"]["crash_after_commit_retry_replays"] is True
    assert data["authority"]["direct_runtime_table_dml"] is False
    assert data["authority"]["production_function_execute_grant"] is False
    assert data["live_money_movement"] is False
    assert data["refund_provider_execution"]=="DEFERRED_FAIL_CLOSED"
    assert {
        "same_tenant_scope_key_same_payload_replays_original_command",
        "same_tenant_scope_key_different_payload_conflicts",
        "logical_command_unknown_blocks_replacement_effect_until_reconciled",
        "unknown_outcome_reason_remains_durable_after_reconciliation",
        "same_logical_command_actor_identity_is_immutable",
        "concurrent_same_key_reservation_is_database_fenced",
        "crash_before_commit_does_not_consume_idempotency_key",
        "crash_after_commit_replays_original_command",
        "terminal_command_truth_is_immutable",
        "command_evidence_has_no_automatic_expiry",
        "actor_reference_is_persisted_only_as_sha256",
        "business_payload_hashing_rejects_float",
        "runtime_has_no_direct_monetary_command_table_dml",
        "live_money_movement_remains_disabled",
    }==set(PAY3_INVARIANTS)
