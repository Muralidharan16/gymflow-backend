from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from app.core.cluster_role_contract import load_contract_bundle
from app.core.runtime_principal_attestation import (
    RuntimePrincipalObservation,
    evaluate_runtime_binding_set,
    evaluate_runtime_principal_observation,
    load_runtime_binding_contract,
    validate_runtime_binding_contract,
    validate_runtime_url_configuration,
)


ROOT = Path(__file__).resolve().parents[1]


def _canonical_observation(
    component: str,
    username: str,
    database: str = "gymflow_runtime_test",
) -> RuntimePrincipalObservation:
    contract = load_runtime_binding_contract()
    bundle = load_contract_bundle()
    binding = contract.bindings[component]
    memberships = tuple({
        "granted_role": role,
        "member_role": username,
        "grantor": "postgres",
        "set_option": False,
        "inherit_option": True,
        "admin_option": False,
    } for role in binding.direct_capabilities)
    semantic_checks = tuple({
        "source": username,
        "target": role,
        "semantic": semantic,
        "allowed": role in binding.direct_capabilities and semantic in {"MEMBER", "USAGE"},
    } for role in sorted(bundle.roles["managed_roles"]) for semantic in ("MEMBER", "USAGE", "SET"))
    return RuntimePrincipalObservation(
        component=component,
        configured_username=username,
        configured_database=database,
        session_user=username,
        current_user=username,
        current_database=database,
        row_security="on",
        can_login=True,
        superuser=False,
        create_role=False,
        create_db=False,
        replication=False,
        bypass_rls=False,
        memberships=memberships,
        semantic_checks=semantic_checks,
        current_settings=dict(binding.session_settings),
        role_settings=dict(binding.session_settings),
        database_specific_settings=(),
    )


def _codes(observation: RuntimePrincipalObservation) -> set[str]:
    return {
        item.code
        for item in evaluate_runtime_principal_observation(observation)
    }


def test_runtime_binding_contract_matches_p2b_p2c_role_model() -> None:
    contract = load_runtime_binding_contract()
    assert validate_runtime_binding_contract(contract) == ()
    assert set(contract.bindings) == {
        "api",
        "auth",
        "finance_payment",
        "worker",
        "maintenance",
        "finance_config",
        "entitlement",
    }
    assert contract.optional_unprovisioned_components == ("entitlement",)
    assert set(contract.reserved_unbound_capabilities) == {
        "finance_runtime",
        "finance_refund_runtime",
        "finance_reconciliation_runtime",
        "finance_read_runtime",
        "finance_maintenance_runtime",
    }
    assert set(contract.bindings["api"].direct_capabilities) == {
        "app_runtime", "app_user"
    }
    assert set(contract.bindings["auth"].direct_capabilities) == {
        "auth_runtime", "app_user"
    }
    assert "app_runtime" not in contract.bindings["auth"].direct_capabilities
    assert contract.bindings["finance_payment"].direct_capabilities == (
        "finance_payment_runtime",
    )
    assert contract.bindings["finance_payment"].environment_variable == (
        "FINANCE_PAYMENT_DATABASE_URL"
    )
    assert contract.bindings["worker"].direct_capabilities == ("worker_runtime",)
    assert contract.bindings["maintenance"].direct_capabilities == (
        "lifecycle_maintenance_runtime",
    )
    assert contract.bindings["finance_config"].direct_capabilities == (
        "finance_config_runtime",
    )
    assert contract.bindings["entitlement"].direct_capabilities == (
        "entitlement_runtime",
    )
    assert contract.bindings["api"].session_settings == {
        "row_security": "on",
        "statement_timeout": "5s",
        "lock_timeout": "2s",
        "idle_in_transaction_session_timeout": "15s",
    }
    assert contract.bindings["worker"].session_settings == {
        "row_security": "on",
        "statement_timeout": "15s",
        "lock_timeout": "2s",
        "idle_in_transaction_session_timeout": "30s",
    }
    assert contract.bindings["finance_payment"].session_settings == {
        "row_security": "on",
        "statement_timeout": "15s",
        "lock_timeout": "2s",
        "idle_in_transaction_session_timeout": "30s",
    }


def test_reserved_finance_capabilities_cannot_be_bound_accidentally() -> None:
    contract = load_runtime_binding_contract()
    api = contract.bindings["api"]
    drifted_api = replace(
        api,
        direct_capabilities=(*api.direct_capabilities, "finance_runtime"),
    )
    drifted = replace(
        contract,
        bindings={**contract.bindings, "api": drifted_api},
    )
    codes = {item.code for item in validate_runtime_binding_contract(drifted)}
    assert "runtime.contract.reserved_direct_capability" in codes


def test_all_canonical_runtime_login_overlays_pass() -> None:
    observations = (
        _canonical_observation("api", "api_login"),
        _canonical_observation("auth", "auth_login"),
        _canonical_observation("finance_payment", "finance_payment_login"),
        _canonical_observation("worker", "worker_login"),
        _canonical_observation("maintenance", "maintenance_login"),
        _canonical_observation("finance_config", "finance_config_deployment"),
    )
    for observation in observations:
        assert evaluate_runtime_principal_observation(observation) == ()
    assert evaluate_runtime_binding_set(observations) == ()


def test_full_stage1_runtime_login_set_is_also_accepted_when_provisioned() -> None:
    observations = (
        _canonical_observation("api", "api_login"),
        _canonical_observation("auth", "auth_login"),
        _canonical_observation("finance_payment", "finance_payment_login"),
        _canonical_observation("worker", "worker_login"),
        _canonical_observation("maintenance", "maintenance_login"),
        _canonical_observation("finance_config", "finance_config_deployment"),
        _canonical_observation("entitlement", "entitlement_login"),
    )
    assert evaluate_runtime_binding_set(observations) == ()


def test_same_login_behind_different_urls_is_rejected_before_connect() -> None:
    urls = {
        "api": "postgresql+asyncpg://shared:a@db/prod?application_name=api",
        "auth": "postgresql+asyncpg://shared:b@db/prod?application_name=auth",
        "finance_payment": "postgresql+asyncpg://payment:p@db/prod",
        "worker": "postgresql+asyncpg://worker:c@db/prod",
        "maintenance": "postgresql+asyncpg://maintenance:d@db/prod",
        "finance_config": "postgresql+psycopg://finance_config_deployment:e@db/prod",
    }
    assert "runtime.config.login_reuse" in {
        item.code for item in validate_runtime_url_configuration(urls)
    }


def test_runtime_urls_must_target_same_database() -> None:
    urls = {
        "api": "postgresql+asyncpg://api:a@db/prod",
        "auth": "postgresql+asyncpg://auth:b@db/prod",
        "finance_payment": "postgresql+asyncpg://payment:p@db/prod",
        "worker": "postgresql+asyncpg://worker:c@db/prod",
        "maintenance": "postgresql+asyncpg://maintenance:d@db/other",
        "finance_config": "postgresql+psycopg://finance_config_deployment:e@db/prod",
    }
    assert "runtime.config.database_divergence" in {
        item.code for item in validate_runtime_url_configuration(urls)
    }


def test_current_user_contamination_is_rejected() -> None:
    observation = replace(
        _canonical_observation("worker", "worker_login"),
        current_user="app_runtime",
    )
    assert "runtime.current_user_mismatch" in _codes(observation)


def test_bypassrls_on_deployment_login_is_rejected() -> None:
    observation = replace(
        _canonical_observation("worker", "worker_login"),
        bypass_rls=True,
    )
    assert "runtime.dangerous_login_attribute" in _codes(observation)


def test_wrong_persisted_timeout_is_rejected() -> None:
    observation = _canonical_observation("worker", "worker_login")
    role_settings = dict(observation.role_settings)
    role_settings["statement_timeout"] = "45s"
    assert "runtime.role_setting" in _codes(
        replace(observation, role_settings=role_settings)
    )


def test_semantically_equivalent_timeout_units_are_accepted() -> None:
    observation = _canonical_observation("api", "api_login")
    current = dict(observation.current_settings)
    persisted = dict(observation.role_settings)
    current["statement_timeout"] = "5000ms"
    persisted["lock_timeout"] = "2000ms"
    assert evaluate_runtime_principal_observation(
        replace(
            observation,
            current_settings=current,
            role_settings=persisted,
        )
    ) == ()


def test_database_specific_runtime_setting_is_rejected() -> None:
    observation = _canonical_observation("maintenance", "maintenance_login")
    drifted = replace(
        observation,
        database_specific_settings=({
            "database": "gymflow_runtime_test",
            "setting": "lock_timeout=9s",
        },),
    )
    assert "runtime.database_specific_setting" in _codes(drifted)


def test_missing_governed_role_setting_is_rejected() -> None:
    observation = _canonical_observation("auth", "auth_login")
    persisted = dict(observation.role_settings)
    persisted.pop("idle_in_transaction_session_timeout")
    assert "runtime.role_setting" in _codes(
        replace(observation, role_settings=persisted)
    )


def test_extra_direct_capability_is_rejected() -> None:
    observation = _canonical_observation("worker", "worker_login")
    extra = {
        "granted_role": "app_runtime",
        "member_role": "worker_login",
        "grantor": "postgres",
        "set_option": False,
        "inherit_option": True,
        "admin_option": False,
    }
    drifted = replace(
        observation,
        memberships=(*observation.memberships, extra),
    )
    assert "runtime.direct_membership_set" in _codes(drifted)


def test_wrong_grantor_is_rejected() -> None:
    observation = _canonical_observation("maintenance", "maintenance_login")
    row = dict(observation.memberships[0])
    row["grantor"] = "unexpected_admin"
    assert "runtime.membership_grantor" in _codes(
        replace(observation, memberships=(row,))
    )


def test_set_role_option_on_runtime_capability_is_rejected() -> None:
    observation = _canonical_observation("worker", "worker_login")
    row = dict(observation.memberships[0])
    row["set_option"] = True
    checks = [dict(item) for item in observation.semantic_checks]
    next(
        item for item in checks
        if item["target"] == "worker_runtime" and item["semantic"] == "SET"
    )["allowed"] = True
    drifted = replace(
        observation,
        memberships=(row,),
        semantic_checks=tuple(checks),
    )
    codes = _codes(drifted)
    assert "runtime.membership_option" in codes
    assert "runtime.semantic_reachability" in codes


def test_transitive_peer_reachability_is_rejected_by_live_semantics() -> None:
    observation = _canonical_observation("worker", "worker_login")
    checks = [dict(item) for item in observation.semantic_checks]
    next(
        item for item in checks
        if item["target"] == "app_runtime" and item["semantic"] == "MEMBER"
    )["allowed"] = True
    assert "runtime.semantic_reachability" in _codes(
        replace(observation, semantic_checks=tuple(checks))
    )


def test_runtime_binding_set_rejects_login_reuse() -> None:
    observations = (
        _canonical_observation("api", "shared_login"),
        _canonical_observation("auth", "shared_login"),
        _canonical_observation("finance_payment", "finance_payment_login"),
        _canonical_observation("worker", "worker_login"),
        _canonical_observation("maintenance", "maintenance_login"),
        _canonical_observation("finance_config", "finance_config_deployment"),
    )
    assert "runtime.binding_set.login_reuse" in {
        item.code for item in evaluate_runtime_binding_set(observations)
    }


def test_repository_runtime_identity_routing_guard_passes() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-s",
            str(ROOT / "scripts/verify_runtime_identity_routing.py"),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr

def test_pay24c_governance_closed_inventories_pass_under_p2d_certification() -> None:
    """Evidence-only bridge: execute PAY-24-C closed inventories in P2D CI."""
    from tests import test_cluster_role_contract_manifests as cluster_contracts
    from tests import test_dafd_app_private_owner_context_boundary as app_private_contract
    from tests import test_migration_app_secure_owner_context_boundary as app_secure_contract

    cluster_contracts.test_exact_managed_role_contract()
    cluster_contracts.test_role_settings_are_exact()
    cluster_contracts.test_exact_membership_and_grantor_contract()
    cluster_contracts.test_runtime_capabilities_are_not_migration_owner_memberships()
    cluster_contracts.test_ownership_manifest_uses_only_allowed_owners()
    cluster_contracts.test_ownership_manifest_matches_reviewed_projection()
    cluster_contracts.test_pay24c_entitlement_ownership_projection_is_exact()

    app_private_contract.test_private_and_executor_sensitive_migration_inventories_are_closed()
    app_private_contract.test_complete_app_private_ddl_category_allowlist_is_exact()
    app_secure_contract.test_complete_app_secure_ddl_category_allowlist_is_exact()

def test_pay24d_stage1_readiness_contracts_pass_under_p2d_certification() -> None:
    from tests import test_pay24d_stage1_readiness_static as pay24d

    pay24d.test_pay24d_revision_and_stage0_authority_are_frozen()
    pay24d.test_stage1_entitlement_deployment_template_is_disabled_and_secret_isolated()
    pay24d.test_entitlement_runtime_requires_tls_and_observability()
    pay24d.test_entitlement_observability_bootstrap_is_required_before_worker_start()
    pay24d.test_stage1_tasks_are_explicitly_routed_but_not_scheduled()
    pay24d.test_pay24d_observability_is_aggregate_and_non_authoritative()
    pay24d.test_stage1_rollback_runbook_is_fail_closed()
    pay24d.test_stage1_internal_canary_is_exact_sha_authorized_and_kill_switched()
    pay24d.test_zero_money_canary_checklist_preserves_stage0()

    from tests import test_process_database_profile_boundary as process_boundary

    process_boundary.test_entitlement_worker_profile_has_only_entitlement_database_identity()
    process_boundary.test_entitlement_worker_rejects_other_database_credentials()
    process_boundary.test_entitlement_worker_requires_verify_full_database_tls()
    process_boundary.test_entitlement_worker_rejects_unrelated_cloud_and_provider_secrets()

def test_pay24e_stage1_canary_contracts_pass_under_p2d_certification() -> None:
    from tests import test_pay24e_stage1_canary_static as pay24e

    pay24e.test_pay24e_migration_does_not_activate_stage1()
    pay24e.test_stage0_claim_is_empty_and_old_claim_is_revoked()
    pay24e.test_stage1_scope_is_exact_internal_org_and_minimal_four_switches()
    pay24e.test_protected_mutations_recheck_stage1_authority()

def test_pay24e_stage1_activation_package_contracts_pass_under_p2d_certification() -> None:
    from tests import test_pay24e_stage1_activation_package_static as package

    package.test_scheduler_publishes_only_entitlement_apply()
    package.test_scheduler_has_no_database_or_provider_credentials()
    package.test_canary_overlay_is_profile_gated_and_zero_replica()
    package.test_preflight_is_read_only_and_exact_sha_stage_aware()
    package.test_runbook_requires_stage0_preprovision_and_durable_authority()

