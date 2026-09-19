from __future__ import annotations

import ast
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
CONTRACT=ROOT/"docs/architecture/pay3_monetary_command_protocol_v1.json"


def test_pay3_hash_is_canonical_and_rejects_float():
    left=canonical_monetary_request_hash({
        "amount":Decimal("100.00"),
        "nested":{"b":2,"a":1},
    })
    right=canonical_monetary_request_hash({
        "nested":{"a":1,"b":2},
        "amount":Decimal("100.0"),
    })
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


def test_pay3_migration_is_additive_and_no_existing_finance_rewrite():
    source=MIGRATION.read_text(encoding="utf-8")
    upper=source.upper()
    assert 'REVISION = "ZM07D8E9F0A47"' in upper
    assert 'DOWN_REVISION = "ZL07D8E9F0A46"' in upper
    assert "CREATE TABLE finance.monetary_commands" in source
    assert "ENABLE ROW LEVEL SECURITY" in source
    assert "FORCE ROW LEVEL SECURITY" in source
    assert "REVOKE ALL ON TABLE finance.monetary_commands FROM PUBLIC" in source
    assert "PAY-3 downgrade blocked: durable monetary command evidence exists" in source
    for forbidden in (
        "UPDATE FINANCE.PAYMENTS",
        "UPDATE FINANCE.INVOICES",
        "ALTER TABLE FINANCE.PAYMENTS",
        "ALTER TABLE FINANCE.INVOICES",
        "DELETE FROM FINANCE.",
        "TRUNCATE TABLE FINANCE.",
    ):
        assert forbidden not in upper


def test_pay3_functions_are_security_definer_public_revoked_and_runtime_table_blind():
    source=MIGRATION.read_text(encoding="utf-8")
    for name in (
        "reserve_finance_monetary_command",
        "complete_finance_monetary_command",
        "mark_finance_monetary_command_unknown",
        "fail_finance_monetary_command",
    ):
        assert f"CREATE FUNCTION app_secure.{name}" in source
    assert source.count("SECURITY DEFINER") >= 4
    assert source.count("SET row_security=on") >= 4
    assert "REVOKE ALL ON FUNCTION {signature} FROM PUBLIC" in source
    assert "GRANT EXECUTE ON FUNCTION" not in source
    assert "PAY-3 finance_runtime received direct monetary table" in source


def test_pay3_invariants_and_machine_contract_match():
    data=json.loads(CONTRACT.read_text(encoding="utf-8"))
    assert data["phase"]=="PAY-3"
    assert data["alembic"]=={
        "predecessor":"zl07d8e9f0a46",
        "head":"zm07d8e9f0a47",
    }
    assert data["protocol"]["persistent_no_automatic_expiry"] is True
    assert data["protocol"]["unknown"]=="blocks_replacement_effect_until_reconciled"
    assert data["authority"]["direct_runtime_table_dml"] is False
    assert data["authority"]["production_function_execute_grant"] is False
    assert data["live_money_movement"] is False
    assert data["refund_provider_execution"]=="DEFERRED_FAIL_CLOSED"
    assert {
        "same_tenant_scope_key_same_payload_replays_original_command",
        "same_tenant_scope_key_different_payload_conflicts",
        "logical_command_unknown_blocks_replacement_effect_until_reconciled",
        "terminal_command_truth_is_immutable",
        "command_evidence_has_no_automatic_expiry",
        "actor_reference_is_persisted_only_as_sha256",
        "business_payload_hashing_rejects_float",
        "runtime_has_no_direct_monetary_command_table_dml",
        "live_money_movement_remains_disabled",
    }==set(PAY3_INVARIANTS)
