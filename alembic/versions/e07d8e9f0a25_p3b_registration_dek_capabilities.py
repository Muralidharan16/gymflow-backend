"""P3B: add registration-specific DEK capabilities.

Revision ID: e07d8e9f0a25
Revises: d07d8e9f0a24
Create Date: 2026-08-15

The API runtime receives EXECUTE-only access to registration-scoped key
capabilities. It never receives direct encryption-key table or sequence ACLs.
The security owner retains only the exact column/sequence privileges required
inside SECURITY DEFINER functions. First-key installation is serialized per
organization/domain and returns the existing winner under a concurrent race.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "e07d8e9f0a25"
down_revision = "d07d8e9f0a24"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_API = "app_runtime"
_KEY_TABLE = "public.encryption_key_registry"
_KEY_SEQUENCE = "public.encryption_key_registry_key_version_seq"
_KEY_SCOPE = "organization_registrations"
_FUNCTIONS = {
    "current_registration_dek": 0,
    "install_registration_dek": 1,
    "lookup_registration_dek": 1,
}
_PREDECESSOR_SECURITY_COLUMN_ACL = {
    ("tenant_id", "SELECT"),
    ("key_version", "SELECT"),
    ("encrypted_dek", "SELECT"),
}
_FORWARD_SECURITY_COLUMN_ACL = _PREDECESSOR_SECURITY_COLUMN_ACL | {
    ("table_name", "SELECT"),
    ("key_status", "SELECT"),
    ("tenant_id", "INSERT"),
    ("table_name", "INSERT"),
    ("encrypted_dek", "INSERT"),
    ("key_status", "INSERT"),
}


def _scalar(bind, sql: str, params: dict[str, object] | None = None):
    return bind.execute(sa.text(sql), params or {}).scalar_one()


def _require_identity(bind) -> None:
    current = bind.execute(
        sa.text(
            """
            SELECT session_user::text, current_user::text,
                   role_data.rolsuper, role_data.rolinherit,
                   role_data.rolcreatedb, role_data.rolcreaterole,
                   role_data.rolreplication, role_data.rolbypassrls
            FROM pg_catalog.pg_roles AS role_data
            WHERE role_data.rolname = current_user
            """
        )
    ).one()
    if current[0] != _MIGRATION_OWNER or current[1] != _MIGRATION_OWNER:
        raise RuntimeError("P3B registration DEK migration requires migration_owner")
    if any(bool(value) for value in current[2:]):
        raise RuntimeError("migration_owner violates the reduced role contract")

    roles = bind.execute(
        sa.text(
            """
            SELECT rolname, rolcanlogin, rolsuper, rolinherit, rolcreatedb,
                   rolcreaterole, rolreplication, rolbypassrls
            FROM pg_catalog.pg_roles
            WHERE rolname IN (:security_owner, :api_role)
            """
        ),
        {"security_owner": _SECURITY_OWNER, "api_role": _API},
    ).mappings().all()
    by_name = {str(item["rolname"]): item for item in roles}
    if set(by_name) != {_SECURITY_OWNER, _API}:
        raise RuntimeError("required P3B DEK roles are missing")
    for role_name, role in by_name.items():
        if any(
            bool(role[key])
            for key in (
                "rolcanlogin",
                "rolsuper",
                "rolinherit",
                "rolcreatedb",
                "rolcreaterole",
                "rolreplication",
                "rolbypassrls",
            )
        ):
            raise RuntimeError(f"managed role {role_name} is over-privileged")

    if not _scalar(
        bind,
        "SELECT pg_catalog.pg_has_role(session_user, :role_name, 'SET')",
        {"role_name": _SECURITY_OWNER},
    ):
        raise RuntimeError("migration_owner lacks bounded SET to app_security_owner")


def _column_acl(bind, role_name: str) -> set[tuple[str, str]]:
    return {
        (str(row[0]), str(row[1]))
        for row in bind.execute(
            sa.text(
                """
                SELECT attribute_data.attname::text,
                       acl_data.privilege_type::text
                FROM pg_catalog.pg_attribute AS attribute_data
                CROSS JOIN LATERAL pg_catalog.aclexplode(attribute_data.attacl) AS acl_data
                JOIN pg_catalog.pg_roles AS grantee_role
                  ON grantee_role.oid = acl_data.grantee
                WHERE attribute_data.attrelid = pg_catalog.to_regclass(:relation)
                  AND attribute_data.attnum > 0
                  AND NOT attribute_data.attisdropped
                  AND grantee_role.rolname = :role_name
                ORDER BY attribute_data.attname, acl_data.privilege_type
                """
            ),
            {"relation": _KEY_TABLE, "role_name": role_name},
        ).all()
    }


def _table_acl(bind, role_name: str) -> set[str]:
    return {
        str(row[0])
        for row in bind.execute(
            sa.text(
                """
                SELECT acl_data.privilege_type::text
                FROM pg_catalog.pg_class AS relation_data
                CROSS JOIN LATERAL pg_catalog.aclexplode(relation_data.relacl) AS acl_data
                JOIN pg_catalog.pg_roles AS grantee_role
                  ON grantee_role.oid = acl_data.grantee
                WHERE relation_data.oid = pg_catalog.to_regclass(:relation)
                  AND grantee_role.rolname = :role_name
                ORDER BY acl_data.privilege_type
                """
            ),
            {"relation": _KEY_TABLE, "role_name": role_name},
        ).all()
    }


def _sequence_acl(bind, role_name: str) -> set[str]:
    return {
        str(row[0])
        for row in bind.execute(
            sa.text(
                """
                SELECT acl_data.privilege_type::text
                FROM pg_catalog.pg_class AS sequence_data
                CROSS JOIN LATERAL pg_catalog.aclexplode(sequence_data.relacl) AS acl_data
                JOIN pg_catalog.pg_roles AS grantee_role
                  ON grantee_role.oid = acl_data.grantee
                WHERE sequence_data.oid = pg_catalog.to_regclass(:sequence_name)
                  AND sequence_data.relkind = 'S'
                  AND grantee_role.rolname = :role_name
                ORDER BY acl_data.privilege_type
                """
            ),
            {"sequence_name": _KEY_SEQUENCE, "role_name": role_name},
        ).all()
    }


def _function_row(bind, name: str, nargs: int):
    return bind.execute(
        sa.text(
            """
            SELECT owner_role.rolname::text AS owner_name,
                   procedure_data.prosecdef,
                   procedure_data.provolatile::text AS volatility,
                   procedure_data.proconfig,
                   procedure_data.prosrc::text AS source,
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
                       WHERE grantee_role.rolname = :api_role
                         AND acl_data.privilege_type = 'EXECUTE'
                   ) AS api_execute,
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
                   ) AS public_execute
            FROM pg_catalog.pg_proc AS procedure_data
            JOIN pg_catalog.pg_namespace AS namespace_data
              ON namespace_data.oid = procedure_data.pronamespace
            JOIN pg_catalog.pg_roles AS owner_role
              ON owner_role.oid = procedure_data.proowner
            WHERE namespace_data.nspname = 'app_secure'
              AND procedure_data.proname = :name
              AND procedure_data.pronargs = :nargs
              AND procedure_data.prokind = 'f'
            """
        ),
        {"name": name, "nargs": nargs, "api_role": _API},
    ).mappings().one_or_none()


def _principal_guard_sql(label: str) -> str:
    return f"""
    v_org_text := pg_catalog.current_setting('app.current_org_id', true);
    v_user_text := pg_catalog.current_setting('app.current_user_id', true);
    v_principal_type := pg_catalog.current_setting('app.current_principal_type', true);
    v_role := pg_catalog.current_setting('app.current_role', true);
    v_gym := pg_catalog.current_setting('app.current_gym_id', true);

    IF v_org_text IS NULL OR pg_catalog.btrim(v_org_text) = ''
       OR v_user_text IS NULL OR pg_catalog.btrim(v_user_text) = ''
       OR v_principal_type IS NULL OR pg_catalog.btrim(v_principal_type) = '' THEN
        RAISE EXCEPTION '{label} principal organization membership is required'
            USING ERRCODE = '42501';
    END IF;
    BEGIN
        v_org_id := v_org_text::uuid;
        v_user_id := v_user_text::uuid;
    EXCEPTION
        WHEN invalid_text_representation THEN
            RAISE EXCEPTION '{label} principal organization membership is invalid'
                USING ERRCODE = '42501';
    END;

    IF v_role IS NULL
       OR pg_catalog.btrim(v_role) NOT IN ('owner', 'admin')
       OR (v_gym IS NOT NULL AND pg_catalog.btrim(v_gym) <> '') THEN
        RAISE EXCEPTION '{label} admin context is required'
            USING ERRCODE = '42501';
    END IF;

    IF v_principal_type = 'owner' THEN
        SELECT EXISTS (
            SELECT 1
            FROM public.owners AS principal_owner
            WHERE principal_owner.id = v_user_id
              AND principal_owner.org_id = v_org_id
        ) INTO v_authorized;
    ELSIF v_principal_type = 'organization_user' THEN
        SELECT EXISTS (
            SELECT 1
            FROM public.organization_users AS principal_user
            WHERE principal_user.id = v_user_id
              AND principal_user.org_id = v_org_id
              AND principal_user.is_active
              AND principal_user.deleted_at IS NULL
        ) INTO v_authorized;
    END IF;

    IF NOT v_authorized THEN
        RAISE EXCEPTION '{label} principal organization membership is required'
            USING ERRCODE = '42501';
    END IF;
"""


_CURRENT_FUNCTION = f"""
CREATE FUNCTION app_secure.current_registration_dek()
RETURNS TABLE (key_version integer, encrypted_dek bytea)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
SET row_security = on
AS $function$
DECLARE
    v_org_text text;
    v_user_text text;
    v_principal_type text;
    v_role text;
    v_gym text;
    v_org_id uuid;
    v_user_id uuid;
    v_authorized boolean := FALSE;
BEGIN
{_principal_guard_sql('registration DEK current lookup')}
    RETURN QUERY
    SELECT key_data.key_version, key_data.encrypted_dek
    FROM public.encryption_key_registry AS key_data
    WHERE key_data.tenant_id = v_org_id
      AND key_data.table_name = '{_KEY_SCOPE}'
      AND key_data.key_status = 'ACTIVE'
    ORDER BY key_data.key_version DESC
    LIMIT 1;
END;
$function$
"""

_LOOKUP_FUNCTION = f"""
CREATE FUNCTION app_secure.lookup_registration_dek(p_key_version integer)
RETURNS bytea
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
SET row_security = on
AS $function$
DECLARE
    v_org_text text;
    v_user_text text;
    v_principal_type text;
    v_role text;
    v_gym text;
    v_org_id uuid;
    v_user_id uuid;
    v_authorized boolean := FALSE;
    v_encrypted_dek bytea;
BEGIN
{_principal_guard_sql('registration DEK historical lookup')}
    IF p_key_version IS NULL OR p_key_version < 1 THEN
        RAISE EXCEPTION 'registration DEK key version is invalid'
            USING ERRCODE = '22023';
    END IF;

    SELECT key_data.encrypted_dek
      INTO v_encrypted_dek
      FROM public.encryption_key_registry AS key_data
     WHERE key_data.tenant_id = v_org_id
       AND key_data.table_name = '{_KEY_SCOPE}'
       AND key_data.key_version = p_key_version;

    RETURN v_encrypted_dek;
END;
$function$
"""

_INSTALL_FUNCTION = f"""
CREATE FUNCTION app_secure.install_registration_dek(p_encrypted_dek bytea)
RETURNS TABLE (key_version integer, encrypted_dek bytea)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog
SET row_security = on
AS $function$
DECLARE
    v_org_text text;
    v_user_text text;
    v_principal_type text;
    v_role text;
    v_gym text;
    v_org_id uuid;
    v_user_id uuid;
    v_authorized boolean := FALSE;
BEGIN
{_principal_guard_sql('registration DEK installation')}
    IF p_encrypted_dek IS NULL OR pg_catalog.octet_length(p_encrypted_dek) = 0 THEN
        RAISE EXCEPTION 'registration DEK ciphertext is required'
            USING ERRCODE = '22023';
    END IF;

    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(
            v_org_id::text || ':{_KEY_SCOPE}',
            0
        )
    );

    RETURN QUERY
    SELECT key_data.key_version, key_data.encrypted_dek
      FROM public.encryption_key_registry AS key_data
     WHERE key_data.tenant_id = v_org_id
       AND key_data.table_name = '{_KEY_SCOPE}'
       AND key_data.key_status = 'ACTIVE'
     ORDER BY key_data.key_version DESC
     LIMIT 1;
    IF FOUND THEN
        RETURN;
    END IF;

    BEGIN
        RETURN QUERY
        INSERT INTO public.encryption_key_registry (
            tenant_id, table_name, encrypted_dek, key_status
        ) VALUES (
            v_org_id, '{_KEY_SCOPE}', p_encrypted_dek, 'ACTIVE'
        )
        RETURNING encryption_key_registry.key_version,
                  encryption_key_registry.encrypted_dek;
        RETURN;
    EXCEPTION
        WHEN unique_violation THEN
            RETURN QUERY
            SELECT key_data.key_version, key_data.encrypted_dek
              FROM public.encryption_key_registry AS key_data
             WHERE key_data.tenant_id = v_org_id
               AND key_data.table_name = '{_KEY_SCOPE}'
               AND key_data.key_status = 'ACTIVE'
             ORDER BY key_data.key_version DESC
             LIMIT 1;
            IF NOT FOUND THEN
                RAISE;
            END IF;
    END;
END;
$function$
"""


def _require_predecessor(bind) -> None:
    if _column_acl(bind, _SECURITY_OWNER) != _PREDECESSOR_SECURITY_COLUMN_ACL:
        raise RuntimeError("P3B DEK predecessor security-owner column ACL drift")
    if _column_acl(bind, _API):
        raise RuntimeError("app_runtime unexpectedly has direct key-registry column ACL")
    for role_name in (_SECURITY_OWNER, _API):
        if _table_acl(bind, role_name):
            raise RuntimeError(f"{role_name} unexpectedly has direct key-registry table ACL")
        if _sequence_acl(bind, role_name):
            raise RuntimeError(f"{role_name} unexpectedly has direct key sequence ACL")
    for name, nargs in _FUNCTIONS.items():
        if _function_row(bind, name, nargs) is not None:
            raise RuntimeError(f"unexpected pre-existing P3B DEK function {name}")


def _require_forward(bind) -> None:
    if _column_acl(bind, _SECURITY_OWNER) != _FORWARD_SECURITY_COLUMN_ACL:
        raise RuntimeError("P3B DEK security-owner column ACL drift")
    if _column_acl(bind, _API):
        raise RuntimeError("app_runtime leaked direct key-registry column ACL")
    if _table_acl(bind, _SECURITY_OWNER) or _table_acl(bind, _API):
        raise RuntimeError("P3B DEK direct key-registry table ACL leak")
    if _sequence_acl(bind, _SECURITY_OWNER) != {"USAGE"}:
        raise RuntimeError("app_security_owner key sequence ACL must be exactly USAGE")
    if _sequence_acl(bind, _API):
        raise RuntimeError("app_runtime leaked direct key sequence ACL")

    for name, nargs in _FUNCTIONS.items():
        row = _function_row(bind, name, nargs)
        if row is None:
            raise RuntimeError(f"P3B DEK function {name} is missing")
        if row["owner_name"] != _SECURITY_OWNER or not bool(row["prosecdef"]):
            raise RuntimeError(f"P3B DEK function {name} owner/security-definer drift")
        expected_volatility = "v" if name == "install_registration_dek" else "s"
        if row["volatility"] != expected_volatility:
            raise RuntimeError(f"P3B DEK function {name} volatility drift")
        if set(row["proconfig"] or []) != {"search_path=pg_catalog", "row_security=on"}:
            raise RuntimeError(f"P3B DEK function {name} setting drift")
        if not bool(row["api_execute"]) or bool(row["public_execute"]):
            raise RuntimeError(f"P3B DEK function {name} EXECUTE ACL drift")
        source = " ".join(str(row["source"]).split()).lower()
        for token in (
            "app.current_org_id",
            "app.current_user_id",
            "app.current_principal_type",
            "app.current_role",
            "public.owners",
            "public.organization_users",
            "public.encryption_key_registry",
            _KEY_SCOPE,
        ):
            if token not in source:
                raise RuntimeError(f"P3B DEK function {name} lost token {token}")

    install_source = " ".join(
        str(_function_row(bind, "install_registration_dek", 1)["source"]).split()
    ).lower()
    for token in ("pg_advisory_xact_lock", "hashtextextended", "unique_violation"):
        if token not in install_source:
            raise RuntimeError(f"P3B DEK installer lost concurrency token {token}")


def _install_functions(bind) -> None:
    had_create = bool(
        _scalar(
            bind,
            "SELECT pg_catalog.has_schema_privilege(:role_name, 'app_secure', 'CREATE')",
            {"role_name": _SECURITY_OWNER},
        )
    )
    if not had_create:
        bind.execute(sa.text("GRANT CREATE ON SCHEMA app_secure TO app_security_owner"))

    bind.execute(sa.text("SET LOCAL ROLE app_security_owner"))
    try:
        for function_sql in (_CURRENT_FUNCTION, _LOOKUP_FUNCTION, _INSTALL_FUNCTION):
            bind.execute(sa.text(function_sql))
        bind.execute(sa.text("REVOKE ALL ON FUNCTION app_secure.current_registration_dek() FROM PUBLIC"))
        bind.execute(sa.text("REVOKE ALL ON FUNCTION app_secure.lookup_registration_dek(integer) FROM PUBLIC"))
        bind.execute(sa.text("REVOKE ALL ON FUNCTION app_secure.install_registration_dek(bytea) FROM PUBLIC"))
        bind.execute(sa.text("GRANT EXECUTE ON FUNCTION app_secure.current_registration_dek() TO app_runtime"))
        bind.execute(sa.text("GRANT EXECUTE ON FUNCTION app_secure.lookup_registration_dek(integer) TO app_runtime"))
        bind.execute(sa.text("GRANT EXECUTE ON FUNCTION app_secure.install_registration_dek(bytea) TO app_runtime"))
    finally:
        bind.execute(sa.text("RESET ROLE"))

    if not had_create:
        bind.execute(sa.text("REVOKE CREATE ON SCHEMA app_secure FROM app_security_owner"))


def upgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    _require_predecessor(bind)

    bind.execute(
        sa.text(
            """
            GRANT SELECT (table_name, key_status),
                  INSERT (tenant_id, table_name, encrypted_dek, key_status)
            ON TABLE public.encryption_key_registry
            TO app_security_owner
            """
        )
    )
    bind.execute(
        sa.text(
            "GRANT USAGE ON SEQUENCE public.encryption_key_registry_key_version_seq "
            "TO app_security_owner"
        )
    )
    _install_functions(bind)
    _require_forward(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    _require_forward(bind)

    bind.execute(sa.text("SET LOCAL ROLE app_security_owner"))
    try:
        bind.execute(sa.text("DROP FUNCTION app_secure.install_registration_dek(bytea)"))
        bind.execute(sa.text("DROP FUNCTION app_secure.lookup_registration_dek(integer)"))
        bind.execute(sa.text("DROP FUNCTION app_secure.current_registration_dek()"))
    finally:
        bind.execute(sa.text("RESET ROLE"))

    bind.execute(
        sa.text(
            "REVOKE USAGE ON SEQUENCE public.encryption_key_registry_key_version_seq "
            "FROM app_security_owner"
        )
    )
    bind.execute(
        sa.text(
            """
            REVOKE SELECT (table_name, key_status),
                   INSERT (tenant_id, table_name, encrypted_dek, key_status)
            ON TABLE public.encryption_key_registry
            FROM app_security_owner
            """
        )
    )
    _require_predecessor(bind)
