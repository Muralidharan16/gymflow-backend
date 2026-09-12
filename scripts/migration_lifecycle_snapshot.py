from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import psycopg
from sqlalchemy.engine import make_url


SYSTEM_SCHEMAS = ("pg_catalog", "information_schema")


def _sync_dsn(raw: str) -> str:
    url = make_url(raw)
    if url.drivername.startswith("postgresql+"):
        url = url.set(drivername="postgresql")
    return url.render_as_string(hide_password=False)


def _connect() -> psycopg.Connection[Any]:
    raw = os.environ.get("DATABASE_URL")
    if not raw:
        raise RuntimeError("DATABASE_URL is required")
    return psycopg.connect(_sync_dsn(raw), autocommit=True)


def _rows(conn: psycopg.Connection[Any], sql: str) -> list[dict[str, Any]]:
    with conn.cursor() as cursor:
        cursor.execute(sql)
        columns = [column.name for column in cursor.description or ()]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _one(conn: psycopg.Connection[Any], sql: str) -> Any:
    with conn.cursor() as cursor:
        cursor.execute(sql)
        row = cursor.fetchone()
        if row is None:
            return None
        if len(row) == 1:
            return row[0]
        columns = [column.name for column in cursor.description or ()]
        return dict(zip(columns, row, strict=True))


def _database_snapshot(conn: psycopg.Connection[Any]) -> dict[str, Any]:
    schemas = _rows(
        conn,
        """
        SELECT n.nspname AS schema_name,
               pg_get_userbyid(n.nspowner) AS owner_name
        FROM pg_namespace AS n
        WHERE n.nspname NOT LIKE 'pg_%'
          AND n.nspname <> 'information_schema'
        ORDER BY n.nspname
        """,
    )

    schema_acls = _rows(
        conn,
        """
        SELECT n.nspname AS schema_name,
               COALESCE(grantee.rolname, 'PUBLIC') AS grantee_name,
               COALESCE(grantor.rolname, 'PUBLIC') AS grantor_name,
               acl.privilege_type,
               acl.is_grantable
        FROM pg_namespace AS n
        CROSS JOIN LATERAL aclexplode(
            COALESCE(n.nspacl, acldefault('n', n.nspowner))
        ) AS acl
        LEFT JOIN pg_roles AS grantor ON grantor.oid = acl.grantor
        LEFT JOIN pg_roles AS grantee ON grantee.oid = NULLIF(acl.grantee, 0)
        WHERE n.nspname NOT LIKE 'pg_%'
          AND n.nspname <> 'information_schema'
        ORDER BY n.nspname, grantee_name, grantor_name,
                 acl.privilege_type, acl.is_grantable
        """,
    )

    extensions = _rows(
        conn,
        """
        SELECT e.extname AS extension_name,
               e.extversion AS extension_version,
               n.nspname AS schema_name,
               pg_get_userbyid(e.extowner) AS owner_name,
               e.extrelocatable AS relocatable
        FROM pg_extension AS e
        JOIN pg_namespace AS n ON n.oid = e.extnamespace
        ORDER BY e.extname
        """,
    )

    relations = _rows(
        conn,
        """
        SELECT n.nspname AS schema_name,
               c.relname AS relation_name,
               c.relkind,
               c.relpersistence,
               pg_get_userbyid(c.relowner) AS owner_name,
               c.relrowsecurity AS row_security,
               c.relforcerowsecurity AS force_row_security,
               c.relispartition AS is_partition,
               CASE
                 WHEN c.relispartition THEN pg_get_expr(c.relpartbound, c.oid, true)
                 ELSE NULL
               END AS partition_bound,
               COALESCE(
                 (
                   SELECT string_agg(
                     pn.nspname || '.' || pc.relname,
                     ',' ORDER BY pn.nspname, pc.relname
                   )
                   FROM pg_inherits AS i
                   JOIN pg_class AS pc ON pc.oid = i.inhparent
                   JOIN pg_namespace AS pn ON pn.oid = pc.relnamespace
                   WHERE i.inhrelid = c.oid
                 ),
                 ''
               ) AS parents
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname NOT LIKE 'pg_%'
          AND n.nspname <> 'information_schema'
          AND c.relname <> 'alembic_version'
          AND c.relkind IN ('r','p','v','m','S')
          AND NOT EXISTS (
              SELECT 1
              FROM pg_depend AS d
              WHERE d.classid = 'pg_class'::regclass
                AND d.objid = c.oid
                AND d.deptype = 'e'
          )
        ORDER BY n.nspname, c.relname, c.relkind
        """,
    )

    columns = _rows(
        conn,
        """
        SELECT n.nspname AS schema_name,
               c.relname AS relation_name,
               a.attnum AS ordinal_position,
               a.attname AS column_name,
               format_type(a.atttypid, a.atttypmod) AS data_type,
               a.attnotnull AS not_null,
               a.attidentity AS identity_kind,
               a.attgenerated AS generated_kind,
               pg_get_expr(ad.adbin, ad.adrelid, true) AS default_expression,
               CASE WHEN a.attcollation = 0 THEN NULL
                    ELSE a.attcollation::regcollation::text END AS collation_name
        FROM pg_attribute AS a
        JOIN pg_class AS c ON c.oid = a.attrelid
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        LEFT JOIN pg_attrdef AS ad
          ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
        WHERE n.nspname NOT LIKE 'pg_%'
          AND n.nspname <> 'information_schema'
          AND c.relname <> 'alembic_version'
          AND c.relkind IN ('r','p','v','m')
          AND a.attnum > 0
          AND NOT a.attisdropped
          AND NOT EXISTS (
              SELECT 1
              FROM pg_depend AS d
              WHERE d.classid = 'pg_class'::regclass
                AND d.objid = c.oid
                AND d.deptype = 'e'
          )
        ORDER BY n.nspname, c.relname, a.attnum
        """,
    )

    constraints = _rows(
        conn,
        """
        SELECT n.nspname AS schema_name,
               c.relname AS relation_name,
               con.conname AS constraint_name,
               con.contype AS constraint_type,
               con.convalidated AS validated,
               con.condeferrable AS deferrable,
               con.condeferred AS initially_deferred,
               pg_get_constraintdef(con.oid, true) AS definition
        FROM pg_constraint AS con
        JOIN pg_class AS c ON c.oid = con.conrelid
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname NOT LIKE 'pg_%'
          AND n.nspname <> 'information_schema'
          AND c.relname <> 'alembic_version'
          AND NOT EXISTS (
              SELECT 1
              FROM pg_depend AS d
              WHERE d.classid = 'pg_class'::regclass
                AND d.objid = c.oid
                AND d.deptype = 'e'
          )
        ORDER BY n.nspname, c.relname, con.conname
        """,
    )

    indexes = _rows(
        conn,
        """
        SELECT tn.nspname AS schema_name,
               t.relname AS relation_name,
               i.relname AS index_name,
               pg_get_userbyid(i.relowner) AS owner_name,
               x.indisunique AS is_unique,
               x.indisprimary AS is_primary,
               x.indisvalid AS is_valid,
               x.indisready AS is_ready,
               pg_get_indexdef(i.oid) AS definition
        FROM pg_index AS x
        JOIN pg_class AS i ON i.oid = x.indexrelid
        JOIN pg_class AS t ON t.oid = x.indrelid
        JOIN pg_namespace AS tn ON tn.oid = t.relnamespace
        WHERE tn.nspname NOT LIKE 'pg_%'
          AND tn.nspname <> 'information_schema'
          AND t.relname <> 'alembic_version'
          AND NOT EXISTS (
              SELECT 1
              FROM pg_depend AS d
              WHERE d.classid = 'pg_class'::regclass
                AND d.objid = t.oid
                AND d.deptype = 'e'
          )
        ORDER BY tn.nspname, t.relname, i.relname
        """,
    )

    triggers = _rows(
        conn,
        """
        SELECT n.nspname AS schema_name,
               c.relname AS relation_name,
               t.tgname AS trigger_name,
               t.tgenabled AS enabled,
               pg_get_triggerdef(t.oid, true) AS definition
        FROM pg_trigger AS t
        JOIN pg_class AS c ON c.oid = t.tgrelid
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE NOT t.tgisinternal
          AND n.nspname NOT LIKE 'pg_%'
          AND n.nspname <> 'information_schema'
          AND c.relname <> 'alembic_version'
          AND NOT EXISTS (
              SELECT 1
              FROM pg_depend AS d
              WHERE d.classid = 'pg_class'::regclass
                AND d.objid = c.oid
                AND d.deptype = 'e'
          )
        ORDER BY n.nspname, c.relname, t.tgname
        """,
    )

    routines = _rows(
        conn,
        """
        SELECT n.nspname AS schema_name,
               p.proname AS routine_name,
               pg_get_function_identity_arguments(p.oid) AS identity_arguments,
               p.prokind AS routine_kind,
               pg_get_userbyid(p.proowner) AS owner_name,
               p.prosecdef AS security_definer,
               p.provolatile AS volatility,
               p.proparallel AS parallel_safety,
               p.proisstrict AS strict,
               COALESCE(array_to_string(p.proconfig, E'\n'), '') AS config,
               pg_get_functiondef(p.oid) AS definition
        FROM pg_proc AS p
        JOIN pg_namespace AS n ON n.oid = p.pronamespace
        WHERE n.nspname NOT LIKE 'pg_%'
          AND n.nspname <> 'information_schema'
          AND NOT EXISTS (
              SELECT 1
              FROM pg_depend AS d
              WHERE d.classid = 'pg_proc'::regclass
                AND d.objid = p.oid
                AND d.deptype = 'e'
          )
        ORDER BY n.nspname, p.proname,
                 pg_get_function_identity_arguments(p.oid), p.prokind
        """,
    )

    views = _rows(
        conn,
        """
        SELECT n.nspname AS schema_name,
               c.relname AS view_name,
               c.relkind,
               pg_get_viewdef(c.oid, true) AS definition
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('v','m')
          AND n.nspname NOT LIKE 'pg_%'
          AND n.nspname <> 'information_schema'
          AND NOT EXISTS (
              SELECT 1
              FROM pg_depend AS d
              WHERE d.classid = 'pg_class'::regclass
                AND d.objid = c.oid
                AND d.deptype = 'e'
          )
        ORDER BY n.nspname, c.relname
        """,
    )

    policies = _rows(
        conn,
        """
        SELECT n.nspname AS schema_name,
               c.relname AS relation_name,
               p.polname AS policy_name,
               p.polpermissive AS permissive,
               p.polcmd AS command,
               COALESCE(
                 (
                   SELECT string_agg(
                     CASE WHEN role_oid = 0 THEN 'PUBLIC'
                          ELSE pg_get_userbyid(role_oid) END,
                     ',' ORDER BY role_oid
                   )
                   FROM unnest(p.polroles) AS role_oid
                 ),
                 ''
               ) AS roles,
               pg_get_expr(p.polqual, p.polrelid, true) AS using_expression,
               pg_get_expr(p.polwithcheck, p.polrelid, true) AS check_expression
        FROM pg_policy AS p
        JOIN pg_class AS c ON c.oid = p.polrelid
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname NOT LIKE 'pg_%'
          AND n.nspname <> 'information_schema'
          AND NOT EXISTS (
              SELECT 1
              FROM pg_depend AS d
              WHERE d.classid = 'pg_class'::regclass
                AND d.objid = c.oid
                AND d.deptype = 'e'
          )
        ORDER BY n.nspname, c.relname, p.polname
        """,
    )

    relation_acls = _rows(
        conn,
        """
        SELECT n.nspname AS schema_name,
               c.relname AS relation_name,
               COALESCE(grantee.rolname, 'PUBLIC') AS grantee_name,
               COALESCE(grantor.rolname, 'PUBLIC') AS grantor_name,
               acl.privilege_type,
               acl.is_grantable
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        CROSS JOIN LATERAL aclexplode(
            COALESCE(c.relacl, acldefault('r', c.relowner))
        ) AS acl
        LEFT JOIN pg_roles AS grantor ON grantor.oid = acl.grantor
        LEFT JOIN pg_roles AS grantee ON grantee.oid = NULLIF(acl.grantee, 0)
        WHERE n.nspname NOT LIKE 'pg_%'
          AND n.nspname <> 'information_schema'
          AND c.relname <> 'alembic_version'
          AND c.relkind IN ('r','p','v','m','S')
          AND NOT EXISTS (
              SELECT 1
              FROM pg_depend AS d
              WHERE d.classid = 'pg_class'::regclass
                AND d.objid = c.oid
                AND d.deptype = 'e'
          )
        ORDER BY n.nspname, c.relname, grantee_name, grantor_name,
                 acl.privilege_type, acl.is_grantable
        """,
    )

    routine_acls = _rows(
        conn,
        """
        SELECT n.nspname AS schema_name,
               p.proname AS routine_name,
               pg_get_function_identity_arguments(p.oid) AS identity_arguments,
               COALESCE(grantee.rolname, 'PUBLIC') AS grantee_name,
               COALESCE(grantor.rolname, 'PUBLIC') AS grantor_name,
               acl.privilege_type,
               acl.is_grantable
        FROM pg_proc AS p
        JOIN pg_namespace AS n ON n.oid = p.pronamespace
        CROSS JOIN LATERAL aclexplode(
            COALESCE(p.proacl, acldefault('f', p.proowner))
        ) AS acl
        LEFT JOIN pg_roles AS grantor ON grantor.oid = acl.grantor
        LEFT JOIN pg_roles AS grantee ON grantee.oid = NULLIF(acl.grantee, 0)
        WHERE n.nspname NOT LIKE 'pg_%'
          AND n.nspname <> 'information_schema'
          AND NOT EXISTS (
              SELECT 1
              FROM pg_depend AS d
              WHERE d.classid = 'pg_proc'::regclass
                AND d.objid = p.oid
                AND d.deptype = 'e'
          )
        ORDER BY n.nspname, p.proname,
                 pg_get_function_identity_arguments(p.oid),
                 grantee_name, grantor_name,
                 acl.privilege_type, acl.is_grantable
        """,
    )

    default_acls = _rows(
        conn,
        """
        SELECT owner.rolname AS owner_name,
               COALESCE(n.nspname, '') AS schema_name,
               d.defaclobjtype AS object_type,
               COALESCE(grantee.rolname, 'PUBLIC') AS grantee_name,
               COALESCE(grantor.rolname, 'PUBLIC') AS grantor_name,
               acl.privilege_type,
               acl.is_grantable
        FROM pg_default_acl AS d
        JOIN pg_roles AS owner ON owner.oid = d.defaclrole
        LEFT JOIN pg_namespace AS n ON n.oid = d.defaclnamespace
        CROSS JOIN LATERAL aclexplode(d.defaclacl) AS acl
        LEFT JOIN pg_roles AS grantor ON grantor.oid = acl.grantor
        LEFT JOIN pg_roles AS grantee ON grantee.oid = NULLIF(acl.grantee, 0)
        WHERE n.nspname IS NULL
           OR (n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema')
        ORDER BY owner.rolname, schema_name, d.defaclobjtype,
                 grantee_name, grantor_name,
                 acl.privilege_type, acl.is_grantable
        """,
    )

    enum_values = _rows(
        conn,
        """
        SELECT n.nspname AS schema_name,
               t.typname AS type_name,
               pg_get_userbyid(t.typowner) AS owner_name,
               e.enumsortorder::text AS sort_order,
               e.enumlabel AS enum_label
        FROM pg_type AS t
        JOIN pg_namespace AS n ON n.oid = t.typnamespace
        JOIN pg_enum AS e ON e.enumtypid = t.oid
        WHERE n.nspname NOT LIKE 'pg_%'
          AND n.nspname <> 'information_schema'
          AND NOT EXISTS (
              SELECT 1
              FROM pg_depend AS d
              WHERE d.classid = 'pg_type'::regclass
                AND d.objid = t.oid
                AND d.deptype = 'e'
          )
        ORDER BY n.nspname, t.typname, e.enumsortorder
        """,
    )

    partman_config: list[str] = []
    if _one(conn, "SELECT to_regclass('partman.part_config') IS NOT NULL"):
        partman_config = [
            row["row_json"]
            for row in _rows(
                conn,
                """
                SELECT row_to_json(config_row)::text AS row_json
                FROM partman.part_config AS config_row
                ORDER BY config_row.parent_table
                """,
            )
        ]

    partman_sub_config: list[str] = []
    if _one(conn, "SELECT to_regclass('partman.part_config_sub') IS NOT NULL"):
        partman_sub_config = [
            row["row_json"]
            for row in _rows(
                conn,
                """
                SELECT row_to_json(config_row)::text AS row_json
                FROM partman.part_config_sub AS config_row
                ORDER BY config_row.sub_parent
                """,
            )
        ]

    alembic_state: list[str] = []
    if _one(conn, "SELECT to_regclass('public.alembic_version') IS NOT NULL"):
        alembic_state = [
            row["version_num"]
            for row in _rows(
                conn,
                "SELECT version_num FROM public.alembic_version ORDER BY version_num",
            )
        ]

    return {
        "schemas": schemas,
        "schema_acls": schema_acls,
        "extensions": extensions,
        "relations": relations,
        "columns": columns,
        "constraints": constraints,
        "indexes": indexes,
        "triggers": triggers,
        "routines": routines,
        "views": views,
        "policies": policies,
        "relation_acls": relation_acls,
        "routine_acls": routine_acls,
        "default_acls": default_acls,
        "enum_values": enum_values,
        "partman_config": partman_config,
        "partman_sub_config": partman_sub_config,
        "alembic_state": alembic_state,
    }


def _cluster_snapshot(conn: psycopg.Connection[Any]) -> dict[str, Any]:
    roles = _rows(
        conn,
        """
        SELECT rolname AS role_name,
               rolcanlogin AS can_login,
               rolsuper AS superuser,
               rolinherit AS inherit,
               rolcreaterole AS create_role,
               rolcreatedb AS create_db,
               rolreplication AS replication,
               rolbypassrls AS bypass_rls,
               rolconnlimit AS connection_limit,
               COALESCE(rolvaliduntil::text, '') AS valid_until
        FROM pg_roles
        WHERE rolname NOT LIKE 'pg_%'
        ORDER BY rolname
        """,
    )

    memberships = _rows(
        conn,
        """
        SELECT granted.rolname AS granted_role,
               member.rolname AS member_role,
               grantor.rolname AS grantor_role,
               m.admin_option,
               m.inherit_option,
               m.set_option
        FROM pg_auth_members AS m
        JOIN pg_roles AS granted ON granted.oid = m.roleid
        JOIN pg_roles AS member ON member.oid = m.member
        JOIN pg_roles AS grantor ON grantor.oid = m.grantor
        WHERE granted.rolname NOT LIKE 'pg_%'
           OR member.rolname NOT LIKE 'pg_%'
        ORDER BY granted.rolname, member.rolname, grantor.rolname,
                 m.admin_option, m.inherit_option, m.set_option
        """,
    )

    settings = _rows(
        conn,
        """
        SELECT COALESCE(r.rolname, '') AS role_name,
               COALESCE(d.datname, '') AS database_name,
               unnest(s.setconfig) AS setting
        FROM pg_db_role_setting AS s
        LEFT JOIN pg_roles AS r ON r.oid = NULLIF(s.setrole, 0)
        LEFT JOIN pg_database AS d ON d.oid = NULLIF(s.setdatabase, 0)
        ORDER BY role_name, database_name, setting
        """,
    )

    return {
        "roles": roles,
        "memberships": memberships,
        "settings": settings,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("database", "cluster"), required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    with _connect() as conn:
        payload = (
            _database_snapshot(conn)
            if args.mode == "database"
            else _cluster_snapshot(conn)
        )

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
