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
    assert set(contract.bindings) == {"api", "auth", "worker", "maintenance"}
    assert set(contract.bindings["api"].direct_capabilities) == {
        "app_runtime", "app_user"
    }
    assert set(contract.bindings["auth"].direct_capabilities) == {
        "auth_runtime", "app_runtime", "app_user"
    }
    assert contract.bindings["worker"].direct_capabilities == ("worker_runtime",)
    assert contract.bindings["maintenance"].direct_capabilities == (
        "lifecycle_maintenance_runtime",
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


def test_all_canonical_runtime_login_overlays_pass() -> None:
    observations = (
        _canonical_observation("api", "api_login"),
        _canonical_observation("auth", "auth_login"),
        _canonical_observation("worker", "worker_login"),
        _canonical_observation("maintenance", "maintenance_login"),
    )
    for observation in observations:
        assert evaluate_runtime_principal_observation(observation) == ()
    assert evaluate_runtime_binding_set(observations) == ()


def test_same_login_behind_different_urls_is_rejected_before_connect() -> None:
    urls = {
        "api": "postgresql+asyncpg://shared:a@db/prod?application_name=api",
        "auth": "postgresql+asyncpg://shared:b@db/prod?application_name=auth",
        "worker": "postgresql+asyncpg://worker:c@db/prod",
        "maintenance": "postgresql+asyncpg://maintenance:d@db/prod",
    }
    assert "runtime.config.login_reuse" in {
        item.code for item in validate_runtime_url_configuration(urls)
    }


def test_runtime_urls_must_target_same_database() -> None:
    urls = {
        "api": "postgresql+asyncpg://api:a@db/prod",
        "auth": "postgresql+asyncpg://auth:b@db/prod",
        "worker": "postgresql+asyncpg://worker:c@db/prod",
        "maintenance": "postgresql+asyncpg://maintenance:d@db/other",
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
        _canonical_observation("worker", "worker_login"),
        _canonical_observation("maintenance", "maintenance_login"),
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
