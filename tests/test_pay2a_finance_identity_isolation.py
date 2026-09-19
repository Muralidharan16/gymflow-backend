from __future__ import annotations

import json
from pathlib import Path

from app.core.cluster_identity_graph import load_identity_transition_policy
from app.core.cluster_role_contract import load_contract_bundle


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "docs/architecture/pay2_financial_authority_v1.json"

FINANCE_ROLES = {
    "finance_runtime",
    "finance_read_runtime",
    "payment_worker_runtime",
    "refund_runtime",
    "finance_reconciliation_runtime",
    "finance_maintenance_runtime",
}


def _contract():
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def test_pay2a_exact_pay1_baseline_and_zero_migration_scope():
    data = _contract()
    assert data["inherited_pay1"] == {
        "commit": "ba8d77d0e50907fd435d358697d9721a63bad64b",
        "tree": "cd9500217b8230df2c0777b405b3d462b83e99ac",
        "alembic_head": "zk07d8e9f0a45",
        "certification": "PAY1_FINAL=PASS",
    }
    assert data["scope"]["alembic_revision"] is False
    assert data["scope"]["finance_acl_mutation"] is False
    assert data["scope"]["runtime_login_binding"] is False


def test_dedicated_finance_roles_are_exact_reduced_capabilities():
    bundle = load_contract_bundle()
    roles = bundle.roles["managed_roles"]
    assert set(_contract()["dedicated_finance_capabilities"]) == FINANCE_ROLES

    expected = {
        "superuser": False,
        "inherit": False,
        "create_role": False,
        "create_db": False,
        "can_login": False,
        "replication": False,
        "bypass_rls": False,
    }
    for role in FINANCE_ROLES:
        assert roles[role]["attributes"] == expected
        assert "must never be granted to migration_owner" in roles[role]["decision"]


def test_finance_roles_are_peer_isolated_and_forbidden_to_migration_owner():
    bundle = load_contract_bundle()
    policy = load_identity_transition_policy()
    forbidden = set(bundle.memberships["forbidden_migration_owner_memberships"])

    assert FINANCE_ROLES <= set(policy.peer_isolation_principals)
    assert FINANCE_ROLES <= forbidden
    pairs = {
        (row["granted_role"], row["member_role"])
        for row in bundle.memberships["exact_rows"]
    }
    assert not any(
        granted in FINANCE_ROLES or member in FINANCE_ROLES
        for granted, member in pairs
    )


def test_finance_roles_cannot_own_objects():
    bundle = load_contract_bundle()
    allowed = set(bundle.ownership["allowed_target_owners"])
    assert FINANCE_ROLES.isdisjoint(allowed)


def test_finance_role_settings_are_bounded_and_rls_on():
    settings = load_contract_bundle().role_settings["settings_by_role"]
    for role in FINANCE_ROLES:
        assert settings[role]["row_security"] == "on"
        assert settings[role]["lock_timeout"] == "2s"
        assert settings[role]["statement_timeout"] in {"5s", "10s", "15s"}
        assert settings[role]["idle_in_transaction_session_timeout"] in {"15s", "30s"}


def test_pay2a_live_money_and_refund_execution_stay_disabled():
    scope = _contract()["scope"]
    assert scope["live_money_movement"] is False
    assert scope["refund_provider_execution"] == "DEFERRED_FAIL_CLOSED"
