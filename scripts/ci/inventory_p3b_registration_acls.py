from __future__ import annotations

import json
import os

import psycopg


RELATION = "public.organization_registrations"
ROLES = ("app_runtime", "app_user", "auth_runtime", "app_security_owner")
PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


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
            direct = [
                {
                    "grantee": row[0],
                    "grantor": row[1],
                    "privilege": row[2],
                    "grantable": bool(row[3]),
                }
                for row in cursor.fetchall()
                if row[0] in ROLES or row[0] == "PUBLIC"
            ]

            effective: dict[str, list[str]] = {}
            for role in ROLES:
                allowed = []
                for privilege in PRIVILEGES:
                    cursor.execute(
                        "SELECT pg_catalog.has_table_privilege(%s, %s, %s)",
                        (role, RELATION, privilege),
                    )
                    if cursor.fetchone()[0]:
                        allowed.append(privilege)
                effective[role] = allowed

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
                WHERE member_role.rolname IN %s
                   OR granted_role.rolname IN %s
                ORDER BY member, granted
                """,
                (ROLES, ROLES),
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
        "direct_acl": direct,
        "effective": effective,
        "memberships": memberships,
    }
    print("P3B_REGISTRATION_ACL_INVENTORY=" + json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    main()
