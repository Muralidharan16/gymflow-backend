from __future__ import annotations

import argparse
import os
from contextlib import contextmanager

import psycopg


REGISTRATION = "public.organization_registrations"
PAYLOAD = "public.organization_registration_payloads_secure"
SECURITY_OWNER = "app_security_owner"
DIRECT_RUNTIME_ROLES = ("app_runtime", "app_user", "auth_runtime")
FINAL_SELECT = {
    "id",
    "org_id",
    "id_type",
    "id_number_masked",
    "country_code",
    "is_verified",
    "verified_at",
}
PREDECESSOR_SELECT = FINAL_SELECT | {"id_number_encrypted", "crypto_version"}
PAYLOAD_SELECT = {"registration_id", "tenant_id"}
BACKFILL_FUNCTIONS = (
    ("current_legacy_registration_backfill_rows", 0, "s"),
    ("convert_legacy_organization_registration_envelope", 3, "v"),
)


@contextmanager
def _connection():
    dsn = os.environ.get("P3B_MIGRATION_DSN", "").strip()
    if not dsn:
        raise RuntimeError("P3B_MIGRATION_DSN is required")
    with psycopg.connect(dsn) as connection:
        yield connection


def _column_acl(cursor, relation: str, role_name: str, privilege: str) -> set[str]:
    cursor.execute(
        """
        SELECT attribute_data.attname::text
        FROM pg_catalog.pg_attribute AS attribute_data
        CROSS JOIN LATERAL pg_catalog.aclexplode(attribute_data.attacl) AS acl_data
        JOIN pg_catalog.pg_roles AS grantee_role
          ON grantee_role.oid = acl_data.grantee
        WHERE attribute_data.attrelid = pg_catalog.to_regclass(%s)
          AND attribute_data.attnum > 0
          AND NOT attribute_data.attisdropped
          AND grantee_role.rolname = %s
          AND acl_data.privilege_type = %s
        ORDER BY attribute_data.attname
        """,
        (relation, role_name, privilege),
    )
    return {str(row[0]) for row in cursor.fetchall()}


def _table_acl(cursor, relation: str, role_name: str) -> set[str]:
    cursor.execute(
        """
        SELECT acl_data.privilege_type::text
        FROM pg_catalog.pg_class AS relation_data
        CROSS JOIN LATERAL pg_catalog.aclexplode(relation_data.relacl) AS acl_data
        JOIN pg_catalog.pg_roles AS grantee_role
          ON grantee_role.oid = acl_data.grantee
        WHERE relation_data.oid = pg_catalog.to_regclass(%s)
          AND grantee_role.rolname = %s
        ORDER BY acl_data.privilege_type
        """,
        (relation, role_name),
    )
    return {str(row[0]) for row in cursor.fetchall()}


def _relation_state(cursor, relation: str) -> tuple[str, bool, bool]:
    cursor.execute(
        """
        SELECT pg_catalog.pg_get_userbyid(relation_data.relowner)::text,
               relation_data.relrowsecurity,
               relation_data.relforcerowsecurity
        FROM pg_catalog.pg_class AS relation_data
        WHERE relation_data.oid = pg_catalog.to_regclass(%s)
        """,
        (relation,),
    )
    row = cursor.fetchone()
    assert row is not None
    return str(row[0]), bool(row[1]), bool(row[2])


def _crypto_default(cursor) -> str | None:
    cursor.execute(
        """
        SELECT pg_catalog.pg_get_expr(default_data.adbin, default_data.adrelid)::text
        FROM pg_catalog.pg_attribute AS attribute_data
        LEFT JOIN pg_catalog.pg_attrdef AS default_data
          ON default_data.adrelid = attribute_data.attrelid
         AND default_data.adnum = attribute_data.attnum
        WHERE attribute_data.attrelid = pg_catalog.to_regclass(%s)
          AND attribute_data.attname = 'crypto_version'
          AND attribute_data.attnum > 0
          AND NOT attribute_data.attisdropped
        """,
        (REGISTRATION,),
    )
    row = cursor.fetchone()
    assert row is not None
    value = row[0]
    if value is None:
        return None
    return str(value).replace("::smallint", "").strip("() ")


def _constraint(cursor, name: str):
    cursor.execute(
        """
        SELECT constraint_data.contype::text,
               constraint_data.convalidated,
               constraint_data.condeferrable,
               constraint_data.condeferred,
               pg_catalog.pg_get_constraintdef(constraint_data.oid, true)::text
        FROM pg_catalog.pg_constraint AS constraint_data
        WHERE constraint_data.conrelid = pg_catalog.to_regclass(%s)
          AND constraint_data.conname = %s
        """,
        (REGISTRATION, name),
    )
    return cursor.fetchone()


def _function(cursor, name: str, nargs: int):
    cursor.execute(
        """
        SELECT owner_role.rolname::text,
               procedure_data.prosecdef,
               procedure_data.provolatile::text,
               procedure_data.proconfig,
               EXISTS (
                   SELECT 1
                   FROM pg_catalog.aclexplode(
                       COALESCE(
                           procedure_data.proacl,
                           pg_catalog.acldefault('f', procedure_data.proowner)
                       )
                   ) AS acl_data
                   JOIN pg_catalog.pg_roles AS grantee_role
                     ON grantee_role.oid = acl_data.grantee
                   WHERE grantee_role.rolname = 'app_runtime'
                     AND acl_data.privilege_type = 'EXECUTE'
               ),
               EXISTS (
                   SELECT 1
                   FROM pg_catalog.aclexplode(
                       COALESCE(
                           procedure_data.proacl,
                           pg_catalog.acldefault('f', procedure_data.proowner)
                       )
                   ) AS acl_data
                   WHERE acl_data.grantee = 0
                     AND acl_data.privilege_type = 'EXECUTE'
               )
        FROM pg_catalog.pg_proc AS procedure_data
        JOIN pg_catalog.pg_namespace AS namespace_data
          ON namespace_data.oid = procedure_data.pronamespace
        JOIN pg_catalog.pg_roles AS owner_role
          ON owner_role.oid = procedure_data.proowner
        WHERE namespace_data.nspname = 'app_secure'
          AND procedure_data.proname = %s
          AND procedure_data.pronargs = %s
          AND procedure_data.prokind = 'f'
        """,
        (name, nargs),
    )
    return cursor.fetchone()


def _assert_shared_boundary(cursor) -> None:
    for relation in (REGISTRATION, PAYLOAD):
        assert _relation_state(cursor, relation) == ("migration_owner", True, True)

    for role_name in DIRECT_RUNTIME_ROLES:
        assert _table_acl(cursor, REGISTRATION, role_name) == set()
        assert _table_acl(cursor, PAYLOAD, role_name) == set()
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert _column_acl(cursor, REGISTRATION, role_name, privilege) == set()
            assert _column_acl(cursor, PAYLOAD, role_name, privilege) == set()

    assert _table_acl(cursor, REGISTRATION, SECURITY_OWNER) == set()
    assert _table_acl(cursor, PAYLOAD, SECURITY_OWNER) == set()
    assert _column_acl(cursor, PAYLOAD, SECURITY_OWNER, "SELECT") == PAYLOAD_SELECT


def _assert_final(cursor) -> None:
    _assert_shared_boundary(cursor)
    assert _crypto_default(cursor) == "1"
    assert _column_acl(cursor, REGISTRATION, SECURITY_OWNER, "SELECT") == FINAL_SELECT

    check_row = _constraint(cursor, "ck_org_reg_envelope_only")
    assert check_row is not None
    assert check_row[0] == "c"
    assert check_row[1] is True
    check_def = " ".join(str(check_row[4]).lower().split())
    assert "crypto_version = 1" in check_def
    assert "id_number_encrypted is null" in check_def

    fk_row = _constraint(cursor, "fk_org_reg_required_envelope")
    assert fk_row is not None
    assert fk_row[0] == "f"
    assert fk_row[1] is True
    assert fk_row[2] is True
    assert fk_row[3] is True
    fk_def = " ".join(str(fk_row[4]).lower().split())
    assert "foreign key (id)" in fk_def
    assert "organization_registration_payloads_secure(registration_id)" in fk_def
    assert "deferrable initially deferred" in fk_def

    for name, nargs, _ in BACKFILL_FUNCTIONS:
        assert _function(cursor, name, nargs) is None


def _assert_predecessor(cursor) -> None:
    _assert_shared_boundary(cursor)
    assert _crypto_default(cursor) == "0"
    assert _column_acl(cursor, REGISTRATION, SECURITY_OWNER, "SELECT") == PREDECESSOR_SELECT
    assert _constraint(cursor, "ck_org_reg_envelope_only") is None
    assert _constraint(cursor, "fk_org_reg_required_envelope") is None

    for name, nargs, volatility in BACKFILL_FUNCTIONS:
        row = _function(cursor, name, nargs)
        assert row is not None
        assert row[0] == SECURITY_OWNER
        assert row[1] is True
        assert row[2] == volatility
        assert set(row[3] or ()) == {"search_path=pg_catalog", "row_security=on"}
        assert row[4] is True
        assert row[5] is False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expect", choices=("final", "predecessor"), required=True)
    args = parser.parse_args()

    with _connection() as connection:
        with connection.cursor() as cursor:
            if args.expect == "final":
                _assert_final(cursor)
            else:
                _assert_predecessor(cursor)

    print(f"P3B registration contract boundary ({args.expect}): PASS")


if __name__ == "__main__":
    main()
