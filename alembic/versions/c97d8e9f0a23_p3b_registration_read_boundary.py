"""P3B: establish tenant-bound masked registration read capabilities.

Revision ID: c97d8e9f0a23
Revises: c87d8e9f0a22
Create Date: 2026-08-15

This is the P3B expand step. It places organization_registrations behind FORCE
RLS and adds principal-bound SECURITY DEFINER capabilities that expose only
masked registration metadata or a boolean existence check. Existing mutation
ACLs are intentionally not contracted in this revision; application callers
move to the capabilities first, then a later P3B contract revision removes the
legacy direct-table path without forcing a flag-day deployment.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "c97d8e9f0a23"
down_revision = "c87d8e9f0a22"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_API = "app_runtime"
_TABLE = "public.organization_registrations"
_POLICY = "p3b_tenant_isolation_organization_registrations"
_READ_COLUMNS = (
    "id",
    "org_id",
    "id_type",
    "id_number_masked",
    "country_code",
    "is_verified",
    "verified_at",
)


def _scalar(bind, sql: str, params: dict[str, object] | None = None):
    return bind.execute(sa.text(sql), params or {}).scalar_one()


def _require_identity(bind) -> None:
    row = bind.execute(
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
    if row[0] != _MIGRATION_OWNER or row[1] != _MIGRATION_OWNER:
        raise RuntimeError("P3B registration boundary requires migration_owner")
    if any(bool(value) for value in row[2:]):
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
        raise RuntimeError("required P3B managed roles are missing")
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


def _relation_state(bind) -> tuple[str, bool, bool]:
    row = bind.execute(
        sa.text(
            """
            SELECT pg_catalog.pg_get_userbyid(relation_data.relowner)::text,
                   relation_data.relrowsecurity,
                   relation_data.relforcerowsecurity
            FROM pg_catalog.pg_class AS relation_data
            WHERE relation_data.oid = pg_catalog.to_regclass(:relation)
            """
        ),
        {"relation": _TABLE},
    ).one_or_none()
    if row is None:
        raise RuntimeError("organization_registrations is missing")
    return str(row[0]), bool(row[1]), bool(row[2])


def _policy_exists(bind) -> bool:
    return bool(
        _scalar(
            bind,
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_policy AS policy_data
                WHERE policy_data.polrelid = pg_catalog.to_regclass(:relation)
                  AND policy_data.polname = :policy
            )
            """,
            {"relation": _TABLE, "policy": _POLICY},
        )
    )


def _direct_column_acl(bind, role_name: str) -> set[tuple[str, str]]:
    return {
        (str(row[0]), str(row[1]))
        for row in bind.execute(
            sa.text(
                """
                SELECT attribute_data.attname::text,
                       acl_data.privilege_type::text
                FROM pg_catalog.pg_attribute AS attribute_data
                CROSS JOIN LATERAL pg_catalog.aclexplode(
                    attribute_data.attacl
                ) AS acl_data
                JOIN pg_catalog.pg_roles AS grantee_role
                  ON grantee_role.oid = acl_data.grantee
                WHERE attribute_data.attrelid = pg_catalog.to_regclass(:relation)
                  AND attribute_data.attnum > 0
                  AND NOT attribute_data.attisdropped
                  AND grantee_role.rolname = :role_name
                ORDER BY attribute_data.attname, acl_data.privilege_type
                """
            ),
            {"relation": _TABLE, "role_name": role_name},
        ).all()
    }


def _function_row(bind, name: str):
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
              AND procedure_data.pronargs = 0
              AND procedure_data.prokind = 'f'
            """
        ),
        {"name": name, "api_role": _API},
    ).mappings().one_or_none()


def _require_predecessor(bind) -> None:
    owner, rls, force_rls = _relation_state(bind)
    if owner != _MIGRATION_OWNER:
        raise RuntimeError(f"unexpected organization_registrations owner: {owner!r}")
    if rls or force_rls:
        raise RuntimeError("organization_registrations unexpectedly already uses RLS")
    if _policy_exists(bind):
        raise RuntimeError("P3B registration RLS policy unexpectedly already exists")
    if _direct_column_acl(bind, _SECURITY_OWNER):
        raise RuntimeError("app_security_owner unexpectedly has registration column ACL")
    for name in (
        "current_organization_registrations",
        "current_organization_has_registration",
    ):
        if _function_row(bind, name) is not None:
            raise RuntimeError(f"unexpected pre-existing P3B function {name}")


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


_LIST_FUNCTION = f"""
CREATE FUNCTION app_secure.current_organization_registrations()
RETURNS TABLE (
    id uuid,
    id_type text,
    id_number_masked text,
    country_code text,
    is_verified boolean,
    verified_at timestamp with time zone
)
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
{_principal_guard_sql('organization registration read')}
    RETURN QUERY
    SELECT registration.id,
           registration.id_type::text,
           registration.id_number_masked::text,
           registration.country_code::text,
           registration.is_verified,
           registration.verified_at
    FROM public.organization_registrations AS registration
    WHERE registration.org_id = v_org_id
    ORDER BY registration.country_code, registration.id_type, registration.id;
END;
$function$
"""

_EXISTS_FUNCTION = f"""
CREATE FUNCTION app_secure.current_organization_has_registration()
RETURNS boolean
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
{_principal_guard_sql('organization registration existence')}
    RETURN EXISTS (
        SELECT 1
        FROM public.organization_registrations AS registration
        WHERE registration.org_id = v_org_id
    );
END;
$function$
"""


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
        bind.execute(sa.text(_LIST_FUNCTION))
        bind.execute(sa.text(_EXISTS_FUNCTION))
        bind.execute(sa.text("REVOKE ALL ON FUNCTION app_secure.current_organization_registrations() FROM PUBLIC"))
        bind.execute(sa.text("REVOKE ALL ON FUNCTION app_secure.current_organization_has_registration() FROM PUBLIC"))
        bind.execute(sa.text("GRANT EXECUTE ON FUNCTION app_secure.current_organization_registrations() TO app_runtime"))
        bind.execute(sa.text("GRANT EXECUTE ON FUNCTION app_secure.current_organization_has_registration() TO app_runtime"))
    finally:
        bind.execute(sa.text("RESET ROLE"))

    if not had_create:
        bind.execute(sa.text("REVOKE CREATE ON SCHEMA app_secure FROM app_security_owner"))


def _require_forward(bind) -> None:
    owner, rls, force_rls = _relation_state(bind)
    if owner != _MIGRATION_OWNER or not rls or not force_rls:
        raise RuntimeError("P3B registration RLS/ownership contract drift")
    if not _policy_exists(bind):
        raise RuntimeError("P3B registration tenant policy is missing")

    acl = _direct_column_acl(bind, _SECURITY_OWNER)
    expected_acl = {(column, "SELECT") for column in _READ_COLUMNS}
    if not expected_acl.issubset(acl):
        raise RuntimeError("app_security_owner lacks bounded registration read columns")
    if ("id_number_encrypted", "SELECT") in acl:
        raise RuntimeError("app_security_owner must not read encrypted registration payloads")

    for name in (
        "current_organization_registrations",
        "current_organization_has_registration",
    ):
        row = _function_row(bind, name)
        if row is None:
            raise RuntimeError(f"P3B function {name} is missing")
        if row["owner_name"] != _SECURITY_OWNER or not bool(row["prosecdef"]):
            raise RuntimeError(f"P3B function {name} owner/security-definer drift")
        if row["volatility"] != "s":
            raise RuntimeError(f"P3B function {name} volatility drift")
        if set(row["proconfig"] or []) != {"search_path=pg_catalog", "row_security=on"}:
            raise RuntimeError(f"P3B function {name} setting drift")
        if not bool(row["api_execute"]) or bool(row["public_execute"]):
            raise RuntimeError(f"P3B function {name} EXECUTE ACL drift")
        source = " ".join(str(row["source"]).split()).lower()
        for token in (
            "app.current_org_id",
            "app.current_user_id",
            "app.current_principal_type",
            "app.current_role",
            "public.owners",
            "public.organization_users",
            "public.organization_registrations",
        ):
            if token not in source:
                raise RuntimeError(f"P3B function {name} lost token {token}")


def upgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    _require_predecessor(bind)

    bind.execute(sa.text("ALTER TABLE public.organization_registrations ENABLE ROW LEVEL SECURITY"))
    bind.execute(sa.text("ALTER TABLE public.organization_registrations FORCE ROW LEVEL SECURITY"))
    bind.execute(
        sa.text(
            f"""
            CREATE POLICY {_POLICY}
            ON public.organization_registrations
            USING (
                org_id = NULLIF(
                    pg_catalog.current_setting('app.current_org_id', true), ''
                )::uuid
            )
            WITH CHECK (
                org_id = NULLIF(
                    pg_catalog.current_setting('app.current_org_id', true), ''
                )::uuid
            )
            """
        )
    )

    columns_sql = ", ".join(_READ_COLUMNS)
    bind.execute(
        sa.text(
            f"GRANT SELECT ({columns_sql}) ON public.organization_registrations "
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
        bind.execute(sa.text("DROP FUNCTION app_secure.current_organization_has_registration()"))
        bind.execute(sa.text("DROP FUNCTION app_secure.current_organization_registrations()"))
    finally:
        bind.execute(sa.text("RESET ROLE"))

    columns_sql = ", ".join(_READ_COLUMNS)
    bind.execute(
        sa.text(
            f"REVOKE SELECT ({columns_sql}) ON public.organization_registrations "
            "FROM app_security_owner"
        )
    )
    bind.execute(sa.text(f"DROP POLICY {_POLICY} ON public.organization_registrations"))
    bind.execute(sa.text("ALTER TABLE public.organization_registrations NO FORCE ROW LEVEL SECURITY"))
    bind.execute(sa.text("ALTER TABLE public.organization_registrations DISABLE ROW LEVEL SECURITY"))
    _require_predecessor(bind)
