#!/usr/bin/env python3
"""Apply P4D-2 Finance configuration through bounded app_secure functions.

This command is intentionally not a general SQL runner. It authenticates with the
Finance configuration control-plane connection URL and calls only the approved
SECURITY DEFINER provisioning functions.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import psycopg
from sqlalchemy.engine import make_url

_URL_ENV = "FINANCE_CONFIG_DATABASE_URL"
_EXPECTED_LOGIN = "finance_config_deployment"
_CAPABILITY_ROLE = "finance_config_runtime"
_FORBIDDEN_LOGINS = {"postgres", "migration_owner", "app_security_owner", "app_test_runtime", "worker_test_runtime", "auth_test_runtime", "lifecycle_maintenance_test_runtime"}
_FORBIDDEN_CAPABILITIES = {"app_runtime", "app_user", "auth_runtime", "worker_runtime", "lifecycle_maintenance_runtime", "migration_owner", "app_security_owner"}
_PROTECTED_TABLES = ("finance.branch_accounting_profiles", "finance.membership_plan_tax_profiles", "finance.tax_codes")
_PROTECTED_TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


def _split_qualified_table_name(relation: str) -> tuple[str, str]:
    schema_name, separator, table_name = relation.partition(".")
    if not separator or not schema_name or not table_name or "." in table_name:
        raise RuntimeError("Protected table inventory must use exact schema.table names")
    return schema_name, table_name


def _protected_table_oids(cur) -> dict[str, int]:
    table_names = {relation: _split_qualified_table_name(relation) for relation in _PROTECTED_TABLES}
    cur.execute(
        """
        SELECT n.nspname::text, c.relname::text, c.oid
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE (n.nspname, c.relname) IN (
            SELECT wanted.schema_name, wanted.table_name
            FROM jsonb_to_recordset(%s::jsonb) AS wanted(schema_name text, table_name text)
        )
        """,
        (json.dumps([{"schema_name": schema, "table_name": table} for schema, table in table_names.values()]),),
    )
    resolved = {f"{schema}.{table}": oid for schema, table, oid in cur.fetchall()}
    if set(resolved) != set(_PROTECTED_TABLES):
        raise RuntimeError("Protected table inventory mismatch")
    return resolved
_CONFIG_FUNCTIONS = (
    "app_secure.establish_finance_tax_code(uuid,text,text,text,text,integer)",
    "app_secure.establish_branch_accounting_profile(uuid,uuid,uuid,uuid,uuid,uuid,date,date)",
    "app_secure.establish_membership_plan_tax_profile(uuid,uuid,uuid,text,date,date)",
)
_FORBIDDEN_FUNCTIONS = (
    "app_secure.record_refund_obligation_binding(uuid,uuid)",
    "app_secure.resolve_branch_refund_required(uuid,uuid)",
    "app_secure.upsert_member_billing_party(uuid,text,text)",
    "app_secure.resolve_member_subscription_checkout_inputs(uuid)",
    "app_secure.record_member_subscription_checkout_binding(uuid,uuid,uuid)",
    "app_secure.attach_member_subscription_checkout_provider_order(uuid,uuid,text)",
)
_REQUIRED_TOP_LEVEL_KEYS = {
    "tax_codes",
    "branch_accounting_profiles",
    "membership_plan_tax_profiles",
}
_TAX_CODE_KEYS = {
    "tax_code_id",
    "code",
    "description",
    "hsn_sac",
    "tax_type",
    "gst_rate_basis_points",
}
_BRANCH_PROFILE_KEYS = {
    "profile_id",
    "branch_id",
    "legal_entity_id",
    "gst_registration_id",
    "division_id",
    "brand_id",
    "effective_from",
    "effective_until",
}
_PLAN_PROFILE_KEYS = {
    "profile_id",
    "membership_plan_id",
    "tax_code_id",
    "pricing_mode",
    "effective_from",
    "effective_until",
}


def _load_manifest(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != _REQUIRED_TOP_LEVEL_KEYS:
        raise ValueError(f"manifest must contain exactly {sorted(_REQUIRED_TOP_LEVEL_KEYS)}")
    for key in _REQUIRED_TOP_LEVEL_KEYS:
        if not isinstance(data[key], list):
            raise ValueError(f"manifest field {key} must be a list")
    return data


def _require_exact_keys(record: object, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(record, dict) or set(record) != keys:
        raise ValueError(f"{label} record must contain exactly {sorted(keys)}")
    return record


def _uuid(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a UUID string")
    return str(uuid.UUID(value))


def _text(value: object, label: str, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def _date(value: object, label: str, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    text = _text(value, label)
    assert text is not None
    parts = text.split("-")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        raise ValueError(f"{label} must be an ISO date")
    return text


def _int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    return value


def _connect():
    raw = os.environ.get(_URL_ENV)
    if not raw:
        raise RuntimeError(f"{_URL_ENV} is required")
    try:
        parsed = make_url(raw)
    except Exception as exc:
        raise RuntimeError(f"{_URL_ENV} is not a valid PostgreSQL URL") from exc
    if not parsed.host or not parsed.database:
        raise RuntimeError(f"{_URL_ENV} must include host and database")
    if parsed.username != _EXPECTED_LOGIN:
        raise RuntimeError("Finance configuration URL must authenticate as the dedicated Finance configuration login")
    return psycopg.connect(raw)


def _validate_connection_identity(cur) -> None:
    cur.execute(
        """
        SELECT session_user::text, current_user::text, current_role::text,
               r.rolcanlogin, r.rolsuper, r.rolbypassrls, r.rolcreatedb, r.rolcreaterole
        FROM pg_catalog.pg_roles r
        WHERE r.rolname = session_user
        """
    )
    identity = cur.fetchone()
    if identity is None:
        raise RuntimeError("Finance configuration authenticated login is not visible in pg_catalog")
    session_user, current_user, current_role, can_login, superuser, bypass_rls, create_db, create_role = identity
    if (session_user, current_user, current_role) != (_EXPECTED_LOGIN, _EXPECTED_LOGIN, _EXPECTED_LOGIN):
        raise RuntimeError("Finance configuration connection identity mismatch")
    if session_user in _FORBIDDEN_LOGINS:
        raise RuntimeError("Forbidden principal supplied for Finance configuration")
    if not can_login or superuser or bypass_rls or create_db or create_role:
        raise RuntimeError("Finance configuration login has unsafe role attributes")

    cur.execute(
        """
        SELECT granted.rolname, memberships.admin_option, memberships.inherit_option, memberships.set_option
        FROM pg_catalog.pg_auth_members memberships
        JOIN pg_catalog.pg_roles granted ON granted.oid = memberships.roleid
        JOIN pg_catalog.pg_roles member ON member.oid = memberships.member
        WHERE member.rolname = %s
        ORDER BY granted.rolname
        """,
        (_EXPECTED_LOGIN,),
    )
    login_memberships = cur.fetchall()
    if login_memberships != [(_CAPABILITY_ROLE, False, True, False)]:
        raise RuntimeError("Finance configuration login membership closure mismatch")

    cur.execute(
        """
        SELECT granted.rolname
        FROM pg_catalog.pg_auth_members memberships
        JOIN pg_catalog.pg_roles granted ON granted.oid = memberships.roleid
        JOIN pg_catalog.pg_roles member ON member.oid = memberships.member
        WHERE member.rolname = %s
        ORDER BY granted.rolname
        """,
        (_CAPABILITY_ROLE,),
    )
    if cur.fetchall() != []:
        raise RuntimeError("finance_config_runtime must not inherit any other capability")

    cur.execute("SELECT pg_catalog.pg_has_role(%s, %s, 'MEMBER')", (_EXPECTED_LOGIN, _CAPABILITY_ROLE))
    if cur.fetchone()[0] is not True:
        raise RuntimeError("Finance configuration login lacks finance_config_runtime membership")
    for role in sorted(_FORBIDDEN_CAPABILITIES):
        cur.execute("SELECT pg_catalog.pg_has_role(%s, %s, 'MEMBER')", (_EXPECTED_LOGIN, role))
        if cur.fetchone()[0]:
            raise RuntimeError("Finance configuration login has forbidden capability membership")

    protected_table_oids = _protected_table_oids(cur)
    for role in (_EXPECTED_LOGIN, _CAPABILITY_ROLE):
        cur.execute("SELECT pg_catalog.has_schema_privilege(%s, 'finance', 'CREATE')", (role,))
        if cur.fetchone()[0]:
            raise RuntimeError("Finance configuration identity has Finance schema CREATE")
        for table in _PROTECTED_TABLES:
            table_oid = protected_table_oids[table]
            for privilege in _PROTECTED_TABLE_PRIVILEGES:
                cur.execute("SELECT pg_catalog.has_table_privilege(%s, %s, %s)", (role, table_oid, privilege))
                if cur.fetchone()[0]:
                    raise RuntimeError("Finance configuration identity has direct protected-table privilege")

    for function_signature in _CONFIG_FUNCTIONS:
        cur.execute("SELECT pg_catalog.has_function_privilege(%s, %s, 'EXECUTE')", (_EXPECTED_LOGIN, function_signature))
        if cur.fetchone()[0] is not True:
            raise RuntimeError("Finance configuration login lacks approved config function EXECUTE")
        cur.execute("SELECT pg_catalog.has_function_privilege(%s, %s, 'EXECUTE')", (_CAPABILITY_ROLE, function_signature))
        if cur.fetchone()[0] is not True:
            raise RuntimeError("finance_config_runtime lacks approved config function EXECUTE")
    for function_signature in _FORBIDDEN_FUNCTIONS:
        cur.execute("SELECT pg_catalog.has_function_privilege(%s, %s, 'EXECUTE')", (_EXPECTED_LOGIN, function_signature))
        if cur.fetchone()[0]:
            raise RuntimeError("Finance configuration login can execute unrelated app_secure function")
        cur.execute("SELECT pg_catalog.has_function_privilege(%s, %s, 'EXECUTE')", (_CAPABILITY_ROLE, function_signature))
        if cur.fetchone()[0]:
            raise RuntimeError("finance_config_runtime can execute unrelated app_secure function")


def apply_manifest(manifest: dict[str, Any]) -> list[tuple[str, str, bool, bool]]:
    results: list[tuple[str, str, bool, bool]] = []
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SHOW server_encoding")
            if cur.fetchone()[0] != "UTF8":
                raise RuntimeError("database server_encoding must be UTF8")
            cur.execute("SHOW client_encoding")
            if cur.fetchone()[0] != "UTF8":
                raise RuntimeError("database client_encoding must be UTF8")
            _validate_connection_identity(cur)

            for raw in manifest["tax_codes"]:
                row = _require_exact_keys(raw, _TAX_CODE_KEYS, "tax_codes")
                cur.execute(
                    "SELECT tax_code_id, code, inserted, replayed FROM app_secure.establish_finance_tax_code(%s,%s,%s,%s,%s,%s)",
                    (
                        _uuid(row["tax_code_id"], "tax_code_id"),
                        _text(row["code"], "code"),
                        _text(row["description"], "description"),
                        _text(row["hsn_sac"], "hsn_sac", allow_none=True),
                        _text(row["tax_type"], "tax_type"),
                        _int(row["gst_rate_basis_points"], "gst_rate_basis_points"),
                    ),
                )
                tax_code_id, code, inserted, replayed = cur.fetchone()
                results.append(("tax_code", f"{tax_code_id}:{code}", inserted, replayed))

            for raw in manifest["branch_accounting_profiles"]:
                row = _require_exact_keys(raw, _BRANCH_PROFILE_KEYS, "branch_accounting_profiles")
                cur.execute(
                    "SELECT profile_id, branch_id, inserted, replayed FROM app_secure.establish_branch_accounting_profile(%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        _uuid(row["profile_id"], "profile_id"),
                        _uuid(row["branch_id"], "branch_id"),
                        _uuid(row["legal_entity_id"], "legal_entity_id"),
                        _uuid(row["gst_registration_id"], "gst_registration_id"),
                        _uuid(row["division_id"], "division_id"),
                        _uuid(row["brand_id"], "brand_id"),
                        _date(row["effective_from"], "effective_from"),
                        _date(row["effective_until"], "effective_until", allow_none=True),
                    ),
                )
                profile_id, branch_id, inserted, replayed = cur.fetchone()
                results.append(("branch_accounting_profile", f"{profile_id}:{branch_id}", inserted, replayed))

            for raw in manifest["membership_plan_tax_profiles"]:
                row = _require_exact_keys(raw, _PLAN_PROFILE_KEYS, "membership_plan_tax_profiles")
                cur.execute(
                    "SELECT profile_id, membership_plan_id, inserted, replayed FROM app_secure.establish_membership_plan_tax_profile(%s,%s,%s,%s,%s,%s)",
                    (
                        _uuid(row["profile_id"], "profile_id"),
                        _uuid(row["membership_plan_id"], "membership_plan_id"),
                        _uuid(row["tax_code_id"], "tax_code_id"),
                        _text(row["pricing_mode"], "pricing_mode"),
                        _date(row["effective_from"], "effective_from"),
                        _date(row["effective_until"], "effective_until", allow_none=True),
                    ),
                )
                profile_id, membership_plan_id, inserted, replayed = cur.fetchone()
                results.append(("membership_plan_tax_profile", f"{profile_id}:{membership_plan_id}", inserted, replayed))
        conn.commit()
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="strict JSON Finance configuration manifest")
    args = parser.parse_args(argv)
    manifest = _load_manifest(args.manifest)
    for kind, identity, inserted, replayed in apply_manifest(manifest):
        print(f"{kind} {identity} inserted={inserted} replayed={replayed}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"p4d2 finance configuration failed: {exc.__class__.__name__}", file=sys.stderr)
        raise SystemExit(1)
