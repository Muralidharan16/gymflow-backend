"""Live, read-only PostgreSQL cluster-role preflight for Alembic HEAD.

The machine-readable manifests in ``security/cluster_role_bootstrap`` are the
source of truth. This module only captures the live PostgreSQL catalog through
an already-open migration connection and delegates semantic validation to the
pure ``cluster_role_contract`` validator.

It intentionally owns no bootstrap/repair behavior: a missing or drifted role,
setting, or migration-owner membership is a deployment failure, not something
Alembic may silently fix.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.core.cluster_role_contract import (
    ContractBundle,
    ContractViolation,
    load_contract_bundle,
    validate_catalog_snapshot,
)


@dataclass(frozen=True)
class ExternalRoleCatalog:
    """Normalized live catalog evidence consumed by the contract evaluators."""

    current_user: str
    session_user: str
    snapshot: Mapping[str, Any]
    database_setting_overrides: tuple[Mapping[str, str], ...] = ()
    duplicate_global_settings: tuple[Mapping[str, str], ...] = ()


def _mapping_rows(connection: Connection, sql: str) -> list[dict[str, Any]]:
    result = connection.execute(text(sql))
    return [dict(row) for row in result.mappings().all()]


def _setting_pair(raw_setting: object) -> tuple[str, str]:
    if not isinstance(raw_setting, str) or "=" not in raw_setting:
        raise RuntimeError(
            "PostgreSQL returned an invalid pg_db_role_setting entry: "
            f"{raw_setting!r}"
        )
    key, value = raw_setting.split("=", 1)
    if not key:
        raise RuntimeError("PostgreSQL returned an empty role-setting name")
    return key, value


def _retired_role_names(contract: ContractBundle) -> set[str]:
    retired = contract.roles.get("retired_roles", {})
    if not isinstance(retired, Mapping):
        raise RuntimeError("roles.retired_roles must be an object")
    return {str(role) for role in retired}


def capture_external_role_catalog(
    connection: Connection,
    bundle: ContractBundle | None = None,
) -> ExternalRoleCatalog:
    """Capture the governed role surface without mutating cluster/database state."""

    contract = bundle or load_contract_bundle()
    managed_roles = set(contract.roles["managed_roles"])
    governed_roles = managed_roles | _retired_role_names(contract)

    identity = connection.execute(
        text(
            """
            SELECT current_user::text AS current_user,
                   session_user::text AS session_user
            """
        )
    ).mappings().one()

    role_rows = _mapping_rows(
        connection,
        """
        SELECT rolname::text AS role,
               rolsuper AS superuser,
               rolinherit AS inherit,
               rolcreaterole AS create_role,
               rolcreatedb AS create_db,
               rolcanlogin AS can_login,
               rolreplication AS replication,
               rolbypassrls AS bypass_rls
        FROM pg_catalog.pg_roles
        WHERE rolname NOT LIKE 'pg\\_%' ESCAPE '\\'
        ORDER BY rolname
        """,
    )
    role_rows = [row for row in role_rows if row.get("role") in governed_roles]

    membership_rows = _mapping_rows(
        connection,
        """
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
        WHERE member.rolname = 'migration_owner'
        ORDER BY granted.rolname, member.rolname, grantor.rolname,
                 membership.set_option, membership.inherit_option,
                 membership.admin_option
        """,
    )

    setting_rows = _mapping_rows(
        connection,
        """
        SELECT role_data.rolname::text AS role,
               database_data.datname::text AS database,
               role_setting.setting::text AS setting
        FROM pg_catalog.pg_db_role_setting AS role_config
        JOIN pg_catalog.pg_roles AS role_data
          ON role_data.oid = role_config.setrole
        LEFT JOIN pg_catalog.pg_database AS database_data
          ON database_data.oid = NULLIF(role_config.setdatabase, 0)
        CROSS JOIN LATERAL unnest(role_config.setconfig) AS role_setting(setting)
        ORDER BY role_data.rolname, database_data.datname NULLS FIRST,
                 role_setting.setting
        """,
    )

    settings_by_role: dict[str, dict[str, str]] = {
        role: {} for role in managed_roles
    }
    database_setting_overrides: list[Mapping[str, str]] = []
    duplicate_global_settings: list[Mapping[str, str]] = []

    for row in setting_rows:
        role = row.get("role")
        if role not in managed_roles:
            continue

        key, value = _setting_pair(row.get("setting"))
        database = row.get("database")
        if isinstance(database, str) and database:
            database_setting_overrides.append(
                {
                    "role": role,
                    "database": database,
                    "setting": f"{key}={value}",
                }
            )
            continue

        role_settings = settings_by_role[role]
        if key in role_settings:
            duplicate_global_settings.append(
                {"role": role, "setting": key}
            )
            continue
        role_settings[key] = value

    return ExternalRoleCatalog(
        current_user=str(identity["current_user"]),
        session_user=str(identity["session_user"]),
        snapshot={
            "roles": role_rows,
            "role_settings": settings_by_role,
            "memberships": membership_rows,
            # Object ownership is a post-migration contract. At preflight the
            # target database may be empty, so ownership completeness is disabled.
            "objects": [],
        },
        database_setting_overrides=tuple(database_setting_overrides),
        duplicate_global_settings=tuple(duplicate_global_settings),
    )


def evaluate_cluster_role_catalog(
    catalog: ExternalRoleCatalog,
    bundle: ContractBundle | None = None,
) -> tuple[ContractViolation, ...]:
    """Evaluate governed cluster roles without imposing an Alembic login identity."""

    contract = bundle or load_contract_bundle()
    violations = list(
        validate_catalog_snapshot(
            catalog.snapshot,
            contract,
            require_complete_ownership=False,
        )
    )

    retired = _retired_role_names(contract)
    for row in catalog.snapshot.get("roles", []):
        if isinstance(row, Mapping) and row.get("role") in retired:
            role = str(row["role"])
            violations.append(
                ContractViolation(
                    code="role.retired_present",
                    subject=role,
                    message="Retired PostgreSQL role must be absent from the governed cluster.",
                )
            )

    for row in catalog.database_setting_overrides:
        violations.append(
            ContractViolation(
                code="preflight.database_role_setting",
                subject=f"{row['role']}@{row['database']}",
                message=(
                    "Database-specific managed-role settings are forbidden; "
                    f"found {row['setting']!r}."
                ),
            )
        )

    for row in catalog.duplicate_global_settings:
        violations.append(
            ContractViolation(
                code="preflight.duplicate_role_setting",
                subject=f"{row['role']}.{row['setting']}",
                message="Managed role setting appears more than once.",
            )
        )

    return tuple(sorted(violations))


def evaluate_external_role_catalog(
    catalog: ExternalRoleCatalog,
    bundle: ContractBundle | None = None,
) -> tuple[ContractViolation, ...]:
    """Evaluate the governed contract plus Alembic's exact reduced login identity."""

    contract = bundle or load_contract_bundle()
    violations = list(evaluate_cluster_role_catalog(catalog, contract))

    if catalog.current_user != "migration_owner":
        violations.append(
            ContractViolation(
                code="preflight.current_user",
                subject="current_user",
                message=(
                    "Alembic HEAD must execute as migration_owner; "
                    f"found {catalog.current_user!r}."
                ),
            )
        )

    if catalog.session_user != "migration_owner":
        violations.append(
            ContractViolation(
                code="preflight.session_user",
                subject="session_user",
                message=(
                    "Alembic HEAD session_user must be migration_owner; "
                    f"found {catalog.session_user!r}."
                ),
            )
        )

    return tuple(sorted(violations))


def assert_external_role_preflight(
    connection: Connection,
    bundle: ContractBundle | None = None,
) -> None:
    """Fail closed before Alembic HEAD if the external role contract drifted.

    SQLAlchemy 2.x starts a transaction automatically on the first catalog
    SELECT. Alembic must receive a pristine connection so revisions that use
    ``autocommit_block()`` can establish their own transaction lifecycle. The
    preflight therefore rejects a caller-owned transaction and always rolls
    back its own read-only autobegin transaction before returning or raising.
    """

    if connection.in_transaction():
        raise RuntimeError(
            "External PostgreSQL role preflight requires a pristine connection; "
            "refusing to inspect inside a caller-owned transaction."
        )

    contract = bundle or load_contract_bundle()
    violations: tuple[ContractViolation, ...] = ()
    try:
        catalog = capture_external_role_catalog(connection, contract)
        violations = evaluate_external_role_catalog(catalog, contract)
    finally:
        if connection.in_transaction():
            connection.rollback()

    if not violations:
        return

    details = "\n".join(
        f" - [{violation.code}] {violation.subject}: {violation.message}"
        for violation in violations
    )
    raise RuntimeError(
        "External PostgreSQL role preflight failed before Alembic HEAD. "
        "Cluster/bootstrap state must be repaired outside Alembic.\n"
        f"{details}"
    )
