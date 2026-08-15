from __future__ import annotations

import json
import os

import psycopg


RELATION = "public.organization_registrations"
ROLES = ("app_runtime", "app_user", "auth_runtime", "app_security_owner")
TABLE_PRIVILEGES = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
)
COLUMN_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "REFERENCES")


def main() -> None:
    dsn = os.environ.get("P3B_MIGRATION_DSN", "").strip()
    if not dsn:
        raise RuntimeError("P3B_MIGRATION_DSN is required")

    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    CASE WHEN acl_data.grantee = 0 THEN 'PUBLIC'
                         ELSE grantee_role.rolname::text END AS grantee,
                    grantor_role.rolname::text AS grantor,
                    acl_data.privilege_type::text,
                    acl_data.is_grantable
                FROM pg_catalog.pg_class AS relation_data
                CROSS JOIN LATERAL pg_catalog.aclexplode(
                    COALESCE(
                        relation_data.relacl,
                        pg_catalog.acldefault('r', relation_data.relowner)
                    )
                ) AS acl_data
                LEFT JOIN pg_catalog.pg_roles AS grantee_role
                  ON grantee_role.oid = acl_data.grantee
                JOIN pg_catalog.pg_roles AS grantor_role
                  ON grantor_role.oid = acl_data.grantor
                WHERE relation_data.oid = pg_catalog.to_regclass(%s)
                ORDER BY grantee, privilege_type, grantor
                """,
                (RELATION,),
            )
            direct_table_acl = [
                {
                    "grantee": row[0],
                    "grantor": row[1],
                    "privilege": row[2],
                    "grantable": bool(row[3]),
                }
                for row in cursor.fetchall()
                if row[0] in ROLES or row[0] == "PUBLIC"
            ]

            effective_table_acl: dict[str, list[str]] = {}
            for role in ROLES:
                allowed = []
                for privilege in TABLE_PRIVILEGES:
                    cursor.execute(
                        "SELECT pg_catalog.has_table_privilege(%s, %s, %s)",
                        (role, RELATION, privilege),
                    )
                    if cursor.fetchone()[0]:
                        allowed.append(privilege)
                effective_table_acl[role] = allowed

            cursor.execute(
                """
                SELECT attribute_data.attname::text,
                       CASE WHEN acl_data.grantee = 0 THEN 'PUBLIC'
                            ELSE grantee_role.rolname::text END AS grantee,
                       grantor_role.rolname::text AS grantor,
                       acl_data.privilege_type::text,
                       acl_data.is_grantable
                FROM pg_catalog.pg_attribute AS attribute_data
                CROSS JOIN LATERAL pg_catalog.aclexplode(attribute_data.attacl) AS acl_data
                LEFT JOIN pg_catalog.pg_roles AS grantee_role
                  ON grantee_role.oid = acl_data.grantee
                JOIN pg_catalog.pg_roles AS grantor_role
                  ON grantor_role.oid = acl_data.grantor
                WHERE attribute_data.attrelid = pg_catalog.to_regclass(%s)
                  AND attribute_data.attnum > 0
                  AND NOT attribute_data.attisdropped
                ORDER BY attribute_data.attname, grantee, privilege_type, grantor
                """,
                (RELATION,),
            )
            direct_column_acl = [
                {
                    "column": row[0],
                    "grantee": row[1],
                    "grantor": row[2],
                    "privilege": row[3],
                    "grantable": bool(row[4]),
                }
                for row in cursor.fetchall()
                if row[1] in ROLES or row[1] == "PUBLIC"
            ]

            cursor.execute(
                """
                SELECT attribute_data.attname::text
                FROM pg_catalog.pg_attribute AS attribute_data
                WHERE attribute_data.attrelid = pg_catalog.to_regclass(%s)
                  AND attribute_data.attnum > 0
                  AND NOT attribute_data.attisdropped
                ORDER BY attribute_data.attnum
                """,
                (RELATION,),
            )
            columns = [str(row[0]) for row in cursor.fetchall()]

            effective_column_acl: dict[str, dict[str, list[str]]] = {}
            for role in ROLES:
                role_columns: dict[str, list[str]] = {}
                for column in columns:
                    allowed = []
                    for privilege in COLUMN_PRIVILEGES:
                        cursor.execute(
                            "SELECT pg_catalog.has_column_privilege(%s, %s, %s, %s)",
                            (role, RELATION, column, privilege),
                        )
                        if cursor.fetchone()[0]:
                            allowed.append(privilege)
                    if allowed:
                        role_columns[column] = allowed
                effective_column_acl[role] = role_columns

            role_list = list(ROLES)
            cursor.execute(
                """
                SELECT member_role.rolname::text AS member,
                       granted_role.rolname::text AS granted,
                       membership.inherit_option,
                       membership.set_option,
                       membership.admin_option
                FROM pg_catalog.pg_auth_members AS membership
                JOIN pg_catalog.pg_roles AS member_role
                  ON member_role.oid = membership.member
                JOIN pg_catalog.pg_roles AS granted_role
                  ON granted_role.oid = membership.roleid
                WHERE member_role.rolname = ANY(%s::text[])
                   OR granted_role.rolname = ANY(%s::text[])
                ORDER BY member, granted
                """,
                (role_list, role_list),
            )
            memberships = [
                {
                    "member": row[0],
                    "granted": row[1],
                    "inherit": bool(row[2]),
                    "set": bool(row[3]),
                    "admin": bool(row[4]),
                }
                for row in cursor.fetchall()
            ]

    evidence = {
        "relation": RELATION,
        "direct_table_acl": direct_table_acl,
        "effective_table_acl": effective_table_acl,
        "direct_column_acl": direct_column_acl,
        "effective_column_acl": effective_column_acl,
        "memberships": memberships,
    }
    print("P3B_REGISTRATION_ACL_INVENTORY=" + json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    main()
