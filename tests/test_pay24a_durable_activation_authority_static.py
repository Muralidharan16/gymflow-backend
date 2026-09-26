from __future__ import annotations

import ast
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / (
    "alembic/versions/zz57d8e9f0a65_pay24a_durable_activation_authority.py"
)
CONTRACT = ROOT / (
    "docs/architecture/pay24a_durable_activation_authority_v1.json"
)
DESIGN = ROOT / "docs/architecture/PAY24A_DURABLE_ACTIVATION_AUTHORITY.md"
DOMAIN = ROOT / "app/payment_activation/domain.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _assignment(module: ast.Module, name: str) -> object:
    for node in module.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"missing assignment: {name}")


def test_pay24a_is_the_single_next_revision() -> None:
    module = ast.parse(_source(MIGRATION))
    assert _assignment(module, "revision") == "zz57d8e9f0a65"
    assert _assignment(module, "down_revision") == "zz47d8e9f0a64"


def test_pay24a_persists_one_authority_and_all_eight_independent_capabilities() -> None:
    source = _source(MIGRATION)
    for relation in (
        "finance.payment_activation_release_identities",
        "finance.payment_activation_authorizations",
        "finance.payment_activation_authority",
        "finance.payment_activation_transition_events",
        "finance.provider_admission_leases",
    ):
        assert relation in source

    for capability in (
        "checkout",
        "webhooks",
        "payment_application",
        "subscription_activation",
        "refund_execution",
        "recurring_billing",
        "dunning",
        "platform_billing",
    ):
        assert capability in source

    assert "payments_enabled" not in source
    assert "global_payments_switch" not in source
    assert "global_payment_switch" not in source


def test_pay24a_database_constraints_encode_stage0_and_stage1_shape() -> None:
    source = _source(MIGRATION).lower()
    required = (
        "stage in (0,1)",
        "provider_egress_state",
        "'blocked'",
        "'open'",
        "'closing'",
        "internal_organization_id is null",
        "internal_organization_id is not null",
        "refund_execution",
        "recurring_billing",
        "dunning",
        "platform_billing",
    )
    for token in required:
        assert token in source


def test_pay24a_release_identity_is_separate_from_transition_input() -> None:
    source = _source(MIGRATION)
    assert "pay24a_bind_release_identity" in source
    assert "pay24a_transition_activation" in source

    transition_start = source.index("pay24a_transition_activation")
    transition_end = source.find("$function$", transition_start)
    transition_header = source[transition_start:transition_end]
    assert "p_deployed_sha" not in transition_header
    assert "p_certified_sha" not in transition_header

    for token in (
        "authorized_sha",
        "authorized_stage",
        "authorized_by",
        "authorized_at",
        "authorization_id",
    ):
        assert token in source


def test_pay24a_generation_evidence_and_operation_idempotency_are_durable() -> None:
    source = _source(MIGRATION).lower()
    for token in (
        "expected_generation",
        "prior_generation",
        "new_generation",
        "operation_id",
        "request_digest",
        "posture_digest",
        "previous_event_hash",
        "event_hash",
        "for update",
    ):
        assert token in source
    # Release binding, human authorization, and transition evidence each own
    # a database-enforced idempotency key.  Match the actual column-form UNIQUE
    # constraint instead of relying on table-constraint spelling.
    assert source.count("operation_id uuid not null unique") == 3


def test_pay24a_admission_lifecycle_and_rollback_protocol_are_explicit() -> None:
    source = _source(MIGRATION).lower()
    for state in (
        "admitted",
        "active",
        "completed",
        "expired",
        "revoked",
        "unknown",
    ):
        assert f"'{state}'" in source

    for function in (
        "pay24a_request_provider_admission",
        "pay24a_start_provider_admission",
        "pay24a_finish_provider_admission",
        "pay24a_begin_emergency_rollback",
        "pay24a_expire_provider_admissions",
        "pay24a_finalize_emergency_rollback",
    ):
        assert function in source

    assert "app.current_org_id" in source
    assert "logical_operation_id" in source
    assert "lease_expires_at" in source
    assert "provider_egress_state='closing'" in source.replace(" ", "")


def test_pay24a_uses_force_rls_security_definer_and_exact_role_partition() -> None:
    source = _source(MIGRATION)
    normalized = " ".join(source.split())
    module = ast.parse(source)
    assert set(_assignment(module, "_TABLES")) == {
        "payment_activation_release_identities",
        "payment_activation_authorizations",
        "payment_activation_authority",
        "payment_activation_transition_events",
        "provider_admission_leases",
    }
    assert "for table_name in _TABLES" in source
    assert (
        'f"ALTER TABLE finance.{table_name} ENABLE ROW LEVEL SECURITY"'
        in source
    )
    assert (
        'f"ALTER TABLE finance.{table_name} FORCE ROW LEVEL SECURITY"'
        in source
    )
    assert source.count("SECURITY DEFINER") >= 10
    assert source.count("SET row_security=on") >= 10
    assert "REVOKE ALL ON FUNCTION" in source
    assert "FROM PUBLIC" in source

    assert "finance_config_runtime" in source
    assert "finance_payment_runtime" in source
    assert "finance_refund_runtime" in source
    assert "lifecycle_maintenance_runtime" in source
    assert "app_security_owner" in source
    assert "GRANT EXECUTE ON FUNCTION" in normalized

    # The PAY-24A primitive does not wire ordinary API/worker identities to
    # provider admission and does not give any runtime direct table DML.
    assert "pay24a_request_provider_admission" in source
    assert "claim_member_subscription_finance_events" not in source
    assert "consume_member_subscription_finance_event" not in source


def test_pay24a_downgrade_refuses_to_destroy_durable_evidence() -> None:
    source = _source(MIGRATION).lower()
    assert "downgrade" in source
    assert "refus" in source
    assert "generation" in source
    assert "provider_admission_leases" in source
    assert "payment_activation_transition_events" in source
    assert "no force row level security" in source
    assert "force row level security" in source
    assert " cascade" not in source


def test_pay22_authorization_is_now_stage_bound_even_in_legacy_policy() -> None:
    source = _source(DOMAIN)
    assert "authorized_stage" in source
    assert "activation.human_authorization.stage_mismatch" in source


def test_pay24a_contract_remains_explicitly_non_live_and_unwired() -> None:
    payload = json.loads(_source(CONTRACT))
    serialized = json.dumps(payload, sort_keys=True)
    design = _source(DESIGN)

    assert payload["phase"] == "PAY-24-A"
    assert payload["status"] != "certified"
    assert payload["alembic_revision"] == "zz57d8e9f0a65"
    assert payload["provider_boundaries_wired"] is False
    assert payload["entitlement_boundary_wired"] is False
    assert payload["stage1_live_activation"] == "NOT_AUTHORIZED"
    assert payload["real_money_movement"] == 0
    assert payload["pay23_enterprise_payment_system"] == "NOT_CERTIFIED"

    forbidden = "PAY23_ENTERPRISE_PAYMENT_SYSTEM=CERTIFIED"
    assert forbidden not in serialized
    assert forbidden not in design
    assert "No Stage-1 authorization" in design or "no Stage-1 authorization" in design



def test_pay24a_closing_is_one_way_and_provider_start_requires_commit() -> None:
    source = _source(MIGRATION)
    contract = json.loads(_source(CONTRACT))
    design = _source(DESIGN)

    assert "rollback is closing; ordinary transition denied" in source
    assert "rollback is closing; release rebind denied" in source
    assert "rollback is closing; authorization rebind denied" in source
    assert contract["rollback"]["closing_is_one_way"] is True
    assert (
        contract["admission"]["active_transition_must_commit_before_provider_io"]
        is True
    )
    assert (
        "commit the `active` transition **before any provider HTTP/I/O**"
        in design
    )
