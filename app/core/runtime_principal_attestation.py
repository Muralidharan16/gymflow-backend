"""P2D runtime PostgreSQL principal binding and fail-closed attestation.

P2B owns cluster capability-role definitions. P2C owns capability reachability.
P2D binds deployment LOGIN roles to those capabilities and verifies the live
session identity that each application process actually uses.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Connection, Engine, make_url
from sqlalchemy.pool import NullPool

from app.core.cluster_identity_graph import load_identity_transition_policy
from app.core.cluster_role_contract import (
    ContractBundle,
    ContractViolation,
    load_contract_bundle,
)


RUNTIME_CONTRACT_DIRECTORY = (
    Path(__file__).resolve().parents[2] / "security" / "runtime_identity"
)
RUNTIME_BINDING_MANIFEST = "runtime_bindings.v1.json"
SEMANTICS = ("MEMBER", "USAGE", "SET")
DANGEROUS_LOGIN_ATTRIBUTES = (
    "superuser",
    "create_role",
    "create_db",
    "replication",
    "bypass_rls",
)


class RuntimePrincipalAttestationError(RuntimeError):
    """Raised when a configured runtime DB identity violates the P2D contract."""


@dataclass(frozen=True)
class RuntimeBinding:
    component: str
    environment_variable: str
    runtime_capability: str
    direct_capabilities: tuple[str, ...]


@dataclass(frozen=True)
class RuntimeBindingContract:
    approved_grantors: tuple[str, ...]
    membership_options: Mapping[str, bool]
    bindings: Mapping[str, RuntimeBinding]
    rules: Mapping[str, Any]


@dataclass(frozen=True)
class RuntimePrincipalObservation:
    component: str
    configured_username: str
    configured_database: str
    session_user: str
    current_user: str
    current_database: str
    row_security: str
    can_login: bool
    superuser: bool
    create_role: bool
    create_db: bool
    replication: bool
    bypass_rls: bool
    memberships: tuple[Mapping[str, Any], ...]
    semantic_checks: tuple[Mapping[str, Any], ...]


def _violation(code: str, subject: str, message: str) -> ContractViolation:
    return ContractViolation(code=code, subject=subject, message=message)


def _string_tuple(value: Any, field: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item for item in value)
        or len(value) != len(set(value))
    ):
        raise ValueError(f"{field} must be a non-empty list of unique strings")
    return tuple(value)


def load_runtime_binding_contract(
    directory: Path | None = None,
) -> RuntimeBindingContract:
    root = directory or RUNTIME_CONTRACT_DIRECTORY
    raw = json.loads((root / RUNTIME_BINDING_MANIFEST).read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping) or raw.get("schema_version") != 1:
        raise ValueError("unsupported P2D runtime binding manifest")

    approved_grantors = _string_tuple(raw.get("approved_grantors"), "approved_grantors")
    membership_options = raw.get("membership_options")
    if not isinstance(membership_options, Mapping):
        raise ValueError("membership_options must be an object")
    required_options = {"admin_option", "inherit_option", "set_option"}
    if set(membership_options) != required_options:
        raise ValueError(
            "membership_options must define exactly admin_option, inherit_option, and set_option"
        )
    if any(not isinstance(membership_options[name], bool) for name in required_options):
        raise ValueError("membership options must be boolean")

    raw_bindings = raw.get("bindings")
    if not isinstance(raw_bindings, Mapping) or not raw_bindings:
        raise ValueError("bindings must be a non-empty object")

    bindings: dict[str, RuntimeBinding] = {}
    for component, record in raw_bindings.items():
        if not isinstance(component, str) or not component:
            raise ValueError("runtime component names must be non-empty strings")
        if not isinstance(record, Mapping):
            raise ValueError(f"binding {component!r} must be an object")
        env_name = record.get("environment_variable")
        capability = record.get("runtime_capability")
        if not isinstance(env_name, str) or not env_name:
            raise ValueError(f"{component}.environment_variable must be set")
        if not isinstance(capability, str) or not capability:
            raise ValueError(f"{component}.runtime_capability must be set")
        bindings[component] = RuntimeBinding(
            component=component,
            environment_variable=env_name,
            runtime_capability=capability,
            direct_capabilities=_string_tuple(
                record.get("direct_capabilities"),
                f"{component}.direct_capabilities",
            ),
        )

    rules = raw.get("rules")
    if not isinstance(rules, Mapping):
        raise ValueError("rules must be an object")

    return RuntimeBindingContract(
        approved_grantors=approved_grantors,
        membership_options=dict(membership_options),
        bindings=bindings,
        rules=dict(rules),
    )


def validate_runtime_binding_contract(
    contract: RuntimeBindingContract,
    bundle: ContractBundle | None = None,
) -> tuple[ContractViolation, ...]:
    cluster = bundle or load_contract_bundle()
    p2c = load_identity_transition_policy()
    managed = set(cluster.roles.get("managed_roles", {}))
    peer_runtimes = set(p2c.peer_isolation_principals) - {p2c.migration_principal}
    violations: list[ContractViolation] = []

    required_components = {"api", "auth", "worker", "maintenance"}
    if set(contract.bindings) != required_components:
        violations.append(_violation(
            "runtime.contract.component_coverage",
            "bindings",
            "P2D must define exactly api/auth/worker/maintenance bindings.",
        ))

    runtime_capabilities = {binding.runtime_capability for binding in contract.bindings.values()}
    if runtime_capabilities != peer_runtimes:
        violations.append(_violation(
            "runtime.contract.p2c_runtime_coverage",
            "runtime_capabilities",
            "P2D runtime capabilities must exactly match P2C peer runtime capabilities; "
            f"expected={sorted(peer_runtimes)!r}, found={sorted(runtime_capabilities)!r}.",
        ))

    env_names = [binding.environment_variable for binding in contract.bindings.values()]
    if len(env_names) != len(set(env_names)):
        violations.append(_violation(
            "runtime.contract.environment_overlap",
            "environment_variables",
            "Each runtime component must bind to a distinct configuration key.",
        ))

    for binding in contract.bindings.values():
        if binding.runtime_capability not in binding.direct_capabilities:
            violations.append(_violation(
                "runtime.contract.primary_capability",
                binding.component,
                "The component's primary runtime capability must be directly granted.",
            ))
        unknown = sorted(set(binding.direct_capabilities) - managed)
        if unknown:
            violations.append(_violation(
                "runtime.contract.unknown_capability",
                binding.component,
                f"Direct capabilities are not P2B managed roles: {unknown!r}.",
            ))

    required_rules = {
        "runtime_logins_are_distinct",
        "all_runtime_logins_target_same_database",
        "reject_unknown_direct_memberships",
        "require_row_security",
        "require_baseline_current_user",
        "forbid_set_role_to_managed_roles",
    }
    disabled = sorted(rule for rule in required_rules if contract.rules.get(rule) is not True)
    if disabled:
        violations.append(_violation(
            "runtime.contract.required_rule",
            "rules",
            f"Required P2D rules must be true; invalid={disabled!r}.",
        ))

    if dict(contract.membership_options) != {
        "admin_option": False,
        "inherit_option": True,
        "set_option": False,
    }:
        violations.append(_violation(
            "runtime.contract.membership_options",
            "membership_options",
            "Runtime login overlays must inherit approved capabilities without ADMIN or SET ROLE delegation.",
        ))

    return tuple(sorted(set(violations)))


def evaluate_runtime_principal_observation(
    observation: RuntimePrincipalObservation,
    contract: RuntimeBindingContract | None = None,
    bundle: ContractBundle | None = None,
) -> tuple[ContractViolation, ...]:
    runtime_contract = contract or load_runtime_binding_contract()
    cluster = bundle or load_contract_bundle()
    contract_violations = validate_runtime_binding_contract(runtime_contract, cluster)
    if contract_violations:
        return contract_violations

    binding = runtime_contract.bindings.get(observation.component)
    if binding is None:
        return (_violation(
            "runtime.component.unknown",
            observation.component,
            "Observation component is not present in the P2D contract.",
        ),)

    violations: list[ContractViolation] = []
    managed = set(cluster.roles.get("managed_roles", {}))

    if observation.session_user != observation.configured_username:
        violations.append(_violation(
            "runtime.session_user_mismatch",
            observation.component,
            f"Configured login {observation.configured_username!r} authenticated as {observation.session_user!r}.",
        ))
    if observation.current_user != observation.session_user:
        violations.append(_violation(
            "runtime.current_user_mismatch",
            observation.component,
            f"Baseline current_user {observation.current_user!r} differs from session_user {observation.session_user!r}.",
        ))
    if observation.current_database != observation.configured_database:
        violations.append(_violation(
            "runtime.database_mismatch",
            observation.component,
            f"Configured database {observation.configured_database!r} resolved to {observation.current_database!r}.",
        ))
    if observation.row_security.lower() != "on":
        violations.append(_violation(
            "runtime.row_security_disabled",
            observation.component,
            "row_security must be on for every application runtime login.",
        ))
    if not observation.can_login:
        violations.append(_violation(
            "runtime.login_attribute",
            observation.session_user,
            "Deployment runtime identity must be a LOGIN role.",
        ))
    for attribute in DANGEROUS_LOGIN_ATTRIBUTES:
        if getattr(observation, attribute) is not False:
            violations.append(_violation(
                "runtime.dangerous_login_attribute",
                f"{observation.session_user}.{attribute}",
                "Deployment runtime login holds a forbidden cluster-level attribute.",
            ))
    if observation.session_user in managed:
        violations.append(_violation(
            "runtime.capability_used_as_login",
            observation.session_user,
            "P2B managed capability roles are NOLOGIN; deployment credentials must use a separate bounded login overlay.",
        ))

    expected_roles = set(binding.direct_capabilities)
    direct_rows = [
        row for row in observation.memberships
        if row.get("member_role") == observation.session_user
    ]
    found_roles = {str(row.get("granted_role")) for row in direct_rows}
    if found_roles != expected_roles:
        violations.append(_violation(
            "runtime.direct_membership_set",
            observation.component,
            "Runtime login must have the exact approved direct capability set; "
            f"missing={sorted(expected_roles-found_roles)!r}, extra={sorted(found_roles-expected_roles)!r}.",
        ))

    rows_by_role: dict[str, list[Mapping[str, Any]]] = {}
    for row in direct_rows:
        rows_by_role.setdefault(str(row.get("granted_role")), []).append(row)

    for role in sorted(expected_roles):
        rows = rows_by_role.get(role, [])
        if len(rows) != 1:
            violations.append(_violation(
                "runtime.membership_cardinality",
                f"{observation.session_user}->{role}",
                f"Expected exactly one direct membership row; found {len(rows)}.",
            ))
            continue
        row = rows[0]
        if row.get("grantor") not in runtime_contract.approved_grantors:
            violations.append(_violation(
                "runtime.membership_grantor",
                f"{observation.session_user}->{role}",
                f"Unapproved membership grantor {row.get('grantor')!r}.",
            ))
        for option, expected in runtime_contract.membership_options.items():
            if row.get(option) is not expected:
                violations.append(_violation(
                    "runtime.membership_option",
                    f"{observation.session_user}->{role}.{option}",
                    f"Expected {expected}; found {row.get(option)!r}.",
                ))

    observed_semantics = {
        (str(row.get("target")), str(row.get("semantic"))): row.get("allowed")
        for row in observation.semantic_checks
        if row.get("source") == observation.session_user
        and isinstance(row.get("target"), str)
        and row.get("semantic") in SEMANTICS
        and isinstance(row.get("allowed"), bool)
    }
    for role in sorted(managed):
        for semantic in SEMANTICS:
            key = (role, semantic)
            if key not in observed_semantics:
                violations.append(_violation(
                    "runtime.live_semantic_missing",
                    f"{observation.session_user}->{role}.{semantic}",
                    "Live pg_has_role evidence is required for every managed role.",
                ))
                continue
            expected = role in expected_roles and semantic in {"MEMBER", "USAGE"}
            if observed_semantics[key] is not expected:
                violations.append(_violation(
                    "runtime.semantic_reachability",
                    f"{observation.session_user}->{role}.{semantic}",
                    f"Expected PostgreSQL result {expected}; found {observed_semantics[key]!r}.",
                ))

    return tuple(sorted(set(violations)))


def evaluate_runtime_binding_set(
    observations: Sequence[RuntimePrincipalObservation],
    contract: RuntimeBindingContract | None = None,
) -> tuple[ContractViolation, ...]:
    runtime_contract = contract or load_runtime_binding_contract()
    violations: list[ContractViolation] = []

    by_component = {item.component: item for item in observations}
    if set(by_component) != set(runtime_contract.bindings) or len(by_component) != len(observations):
        violations.append(_violation(
            "runtime.binding_set.component_coverage",
            "observations",
            "Live P2D certification requires exactly one observation for each runtime component.",
        ))

    users = [item.session_user for item in observations]
    if len(users) != len(set(users)):
        violations.append(_violation(
            "runtime.binding_set.login_reuse",
            "session_user",
            "API, auth, worker, and maintenance must authenticate as distinct LOGIN roles.",
        ))

    databases = {item.current_database for item in observations}
    if len(databases) != 1:
        violations.append(_violation(
            "runtime.binding_set.database_divergence",
            "current_database",
            f"Runtime bindings must target one application database; found={sorted(databases)!r}.",
        ))

    return tuple(sorted(set(violations)))


def _psycopg_url(raw_url: str) -> str:
    parsed = make_url(raw_url)
    if not parsed.username:
        raise ValueError("runtime database URL must include a PostgreSQL username")
    if not parsed.database:
        raise ValueError("runtime database URL must include a database name")
    if not parsed.drivername.startswith("postgresql"):
        raise ValueError("runtime database URL must use PostgreSQL")
    return parsed.set(drivername="postgresql+psycopg").render_as_string(hide_password=False)


def _capture_runtime_principal(
    connection: Connection,
    component: str,
    configured_username: str,
    configured_database: str,
    bundle: ContractBundle,
) -> RuntimePrincipalObservation:
    identity = connection.execute(text("""
        SELECT session_user::text AS session_user,
               current_user::text AS current_user,
               current_database()::text AS current_database,
               current_setting('row_security')::text AS row_security
    """)).mappings().one()

    role = connection.execute(text("""
        SELECT rolcanlogin AS can_login,
               rolsuper AS superuser,
               rolcreaterole AS create_role,
               rolcreatedb AS create_db,
               rolreplication AS replication,
               rolbypassrls AS bypass_rls
        FROM pg_catalog.pg_roles
        WHERE rolname = session_user
    """)).mappings().one()

    memberships = tuple(dict(row) for row in connection.execute(text("""
        SELECT granted.rolname::text AS granted_role,
               member.rolname::text AS member_role,
               grantor.rolname::text AS grantor,
               membership.set_option,
               membership.inherit_option,
               membership.admin_option
        FROM pg_catalog.pg_auth_members AS membership
        JOIN pg_catalog.pg_roles AS granted ON granted.oid = membership.roleid
        JOIN pg_catalog.pg_roles AS member ON member.oid = membership.member
        JOIN pg_catalog.pg_roles AS grantor ON grantor.oid = membership.grantor
        WHERE member.rolname = session_user
        ORDER BY granted.rolname, grantor.rolname
    """)).mappings().all())

    semantic_checks: list[Mapping[str, Any]] = []
    for target in sorted(bundle.roles.get("managed_roles", {})):
        for semantic in SEMANTICS:
            allowed = connection.scalar(
                text("SELECT pg_catalog.pg_has_role(session_user, :target, :semantic)"),
                {"target": target, "semantic": semantic},
            )
            semantic_checks.append({
                "source": str(identity["session_user"]),
                "target": target,
                "semantic": semantic,
                "allowed": bool(allowed),
            })

    return RuntimePrincipalObservation(
        component=component,
        configured_username=configured_username,
        configured_database=configured_database,
        session_user=str(identity["session_user"]),
        current_user=str(identity["current_user"]),
        current_database=str(identity["current_database"]),
        row_security=str(identity["row_security"]),
        can_login=bool(role["can_login"]),
        superuser=bool(role["superuser"]),
        create_role=bool(role["create_role"]),
        create_db=bool(role["create_db"]),
        replication=bool(role["replication"]),
        bypass_rls=bool(role["bypass_rls"]),
        memberships=memberships,
        semantic_checks=tuple(semantic_checks),
    )


def capture_runtime_url_observation(
    component: str,
    raw_url: str,
    bundle: ContractBundle | None = None,
) -> RuntimePrincipalObservation:
    cluster = bundle or load_contract_bundle()
    parsed = make_url(raw_url)
    configured_username = parsed.username or ""
    configured_database = parsed.database or ""
    engine = create_engine(_psycopg_url(raw_url), poolclass=NullPool, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            return _capture_runtime_principal(
                connection,
                component,
                configured_username,
                configured_database,
                cluster,
            )
    finally:
        engine.dispose()


def _format_violations(violations: Sequence[ContractViolation]) -> str:
    return "; ".join(
        f"[{item.code}] {item.subject}: {item.message}"
        for item in violations
    )


def attest_runtime_url_binding(
    component: str,
    raw_url: str,
    contract: RuntimeBindingContract | None = None,
    bundle: ContractBundle | None = None,
) -> RuntimePrincipalObservation:
    runtime_contract = contract or load_runtime_binding_contract()
    cluster = bundle or load_contract_bundle()
    observation = capture_runtime_url_observation(component, raw_url, cluster)
    violations = evaluate_runtime_principal_observation(observation, runtime_contract, cluster)
    if violations:
        raise RuntimePrincipalAttestationError(_format_violations(violations))
    return observation


def configured_runtime_urls(components: Sequence[str] | None = None) -> dict[str, str]:
    contract = load_runtime_binding_contract()
    selected = tuple(components or contract.bindings)
    urls: dict[str, str] = {}
    for component in selected:
        binding = contract.bindings.get(component)
        if binding is None:
            raise RuntimePrincipalAttestationError(f"Unknown P2D runtime component: {component!r}")
        value = os.environ.get(binding.environment_variable, "").strip()
        if not value:
            raise RuntimePrincipalAttestationError(
                f"{binding.environment_variable} is required to attest {component}"
            )
        urls[component] = value
    return urls


def attest_configured_runtime_bindings(
    components: Sequence[str] | None = None,
) -> tuple[RuntimePrincipalObservation, ...]:
    contract = load_runtime_binding_contract()
    contract_violations = validate_runtime_binding_contract(contract)
    if contract_violations:
        raise RuntimePrincipalAttestationError(_format_violations(contract_violations))

    urls = configured_runtime_urls(components)
    observations = tuple(
        attest_runtime_url_binding(component, raw_url, contract)
        for component, raw_url in urls.items()
    )
    if components is None:
        set_violations = evaluate_runtime_binding_set(observations, contract)
        if set_violations:
            raise RuntimePrincipalAttestationError(_format_violations(set_violations))
    return observations


def validate_runtime_url_configuration(
    urls: Mapping[str, str],
) -> tuple[ContractViolation, ...]:
    """Pure semantic validation for production settings before any connection."""

    violations: list[ContractViolation] = []
    parsed: dict[str, Any] = {}
    for component, raw_url in urls.items():
        try:
            value = make_url(raw_url)
        except Exception as exc:
            violations.append(_violation(
                "runtime.config.invalid_url",
                component,
                f"Database URL cannot be parsed: {type(exc).__name__}.",
            ))
            continue
        if not value.drivername.startswith("postgresql"):
            violations.append(_violation(
                "runtime.config.not_postgresql",
                component,
                "Runtime database URL must use PostgreSQL.",
            ))
        if not value.username:
            violations.append(_violation(
                "runtime.config.missing_username",
                component,
                "Runtime database URL must include a PostgreSQL login.",
            ))
        if not value.database:
            violations.append(_violation(
                "runtime.config.missing_database",
                component,
                "Runtime database URL must include the application database name.",
            ))
        parsed[component] = value

    usernames = [item.username for item in parsed.values() if item.username]
    if len(usernames) != len(set(usernames)):
        violations.append(_violation(
            "runtime.config.login_reuse",
            "runtime_database_urls",
            "API, auth, worker, and maintenance URLs must use distinct PostgreSQL logins.",
        ))

    databases = {item.database for item in parsed.values() if item.database}
    if len(databases) > 1:
        violations.append(_violation(
            "runtime.config.database_divergence",
            "runtime_database_urls",
            f"Runtime URLs must target one application database; found={sorted(databases)!r}.",
        ))

    return tuple(sorted(set(violations)))


def _cheap_identity_query(dbapi_connection) -> tuple[str, str, str, str]:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(
            "SELECT session_user::text, current_user::text, current_database()::text, "
            "current_setting('row_security')::text"
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimePrincipalAttestationError(
                "runtime identity guard returned no PostgreSQL identity row"
            )
        return str(row[0]), str(row[1]), str(row[2]), str(row[3])
    finally:
        cursor.close()


def install_connection_identity_guard(engine: Engine, component: str, raw_url: str) -> None:
    """Install a cheap physical-connection and pool-checkout identity guard."""

    parsed = make_url(raw_url)
    expected_user = parsed.username or ""
    expected_database = parsed.database or ""

    def _assert_identity(dbapi_connection) -> None:
        session_user, current_user, database, row_security = _cheap_identity_query(dbapi_connection)
        if session_user != expected_user:
            raise RuntimePrincipalAttestationError(f"{component}: PostgreSQL session_user mismatch")
        if current_user != session_user:
            raise RuntimePrincipalAttestationError(f"{component}: pooled current_user is contaminated")
        if database != expected_database:
            raise RuntimePrincipalAttestationError(f"{component}: PostgreSQL database mismatch")
        if row_security.lower() != "on":
            raise RuntimePrincipalAttestationError(f"{component}: pooled row_security is disabled")

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, _connection_record) -> None:
        _assert_identity(dbapi_connection)

    @event.listens_for(engine, "checkout")
    def _on_checkout(dbapi_connection, _connection_record, _connection_proxy) -> None:
        _assert_identity(dbapi_connection)
