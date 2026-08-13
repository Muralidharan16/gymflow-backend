from __future__ import annotations

import ast
import re
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from app.core.cluster_identity_graph import (
    IdentityGraphCatalog,
    evaluate_identity_graph_catalog,
    governed_semantic_expectations,
    load_identity_transition_policy,
    validate_identity_transition_policy,
)
from app.core.cluster_role_contract import ContractBundle, load_contract_bundle


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "app/core/cluster_identity_graph.py"
ALEMBIC_ENV = ROOT / "alembic/env.py"


def _canonical_catalog(
    bundle: ContractBundle | None = None,
) -> tuple[ContractBundle, object, IdentityGraphCatalog]:
    bundle = bundle or load_contract_bundle()
    policy = load_identity_transition_policy()
    roles = tuple(
        {"role": role, **record["attributes"]}
        for role, record in bundle.roles["managed_roles"].items()
    )
    memberships = tuple(
        {
            "granted_role": row["granted_role"],
            "member_role": row["member_role"],
            "grantor": row["approved_grantor"],
            "set_option": row["set_option"],
            "inherit_option": row["inherit_option"],
            "admin_option": row["admin_option"],
        }
        for row in bundle.memberships["exact_rows"]
    )
    semantic_checks = tuple(
        {
            "source": source,
            "target": target,
            "semantic": semantic,
            "allowed": allowed,
        }
        for (source, target, semantic), allowed in sorted(
            governed_semantic_expectations(bundle, policy).items()
        )
    )
    return bundle, policy, IdentityGraphCatalog(
        roles=roles,
        memberships=memberships,
        semantic_checks=semantic_checks,
    )


def _with_memberships(
    catalog: IdentityGraphCatalog,
    *rows: dict[str, object],
) -> IdentityGraphCatalog:
    return replace(catalog, memberships=(*catalog.memberships, *rows))


def _codes(catalog: IdentityGraphCatalog) -> set[str]:
    bundle = load_contract_bundle()
    policy = load_identity_transition_policy()
    return {
        item.code
        for item in evaluate_identity_graph_catalog(catalog, bundle, policy)
    }


def _violations(catalog: IdentityGraphCatalog):
    bundle = load_contract_bundle()
    policy = load_identity_transition_policy()
    return evaluate_identity_graph_catalog(catalog, bundle, policy)


def test_policy_classifies_every_managed_role_exactly_once() -> None:
    bundle, policy, _ = _canonical_catalog()
    assert validate_identity_transition_policy(bundle, policy) == ()
    classified = (
        *policy.peer_isolation_principals,
        *policy.migration_helpers,
        *policy.ordinary_capabilities,
    )
    assert len(classified) == len(set(classified))
    assert set(classified) == set(bundle.roles["managed_roles"])


def test_canonical_graph_and_live_semantic_projection_pass() -> None:
    bundle, policy, catalog = _canonical_catalog()
    assert evaluate_identity_graph_catalog(catalog, bundle, policy) == ()


def test_direct_member_only_peer_edge_is_rejected() -> None:
    _, _, catalog = _canonical_catalog()
    drifted = _with_memberships(
        catalog,
        {
            "granted_role": "auth_runtime",
            "member_role": "app_runtime",
            "grantor": "postgres",
            "set_option": False,
            "inherit_option": False,
            "admin_option": False,
        },
    )
    assert "identity.member_reachability" in _codes(drifted)


def test_transitive_set_bridge_reports_intermediate_path() -> None:
    _, _, catalog = _canonical_catalog()
    drifted = _with_memberships(
        catalog,
        {
            "granted_role": "p2c_bridge",
            "member_role": "worker_runtime",
            "grantor": "postgres",
            "set_option": True,
            "inherit_option": False,
            "admin_option": False,
        },
        {
            "granted_role": "auth_runtime",
            "member_role": "p2c_bridge",
            "grantor": "postgres",
            "set_option": True,
            "inherit_option": False,
            "admin_option": False,
        },
    )
    assert any(
        item.code == "identity.set_reachability"
        and "worker_runtime --SET--> p2c_bridge --SET--> auth_runtime" in item.message
        for item in _violations(drifted)
    )


def test_transitive_usage_bridge_is_rejected() -> None:
    _, _, catalog = _canonical_catalog()
    drifted = _with_memberships(
        catalog,
        {
            "granted_role": "p2c_bridge",
            "member_role": "app_runtime",
            "grantor": "postgres",
            "set_option": False,
            "inherit_option": True,
            "admin_option": False,
        },
        {
            "granted_role": "worker_runtime",
            "member_role": "p2c_bridge",
            "grantor": "postgres",
            "set_option": False,
            "inherit_option": True,
            "admin_option": False,
        },
    )
    assert "identity.usage_reachability" in _codes(drifted)


def test_migration_helper_cannot_bridge_to_runtime() -> None:
    _, _, catalog = _canonical_catalog()
    drifted = _with_memberships(
        catalog,
        {
            "granted_role": "worker_runtime",
            "member_role": "app_security_owner",
            "grantor": "postgres",
            "set_option": True,
            "inherit_option": False,
            "admin_option": False,
        },
    )
    codes = _codes(drifted)
    assert "identity.outgoing_membership" in codes
    assert "identity.set_reachability" in codes


def test_migration_helpers_are_non_delegable_to_arbitrary_logins() -> None:
    _, _, catalog = _canonical_catalog()
    drifted = _with_memberships(
        catalog,
        {
            "granted_role": "app_rls_executor",
            "member_role": "unexpected_login",
            "grantor": "postgres",
            "set_option": True,
            "inherit_option": False,
            "admin_option": False,
        },
    )
    assert "identity.non_delegable_grant" in _codes(drifted)


def test_admin_option_on_protected_capability_is_rejected() -> None:
    _, _, catalog = _canonical_catalog()
    drifted = _with_memberships(
        catalog,
        {
            "granted_role": "auth_runtime",
            "member_role": "unexpected_login",
            "grantor": "postgres",
            "set_option": False,
            "inherit_option": False,
            "admin_option": True,
        },
    )
    assert "identity.admin_delegation" in _codes(drifted)


def test_set_reachable_admin_holder_is_graph_mutation_path() -> None:
    _, _, catalog = _canonical_catalog()
    drifted = _with_memberships(
        catalog,
        {
            "granted_role": "p2c_admin_bridge",
            "member_role": "app_runtime",
            "grantor": "postgres",
            "set_option": True,
            "inherit_option": False,
            "admin_option": False,
        },
        {
            "granted_role": "auth_runtime",
            "member_role": "p2c_admin_bridge",
            "grantor": "postgres",
            "set_option": False,
            "inherit_option": False,
            "admin_option": True,
        },
    )
    assert any(
        item.code == "identity.admin_escalation"
        and item.subject == "app_runtime->auth_runtime"
        for item in _violations(drifted)
    )


def test_protected_createrole_is_rejected() -> None:
    _, _, catalog = _canonical_catalog()
    roles = [dict(row) for row in catalog.roles]
    next(row for row in roles if row["role"] == "worker_runtime")["create_role"] = True
    drifted = replace(catalog, roles=tuple(roles))
    assert "identity.graph_mutation_attribute" in _codes(drifted)


def test_postgresql_semantics_must_agree_with_pure_graph() -> None:
    _, _, catalog = _canonical_catalog()
    checks = [dict(row) for row in catalog.semantic_checks]
    check = next(
        row for row in checks
        if row["source"] == "app_runtime"
        and row["target"] == "auth_runtime"
        and row["semantic"] == "SET"
    )
    check["allowed"] = True
    drifted = replace(catalog, semantic_checks=tuple(checks))
    codes = _codes(drifted)
    assert "identity.semantic_disagreement" in codes
    assert "identity.postgresql_semantics" in codes


def test_missing_live_pg_has_role_evidence_fails_closed() -> None:
    _, _, catalog = _canonical_catalog()
    drifted = replace(catalog, semantic_checks=catalog.semantic_checks[:-1])
    assert "identity.live_semantic_missing" in _codes(drifted)


def test_future_managed_role_requires_explicit_p2c_classification() -> None:
    bundle, policy, _ = _canonical_catalog()
    roles = deepcopy(bundle.roles)
    roles["managed_roles"]["future_runtime"] = deepcopy(
        roles["managed_roles"]["worker_runtime"]
    )
    roles["managed_roles"]["future_runtime"]["role"] = "future_runtime"
    future = ContractBundle(
        roles=roles,
        role_settings=bundle.role_settings,
        memberships=bundle.memberships,
        grantors=bundle.grantors,
        ownership=bundle.ownership,
    )
    assert any(
        item.code == "identity.policy.coverage"
        for item in validate_identity_transition_policy(future, policy)
    )


def test_live_identity_graph_sql_is_select_only() -> None:
    source = MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    mutation = re.compile(
        r"\b(?:INSERT|UPDATE|DELETE|ALTER|CREATE|DROP|GRANT|REVOKE|TRUNCATE)\b",
        flags=re.IGNORECASE,
    )
    sql_literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "SELECT" in node.value.upper()
    ]
    assert sql_literals
    for sql in sql_literals:
        assert mutation.search(sql) is None


def test_alembic_head_runs_p2c_after_p2a_and_before_configure() -> None:
    source = ALEMBIC_ENV.read_text(encoding="utf-8")
    function = source[source.index("def do_run_migrations"):source.index("async def run_async_migrations")]
    p2a = function.index("assert_external_role_preflight(connection)")
    p2c = function.index("assert_identity_graph_preflight(connection)")
    configure = function.index("context.configure(")
    assert p2a < p2c < configure
