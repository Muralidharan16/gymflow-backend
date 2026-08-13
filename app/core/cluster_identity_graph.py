"""PostgreSQL 16 P2C identity non-escalation contract and live verifier.

Membership edges point from member -> granted role. MEMBER follows every edge,
SET only ``set_option`` edges, and USAGE only ``inherit_option`` edges. The
live proof compares this pure graph model with PostgreSQL ``pg_has_role``.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.core.cluster_role_contract import (
    CONTRACT_DIRECTORY,
    ContractBundle,
    ContractViolation,
    load_contract_bundle,
)

MANIFEST = "identity_transitions.v1.json"
SEMANTICS = ("MEMBER", "SET", "USAGE")
DANGEROUS_ATTRIBUTES = (
    "superuser",
    "create_role",
    "create_db",
    "replication",
    "bypass_rls",
)


@dataclass(frozen=True)
class IdentityTransitionPolicy:
    migration_principal: str
    peer_isolation_principals: tuple[str, ...]
    migration_helpers: tuple[str, ...]
    ordinary_capabilities: tuple[str, ...]
    rules: Mapping[str, Any]


@dataclass(frozen=True)
class IdentityGraphCatalog:
    roles: tuple[Mapping[str, Any], ...]
    memberships: tuple[Mapping[str, Any], ...]
    semantic_checks: tuple[Mapping[str, Any], ...] = ()


def _violation(code: str, subject: str, message: str) -> ContractViolation:
    return ContractViolation(code=code, subject=subject, message=message)


def _role_list(raw: Mapping[str, Any], field: str) -> tuple[str, ...]:
    value = raw.get(field)
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item for item in value)
        or len(value) != len(set(value))
    ):
        raise ValueError(f"{field} must be a non-empty list of unique role names")
    return tuple(value)


def load_identity_transition_policy(
    directory: Path | None = None,
) -> IdentityTransitionPolicy:
    root = directory or CONTRACT_DIRECTORY
    raw = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping) or raw.get("schema_version") != 1:
        raise ValueError("unsupported P2C identity transition manifest")
    migration = raw.get("migration_principal")
    rules = raw.get("rules")
    if not isinstance(migration, str) or not migration:
        raise ValueError("migration_principal must be a non-empty role name")
    if not isinstance(rules, Mapping):
        raise ValueError("identity transition rules must be an object")
    return IdentityTransitionPolicy(
        migration_principal=migration,
        peer_isolation_principals=_role_list(raw, "peer_isolation_principals"),
        migration_helpers=_role_list(raw, "migration_helpers"),
        ordinary_capabilities=_role_list(raw, "ordinary_capabilities"),
        rules=dict(rules),
    )


def _helper_pairs(
    bundle: ContractBundle,
    policy: IdentityTransitionPolicy,
) -> set[tuple[str, str]]:
    helpers = set(policy.migration_helpers)
    return {
        (policy.migration_principal, str(row["granted_role"]))
        for row in bundle.memberships.get("exact_rows", [])
        if isinstance(row, Mapping)
        and row.get("member_role") == policy.migration_principal
        and row.get("granted_role") in helpers
    }


def validate_identity_transition_policy(
    bundle: ContractBundle,
    policy: IdentityTransitionPolicy,
) -> tuple[ContractViolation, ...]:
    violations: list[ContractViolation] = []
    managed = set(bundle.roles.get("managed_roles", {}))
    groups = (
        policy.peer_isolation_principals,
        policy.migration_helpers,
        policy.ordinary_capabilities,
    )
    flattened = [role for group in groups for role in group]
    classified = set(flattened)
    if len(classified) != len(flattened):
        violations.append(_violation(
            "identity.policy.overlap",
            "managed_role_classes",
            "A managed role appears in more than one P2C identity class.",
        ))
    if classified != managed:
        violations.append(_violation(
            "identity.policy.coverage",
            "managed_roles",
            "P2C classes must exactly cover managed roles; "
            f"missing={sorted(managed-classified)!r}, extra={sorted(classified-managed)!r}.",
        ))
    if policy.migration_principal not in policy.peer_isolation_principals:
        violations.append(_violation(
            "identity.policy.migration_principal",
            policy.migration_principal,
            "Migration principal must be a peer-isolation principal.",
        ))
    expected_helpers = {
        (policy.migration_principal, helper)
        for helper in policy.migration_helpers
    }
    if _helper_pairs(bundle, policy) != expected_helpers:
        violations.append(_violation(
            "identity.policy.helper_contract",
            policy.migration_principal,
            "Migration helper class must exactly match canonical membership rows.",
        ))
    required_rules = {
        "classify_every_managed_role",
        "forbid_unapproved_outgoing_membership_from_protected_roles",
        "forbid_admin_option_on_protected_capability_grants",
        "migration_helpers_are_non_delegable",
        "prove_member_set_usage_semantics",
    }
    disabled = sorted(
        rule for rule in required_rules if policy.rules.get(rule) is not True
    )
    if disabled:
        violations.append(_violation(
            "identity.policy.required_rule",
            "rules",
            f"Required P2C rules must be true; invalid={disabled!r}.",
        ))
    return tuple(sorted(violations))


def _pair(row: Mapping[str, Any]) -> tuple[str | None, str | None]:
    member, granted = row.get("member_role"), row.get("granted_role")
    return (
        member if isinstance(member, str) else None,
        granted if isinstance(granted, str) else None,
    )


def _graph(
    memberships: Sequence[Mapping[str, Any]],
    semantic: str,
) -> dict[str, set[str]]:
    option = {
        "MEMBER": None,
        "SET": "set_option",
        "USAGE": "inherit_option",
    }[semantic]
    result: dict[str, set[str]] = {}
    for row in memberships:
        source, target = _pair(row)
        if source is None or target is None:
            continue
        if option is not None and row.get(option) is not True:
            continue
        result.setdefault(source, set()).add(target)
    return result


def _path(
    graph: Mapping[str, set[str]],
    source: str,
    target: str,
) -> tuple[str, ...] | None:
    queue: deque[tuple[str, tuple[str, ...]]] = deque([(source, (source,))])
    visited = {source}
    while queue:
        node, path = queue.popleft()
        for neighbor in sorted(graph.get(node, ())):
            if neighbor in visited:
                continue
            candidate = (*path, neighbor)
            if neighbor == target:
                return candidate
            visited.add(neighbor)
            queue.append((neighbor, candidate))
    return None


def _render_path(path: Sequence[str], semantic: str) -> str:
    return f" --{semantic}--> ".join(path)


def prohibited_pairs(
    policy: IdentityTransitionPolicy,
) -> set[tuple[str, str]]:
    peers = set(policy.peer_isolation_principals)
    helpers = set(policy.migration_helpers)
    runtimes = peers - {policy.migration_principal}
    pairs = {(a, b) for a in peers for b in peers if a != b}
    pairs |= {
        (source, helper)
        for source in runtimes
        for helper in helpers
    }
    pairs |= {
        (helper, peer)
        for helper in helpers
        for peer in peers
    }
    pairs |= {(a, b) for a in helpers for b in helpers if a != b}
    return pairs


def governed_semantic_expectations(
    bundle: ContractBundle,
    policy: IdentityTransitionPolicy,
) -> dict[tuple[str, str, str], bool]:
    expected = {
        (source, target, semantic): False
        for source, target in prohibited_pairs(policy)
        for semantic in SEMANTICS
    }
    for source, target in _helper_pairs(bundle, policy):
        expected[(source, target, "MEMBER")] = True
        expected[(source, target, "SET")] = True
        expected[(source, target, "USAGE")] = False
    return expected


def _role_attribute_violations(
    catalog: IdentityGraphCatalog,
    protected: set[str],
) -> list[ContractViolation]:
    by_name: dict[str, list[Mapping[str, Any]]] = {}
    for row in catalog.roles:
        if isinstance(row.get("role"), str):
            by_name.setdefault(str(row["role"]), []).append(row)
    violations: list[ContractViolation] = []
    for role in sorted(protected):
        rows = by_name.get(role, [])
        if len(rows) != 1:
            violations.append(_violation(
                "identity.role_cardinality",
                role,
                f"Expected exactly one protected role row; found {len(rows)}.",
            ))
            continue
        for attribute in DANGEROUS_ATTRIBUTES:
            if rows[0].get(attribute) is not False:
                violations.append(_violation(
                    "identity.graph_mutation_attribute",
                    f"{role}.{attribute}",
                    "Protected roles must not hold cluster-level escalation attributes.",
                ))
    return violations


def evaluate_identity_graph_catalog(
    catalog: IdentityGraphCatalog,
    bundle: ContractBundle | None = None,
    policy: IdentityTransitionPolicy | None = None,
    *,
    require_live_semantics: bool = True,
) -> tuple[ContractViolation, ...]:
    contract = bundle or load_contract_bundle()
    policy = policy or load_identity_transition_policy()
    policy_violations = validate_identity_transition_policy(contract, policy)
    if policy_violations:
        return policy_violations

    peers = set(policy.peer_isolation_principals)
    helpers = set(policy.migration_helpers)
    protected = peers | helpers
    allowed = _helper_pairs(contract, policy)
    violations = _role_attribute_violations(catalog, protected)

    for row in catalog.memberships:
        source, target = _pair(row)
        if source is None or target is None:
            continue
        if source in protected and (source, target) not in allowed:
            violations.append(_violation(
                "identity.outgoing_membership",
                f"{source}->{target}",
                "Protected roles may not accumulate unapproved outgoing memberships.",
            ))
        if (
            target in helpers | {policy.migration_principal}
            and (source, target) not in allowed
        ):
            violations.append(_violation(
                "identity.non_delegable_grant",
                f"{source}->{target}",
                "Migration identity/helper roles are non-delegable.",
            ))
        if target in protected and row.get("admin_option") is True:
            violations.append(_violation(
                "identity.admin_delegation",
                f"{source}->{target}",
                "ADMIN OPTION is forbidden on protected capability grants.",
            ))

    graphs = {
        semantic: _graph(catalog.memberships, semantic)
        for semantic in SEMANTICS
    }
    expectations = governed_semantic_expectations(contract, policy)
    for (source, target, semantic), expected in sorted(expectations.items()):
        path = _path(graphs[semantic], source, target)
        if (path is not None) != expected:
            code = (
                "identity.allowed_helper_semantics"
                if expected
                else f"identity.{semantic.lower()}_reachability"
            )
            detail = _render_path(path, semantic) if path else "no path"
            violations.append(_violation(
                code,
                f"{source}->{target}.{semantic}",
                f"Expected {expected}; graph resolved {path is not None} via {detail}.",
            ))

    nodes = {
        name
        for row in catalog.memberships
        for name in _pair(row)
        if name
    }
    admin_rows = [
        row for row in catalog.memberships
        if row.get("admin_option") is True
    ]
    for source in sorted(protected):
        reachable = {source} | {
            node for node in nodes
            if _path(graphs["SET"], source, node)
        }
        for row in admin_rows:
            holder, target = _pair(row)
            if (
                holder in reachable
                and target in protected
                and (holder, target) not in allowed
            ):
                prefix = _path(graphs["SET"], source, str(holder))
                violations.append(_violation(
                    "identity.admin_escalation",
                    f"{source}->{target}",
                    "Graph can be mutated via "
                    f"{_render_path(prefix, 'SET') if prefix else source} "
                    f"--ADMIN--> {target}.",
                ))

    observed = {
        (
            str(row["source"]),
            str(row["target"]),
            str(row["semantic"]),
        ): row["allowed"]
        for row in catalog.semantic_checks
        if isinstance(row.get("source"), str)
        and isinstance(row.get("target"), str)
        and row.get("semantic") in SEMANTICS
        and isinstance(row.get("allowed"), bool)
    }
    if require_live_semantics:
        for source, target, semantic in sorted(
            set(expectations) - set(observed)
        ):
            violations.append(_violation(
                "identity.live_semantic_missing",
                f"{source}->{target}.{semantic}",
                "Live pg_has_role evidence is required for every governed pair.",
            ))
    for key, expected in expectations.items():
        if key not in observed:
            continue
        source, target, semantic = key
        graph_value = _path(graphs[semantic], source, target) is not None
        if observed[key] != graph_value:
            violations.append(_violation(
                "identity.semantic_disagreement",
                f"{source}->{target}.{semantic}",
                f"Pure graph={graph_value}; PostgreSQL pg_has_role={observed[key]}.",
            ))
        if observed[key] != expected:
            violations.append(_violation(
                "identity.postgresql_semantics",
                f"{source}->{target}.{semantic}",
                f"Expected PostgreSQL result {expected}; found {observed[key]}.",
            ))
    return tuple(sorted(set(violations)))


def _rows(
    connection: Connection,
    sql: str,
) -> tuple[Mapping[str, Any], ...]:
    return tuple(
        dict(row)
        for row in connection.execute(text(sql)).mappings().all()
    )


def capture_identity_graph_catalog(
    connection: Connection,
    bundle: ContractBundle | None = None,
    policy: IdentityTransitionPolicy | None = None,
) -> IdentityGraphCatalog:
    contract = bundle or load_contract_bundle()
    policy = policy or load_identity_transition_policy()
    violations = validate_identity_transition_policy(contract, policy)
    if violations:
        raise RuntimeError(
            "Invalid P2C identity policy: "
            + "; ".join(
                f"[{item.code}] {item.subject}: {item.message}"
                for item in violations
            )
        )

    roles = _rows(connection, """
        SELECT rolname::text AS role,
               rolsuper AS superuser,
               rolcreaterole AS create_role,
               rolcreatedb AS create_db,
               rolreplication AS replication,
               rolbypassrls AS bypass_rls
        FROM pg_catalog.pg_roles
        ORDER BY rolname
    """)
    memberships = _rows(connection, """
        SELECT granted.rolname::text AS granted_role,
               member.rolname::text AS member_role,
               grantor.rolname::text AS grantor,
               membership.set_option,
               membership.inherit_option,
               membership.admin_option
        FROM pg_catalog.pg_auth_members AS membership
        JOIN pg_catalog.pg_roles AS granted
          ON granted.oid = membership.roleid
        JOIN pg_catalog.pg_roles AS member
          ON member.oid = membership.member
        JOIN pg_catalog.pg_roles AS grantor
          ON grantor.oid = membership.grantor
        ORDER BY member.rolname, granted.rolname, grantor.rolname,
                 membership.set_option, membership.inherit_option,
                 membership.admin_option
    """)
    checks = []
    for source, target, semantic in sorted(
        governed_semantic_expectations(contract, policy)
    ):
        allowed = connection.execute(
            text(
                "SELECT pg_catalog.pg_has_role(:source, :target, :semantic)"
            ),
            {"source": source, "target": target, "semantic": semantic},
        ).scalar_one()
        checks.append({
            "source": source,
            "target": target,
            "semantic": semantic,
            "allowed": bool(allowed),
        })
    return IdentityGraphCatalog(roles, memberships, tuple(checks))


def assert_identity_graph_preflight(
    connection: Connection,
    bundle: ContractBundle | None = None,
    policy: IdentityTransitionPolicy | None = None,
) -> None:
    """Fail closed before Alembic HEAD; never repair cluster identity state."""
    if connection.in_transaction():
        raise RuntimeError(
            "PostgreSQL identity-graph preflight requires a pristine connection."
        )
    violations: tuple[ContractViolation, ...] = ()
    try:
        contract = bundle or load_contract_bundle()
        policy = policy or load_identity_transition_policy()
        catalog = capture_identity_graph_catalog(connection, contract, policy)
        violations = evaluate_identity_graph_catalog(
            catalog,
            contract,
            policy,
        )
    finally:
        if connection.in_transaction():
            connection.rollback()
    if violations:
        details = "\n".join(
            f" - [{item.code}] {item.subject}: {item.message}"
            for item in violations
        )
        raise RuntimeError(
            "PostgreSQL identity non-escalation preflight failed before Alembic HEAD. "
            "Repair cluster identity topology outside Alembic.\n" + details
        )
