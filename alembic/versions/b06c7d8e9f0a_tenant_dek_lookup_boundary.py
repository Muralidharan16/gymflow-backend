"""Bound historical DEK lookup to the tenant-scoped API runtime.

Revision ID: b06c7d8e9f0a
Revises: af5b6c7d8e9f
"""

from alembic import op
import sqlalchemy as sa

revision = "b06c7d8e9f0a"
down_revision = "af5b6c7d8e9f"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_RUNTIME_ROLE = "app_runtime"
_RELATION = "encryption_key_registry"
_COLUMNS = ("tenant_id", "key_version", "encrypted_dek")
_FUNCTION = "app_secure.lookup_encrypted_dek(uuid,integer)"


def _scalar(bind, sql: str, params: dict | None = None):
    return bind.execute(sa.text(sql), params or {}).scalar_one()


def _require_identity(bind) -> None:
    row = bind.execute(sa.text("""
        SELECT session_user::text,
               current_user::text,
               r.rolsuper,
               r.rolcreatedb,
               r.rolcreaterole,
               r.rolreplication,
               r.rolbypassrls
        FROM pg_catalog.pg_roles AS r
        WHERE r.rolname = current_user
    """)).one()
    if row[0] != _MIGRATION_OWNER or row[1] != _MIGRATION_OWNER:
        raise RuntimeError("tenant DEK lookup migration requires migration_owner")
    if any(bool(value) for value in row[2:]):
        raise RuntimeError("migration_owner violates the reduced role contract")

    roles = bind.execute(sa.text("""
        SELECT rolname, rolcanlogin, rolsuper, rolinherit, rolbypassrls
        FROM pg_catalog.pg_roles
        WHERE rolname IN (:security_owner, :runtime_role)
    """), {
        "security_owner": _SECURITY_OWNER,
        "runtime_role": _RUNTIME_ROLE,
    }).mappings().all()
    by_name = {item["rolname"]: item for item in roles}
    if set(by_name) != {_SECURITY_OWNER, _RUNTIME_ROLE}:
        raise RuntimeError("required tenant DEK roles are missing")
    for name, role in by_name.items():
        if role["rolcanlogin"] or role["rolsuper"] or role["rolinherit"] or role["rolbypassrls"]:
            raise RuntimeError(f"managed role {name} violates NOLOGIN/NOINHERIT/NOBYPASSRLS")

    edge = bind.execute(sa.text("""
        SELECT m.admin_option, m.inherit_option, m.set_option
        FROM pg_catalog.pg_auth_members AS m
        JOIN pg_catalog.pg_roles AS granted ON granted.oid = m.roleid
        JOIN pg_catalog.pg_roles AS member_role ON member_role.oid = m.member
        WHERE granted.rolname = :granted
          AND member_role.rolname = :member
    """), {
        "granted": _SECURITY_OWNER,
        "member": _MIGRATION_OWNER,
    }).mappings().all()
    if (
        len(edge) != 1
        or edge[0]["admin_option"]
        or edge[0]["inherit_option"]
        or not edge[0]["set_option"]
    ):
        raise RuntimeError("migration_owner -> app_security_owner must remain SET-only")


def _has_column(bind, role: str, column: str) -> bool:
    return bool(_scalar(bind, """
        SELECT pg_catalog.has_column_privilege(
            :role,
            :relation,
            :column,
            'SELECT'
        )
    """, {
        "role": role,
        "relation": f"public.{_RELATION}",
        "column": column,
    }))


def _direct_schema_usage(bind, role: str) -> bool:
    return bool(_scalar(bind, """
        SELECT EXISTS (
            SELECT 1
            FROM pg_catalog.pg_namespace AS ns
            CROSS JOIN LATERAL pg_catalog.aclexplode(
                COALESCE(ns.nspacl, pg_catalog.acldefault('n', ns.nspowner))
            ) AS acl
            JOIN pg_catalog.pg_roles AS grantee ON grantee.oid = acl.grantee
            WHERE ns.nspname = 'app_secure'
              AND grantee.rolname = :role
              AND acl.privilege_type = 'USAGE'
        )
    """, {"role": role}))


def _function_row(bind):
    return bind.execute(sa.text("""
        SELECT owner.rolname::text AS owner_name,
               p.prosecdef,
               p.proconfig,
               p.prosrc,
               EXISTS (
                   SELECT 1
                   FROM pg_catalog.aclexplode(
                       COALESCE(p.proacl, pg_catalog.acldefault('f', p.proowner))
                   ) AS acl
                   JOIN pg_catalog.pg_roles AS grantee ON grantee.oid = acl.grantee
                   WHERE grantee.rolname = :runtime_role
                     AND acl.privilege_type = 'EXECUTE'
               ) AS runtime_execute,
               EXISTS (
                   SELECT 1
                   FROM pg_catalog.aclexplode(
                       COALESCE(p.proacl, pg_catalog.acldefault('f', p.proowner))
                   ) AS acl
                   WHERE acl.grantee = 0
                     AND acl.privilege_type = 'EXECUTE'
               ) AS public_execute
        FROM pg_catalog.pg_proc AS p
        JOIN pg_catalog.pg_namespace AS ns ON ns.oid = p.pronamespace
        JOIN pg_catalog.pg_roles AS owner ON owner.oid = p.proowner
        WHERE ns.nspname = 'app_secure'
          AND p.proname = 'lookup_encrypted_dek'
          AND p.pronargs = 2
          AND p.proargtypes[0] = 'uuid'::regtype::oid
          AND p.proargtypes[1] = 'integer'::regtype::oid
          AND p.prorettype = 'bytea'::regtype::oid
          AND p.prokind = 'f'
    """), {"runtime_role": _RUNTIME_ROLE}).mappings().one_or_none()


def _require_predecessor(bind) -> None:
    if not _direct_schema_usage(bind, _RUNTIME_ROLE):
        raise RuntimeError("app_runtime must retain historical app_secure USAGE")
    if _function_row(bind) is not None:
        raise RuntimeError("tenant DEK helper already exists; refusing adoption")
    for column in _COLUMNS:
        if _has_column(bind, _SECURITY_OWNER, column):
            raise RuntimeError(
                f"predecessor unexpectedly grants app_security_owner SELECT({column})"
            )
        if _has_column(bind, _RUNTIME_ROLE, column):
            raise RuntimeError(
                f"predecessor unexpectedly grants app_runtime SELECT({column})"
            )


def _require_forward_contract(bind) -> None:
    if not _direct_schema_usage(bind, _RUNTIME_ROLE):
        raise RuntimeError("app_runtime lost app_secure USAGE")
    for column in _COLUMNS:
        if not _has_column(bind, _SECURITY_OWNER, column):
            raise RuntimeError(
                f"app_security_owner lacks required SELECT({column})"
            )
        if _has_column(bind, _RUNTIME_ROLE, column):
            raise RuntimeError(
                f"app_runtime leaked direct SELECT({column}) on encryption key registry"
            )

    row = _function_row(bind)
    if row is None:
        raise RuntimeError("tenant DEK helper is absent or has a drifted signature")
    if row["owner_name"] != _SECURITY_OWNER or not row["prosecdef"]:
        raise RuntimeError("tenant DEK helper owner/SECURITY DEFINER contract drifted")
    if set(row["proconfig"] or []) != {
        "search_path=pg_catalog",
        "row_security=on",
    }:
        raise RuntimeError("tenant DEK helper session settings drifted")
    if not row["runtime_execute"] or row["public_execute"]:
        raise RuntimeError("tenant DEK helper EXECUTE ACL drifted")
    body = (row["prosrc"] or "").lower()
    for token in (
        "app.current_org_id",
        "p_tenant_id",
        "p_key_version",
        "public.encryption_key_registry",
        "encrypted_dek",
    ):
        if token not in body:
            raise RuntimeError(f"tenant DEK helper lost required contract token {token}")


def upgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    _require_predecessor(bind)

    op.execute("""
        GRANT SELECT (tenant_id, key_version, encrypted_dek)
        ON TABLE public.encryption_key_registry
        TO app_security_owner
    """)

    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute(r"""
        CREATE FUNCTION app_secure.lookup_encrypted_dek(
            p_tenant_id uuid,
            p_key_version integer
        ) RETURNS bytea
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog
        SET row_security = on
        AS $function$
        DECLARE
            v_encrypted_dek bytea;
        BEGIN
            IF p_tenant_id IS NULL
               OR p_key_version IS NULL
               OR p_key_version < 1
               OR pg_catalog.current_setting(
                    'app.current_org_id', true
                  ) IS DISTINCT FROM p_tenant_id::text THEN
                RAISE EXCEPTION 'invalid tenant DEK lookup context'
                    USING ERRCODE = '42501';
            END IF;

            SELECT key_data.encrypted_dek
              INTO v_encrypted_dek
              FROM public.encryption_key_registry AS key_data
             WHERE key_data.tenant_id = p_tenant_id
               AND key_data.key_version = p_key_version;

            RETURN v_encrypted_dek;
        END;
        $function$;
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION app_secure.lookup_encrypted_dek(uuid,integer) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_secure.lookup_encrypted_dek(uuid,integer) TO app_runtime"
    )
    op.execute("RESET ROLE")

    _require_forward_contract(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    _require_forward_contract(bind)

    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute("DROP FUNCTION app_secure.lookup_encrypted_dek(uuid,integer)")
    op.execute("RESET ROLE")
    op.execute("""
        REVOKE SELECT (tenant_id, key_version, encrypted_dek)
        ON TABLE public.encryption_key_registry
        FROM app_security_owner
    """)

    _require_predecessor(bind)
