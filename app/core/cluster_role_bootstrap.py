"""Deterministic fresh-cluster PostgreSQL role bootstrap rendered from manifests.

This module is intentionally pure: it renders SQL but never opens a database
connection. The rendered bootstrap is create-only. It refuses to run if any
managed or explicitly retired role already exists, so it cannot become an
implicit production drift-repair mechanism.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from app.core.cluster_role_contract import ContractBundle, load_contract_bundle


_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
_ALLOWED_SETTINGS = frozenset(
    {
        "statement_timeout",
        "lock_timeout",
        "idle_in_transaction_session_timeout",
        "row_security",
    }
)
_ATTRIBUTE_SQL = (
    ("superuser", "SUPERUSER", "NOSUPERUSER"),
    ("create_db", "CREATEDB", "NOCREATEDB"),
    ("create_role", "CREATEROLE", "NOCREATEROLE"),
    ("inherit", "INHERIT", "NOINHERIT"),
    ("can_login", "LOGIN", "NOLOGIN"),
    ("replication", "REPLICATION", "NOREPLICATION"),
    ("bypass_rls", "BYPASSRLS", "NOBYPASSRLS"),
)


class BootstrapContractError(ValueError):
    """Raised when a manifest cannot safely drive cluster bootstrap SQL."""


def _identifier(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise BootstrapContractError(f"unsafe PostgreSQL identifier for {field}: {value!r}")
    return value


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _boolean_option(value: object, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise BootstrapContractError(f"{field} must be boolean")
    return value


def _managed_roles(bundle: ContractBundle) -> Mapping[str, Any]:
    roles = bundle.roles.get("managed_roles")
    if not isinstance(roles, Mapping) or not roles:
        raise BootstrapContractError("roles.managed_roles must be a non-empty object")
    return roles


def _retired_roles(bundle: ContractBundle) -> tuple[str, ...]:
    raw = bundle.roles.get("retired_roles", {})
    if not isinstance(raw, Mapping):
        raise BootstrapContractError("roles.retired_roles must be an object")

    retired: list[str] = []
    for key, record in raw.items():
        role = _identifier(key, field="retired role key")
        if not isinstance(record, Mapping) or record.get("role") != role:
            raise BootstrapContractError(f"retired role record is inconsistent for {role}")
        if record.get("expected_presence") is not False:
            raise BootstrapContractError(f"retired role {role} must declare expected_presence=false")
        retired.append(role)
    return tuple(sorted(retired))


def _render_role(role: str, record: Mapping[str, Any]) -> str:
    if record.get("role") != role:
        raise BootstrapContractError(f"managed role record is inconsistent for {role}")
    attributes = record.get("attributes")
    if not isinstance(attributes, Mapping):
        raise BootstrapContractError(f"managed role {role} attributes must be an object")

    expected_keys = {name for name, _, _ in _ATTRIBUTE_SQL}
    if set(attributes) != expected_keys:
        raise BootstrapContractError(
            f"managed role {role} attributes must be exactly {sorted(expected_keys)!r}"
        )

    clauses = []
    for attribute, enabled_sql, disabled_sql in _ATTRIBUTE_SQL:
        enabled = _boolean_option(attributes[attribute], field=f"{role}.{attribute}")
        clauses.append(enabled_sql if enabled else disabled_sql)
    return f"CREATE ROLE {role} " + " ".join(clauses) + ";"


def _render_settings(bundle: ContractBundle, managed: Mapping[str, Any]) -> list[str]:
    raw = bundle.role_settings.get("settings_by_role")
    if not isinstance(raw, Mapping) or set(raw) != set(managed):
        raise BootstrapContractError("role settings must cover the exact managed role set")

    statements: list[str] = []
    for role in sorted(managed):
        settings = raw[role]
        if not isinstance(settings, Mapping):
            raise BootstrapContractError(f"settings for {role} must be an object")
        for setting in sorted(settings):
            if setting not in _ALLOWED_SETTINGS:
                raise BootstrapContractError(f"unsupported managed role setting: {setting}")
            value = settings[setting]
            if not isinstance(value, str) or not value or "\x00" in value:
                raise BootstrapContractError(f"invalid value for {role}.{setting}")
            statements.append(f"ALTER ROLE {role} SET {setting} = {_literal(value)};")
    return statements


def _render_memberships(bundle: ContractBundle, managed: Mapping[str, Any]) -> list[str]:
    rows = bundle.memberships.get("exact_rows")
    if not isinstance(rows, list):
        raise BootstrapContractError("memberships.exact_rows must be a list")

    approved_grantor = bundle.grantors.get("approved_membership_grantor")
    approved_grantor = _identifier(approved_grantor, field="approved membership grantor")
    if bundle.grantors.get("approved_grantors") != [approved_grantor]:
        raise BootstrapContractError("bootstrap requires one exact approved membership grantor")

    statements: list[str] = []
    seen: set[tuple[str, str]] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise BootstrapContractError(f"membership row {index} must be an object")
        granted = _identifier(row.get("granted_role"), field=f"membership[{index}].granted_role")
        member = _identifier(row.get("member_role"), field=f"membership[{index}].member_role")
        if granted not in managed or member not in managed:
            raise BootstrapContractError("managed bootstrap membership references an unmanaged role")
        pair = (granted, member)
        if pair in seen:
            raise BootstrapContractError(f"duplicate membership contract pair: {granted}->{member}")
        seen.add(pair)
        if row.get("approved_grantor") != approved_grantor or row.get("exact_row_count") != 1:
            raise BootstrapContractError(f"membership {granted}->{member} grantor/cardinality is unsafe")
        admin = _boolean_option(row.get("admin_option"), field="membership.admin_option")
        inherit = _boolean_option(row.get("inherit_option"), field="membership.inherit_option")
        set_option = _boolean_option(row.get("set_option"), field="membership.set_option")
        statements.append(
            f"GRANT {granted} TO {member} WITH ADMIN {'TRUE' if admin else 'FALSE'}, "
            f"INHERIT {'TRUE' if inherit else 'FALSE'}, SET {'TRUE' if set_option else 'FALSE'};"
        )
    return statements


def render_fresh_cluster_bootstrap(bundle: ContractBundle | None = None) -> str:
    """Render a create-only PostgreSQL 16 bootstrap for the exact manifest contract."""

    contract = bundle or load_contract_bundle()
    managed = _managed_roles(contract)
    managed_names = tuple(sorted(_identifier(role, field="managed role") for role in managed))
    retired_names = _retired_roles(contract)

    approved_grantor = _identifier(
        contract.grantors.get("approved_membership_grantor"),
        field="approved membership grantor",
    )
    if approved_grantor not in contract.grantors.get("approved_grantors", []):
        raise BootstrapContractError("approved membership grantor is not approved")

    all_governed = managed_names + retired_names
    governed_array = ", ".join(_literal(role) for role in all_governed)

    lines = [
        "\\set ON_ERROR_STOP on",
        "BEGIN;",
        "DO $doers_cluster_bootstrap_guard$",
        "DECLARE",
        "  operator_record record;",
        "  existing_roles text;",
        "BEGIN",
        "  SELECT * INTO operator_record",
        "  FROM pg_catalog.pg_roles",
        "  WHERE rolname = current_user;",
        "",
        f"  IF current_user <> {_literal(approved_grantor)}",
        f"     OR session_user <> {_literal(approved_grantor)}",
        "     OR operator_record IS NULL",
        "     OR NOT operator_record.rolsuper THEN",
        "    RAISE EXCEPTION",
        f"      'fresh cluster bootstrap requires current_user=session_user={approved_grantor} with SUPERUSER';",
        "  END IF;",
        "",
        "  SELECT string_agg(rolname, ', ' ORDER BY rolname)",
        "  INTO existing_roles",
        "  FROM pg_catalog.pg_roles",
        f"  WHERE rolname = ANY (ARRAY[{governed_array}]::text[]);",
        "",
        "  IF existing_roles IS NOT NULL THEN",
        "    RAISE EXCEPTION",
        "      'fresh cluster bootstrap refuses existing managed/retired roles: %',",
        "      existing_roles;",
        "  END IF;",
        "END",
        "$doers_cluster_bootstrap_guard$;",
        "",
    ]

    for role in managed_names:
        record = managed[role]
        if not isinstance(record, Mapping):
            raise BootstrapContractError(f"managed role {role} record must be an object")
        lines.append(_render_role(role, record))
    lines.append("")
    lines.extend(_render_settings(contract, managed))
    lines.append("")
    lines.extend(_render_memberships(contract, managed))
    lines.extend(["", "COMMIT;", ""])

    return "\n".join(lines)
