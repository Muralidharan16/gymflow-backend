"""P3A: bind organization profile capability to current principal membership.

Revision ID: c37d8e9f0a1d
Revises: c27d8e9f0a1c
Create Date: 2026-08-14

The tenant GUC identifies request scope but is not, by itself, a business
identity. This revision wraps the P3A profile functions with an owner or active
organization-user membership proof before the existing tenant/role/gym guard
is allowed to execute.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "c37d8e9f0a1d"
down_revision = "c27d8e9f0a1c"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_API = "app_runtime"
_ORG_USER_SELECT_COLUMNS = ("id", "org_id", "is_active", "deleted_at")


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
        raise RuntimeError("P3A principal binding requires migration_owner")
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
        raise RuntimeError("required P3A principal-binding roles are missing")
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


def _direct_column_acl(bind, relation: str, role_name: str) -> set[tuple[str, str]]:
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
            {"relation": relation, "role_name": role_name},
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
                   pg_catalog.pg_get_function_result(procedure_data.oid)::text AS result_type,
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


def _require_predecessor(bind) -> None:
    owner_acl = _direct_column_acl(bind, "public.owners", _SECURITY_OWNER)
    for column in ("id", "org_id", "onboarding_completed"):
        if (column, "SELECT") not in owner_acl:
            raise RuntimeError(
                f"c27 owner membership capability missing SELECT({column})"
            )

    org_user_acl = _direct_column_acl(bind, "public.organization_users", _SECURITY_OWNER)
    for column in _ORG_USER_SELECT_COLUMNS:
        if (column, "SELECT") in org_user_acl:
            raise RuntimeError(
                f"app_security_owner unexpectedly has organization_users SELECT({column})"
            )

    read_row = _function_row(bind, "current_organization_profile", 0)
    update_row = _function_row(bind, "update_current_organization_profile", 1)
    for name, row, volatility in (
        ("current_organization_profile", read_row, "s"),
        ("update_current_organization_profile", update_row, "v"),
    ):
        if row is None:
            raise RuntimeError(f"P3A predecessor function {name} is missing")
        if row["owner_name"] != _SECURITY_OWNER or not bool(row["prosecdef"]):
            raise RuntimeError(f"P3A predecessor function {name} owner drift")
        if row["volatility"] != volatility:
            raise RuntimeError(f"P3A predecessor function {name} volatility drift")
        if not bool(row["api_execute"]) or bool(row["public_execute"]):
            raise RuntimeError(f"P3A predecessor function {name} EXECUTE ACL drift")

    for name, nargs in (
        ("current_organization_profile_internal", 0),
        ("update_current_organization_profile_internal", 1),
    ):
        if _function_row(bind, name, nargs) is not None:
            raise RuntimeError(f"unexpected pre-existing internal P3A function {name}")


def _require_forward(bind) -> None:
    org_user_acl = _direct_column_acl(bind, "public.organization_users", _SECURITY_OWNER)
    for column in _ORG_USER_SELECT_COLUMNS:
        if (column, "SELECT") not in org_user_acl:
            raise RuntimeError(
                f"app_security_owner lacks organization_users SELECT({column})"
            )

    for name, nargs, volatility in (
        ("current_organization_profile", 0, "s"),
        ("update_current_organization_profile", 1, "v"),
    ):
        row = _function_row(bind, name, nargs)
        if row is None:
            raise RuntimeError(f"P3A principal-bound wrapper {name} is missing")
        if row["owner_name"] != _SECURITY_OWNER or not bool(row["prosecdef"]):
            raise RuntimeError(f"P3A principal-bound wrapper {name} owner drift")
        if row["volatility"] != volatility:
            raise RuntimeError(f"P3A principal-bound wrapper {name} volatility drift")
        if set(row["proconfig"] or []) != {"search_path=pg_catalog", "row_security=on"}:
            raise RuntimeError(f"P3A principal-bound wrapper {name} setting drift")
        if not bool(row["api_execute"]) or bool(row["public_execute"]):
            raise RuntimeError(f"P3A principal-bound wrapper {name} EXECUTE ACL drift")
        source = " ".join(str(row["source"]).split()).lower()
        for token in (
            "app.current_org_id",
            "app.current_user_id",
            "app.current_principal_type",
            "public.owners",
            "public.organization_users",
            "principal organization membership is required",
        ):
            if token not in source:
                raise RuntimeError(f"P3A principal wrapper {name} lost token {token}")

    for name, nargs in (
        ("current_organization_profile_internal", 0),
        ("update_current_organization_profile_internal", 1),
    ):
        row = _function_row(bind, name, nargs)
        if row is None:
            raise RuntimeError(f"P3A internal function {name} is missing")
        if row["owner_name"] != _SECURITY_OWNER or not bool(row["prosecdef"]):
            raise RuntimeError(f"P3A internal function {name} owner drift")
        if bool(row["api_execute"]) or bool(row["public_execute"]):
            raise RuntimeError(f"P3A internal function {name} leaked EXECUTE")


_READ_WRAPPER = r"""
CREATE FUNCTION app_secure.current_organization_profile()
RETURNS TABLE (
    id uuid,
    name text,
    business_type text,
    tagline text,
    description text,
    year_established smallint,
    website_url text,
    social_links jsonb,
    logo_status text,
    logo_thumb_key text,
    logo_medium_key text,
    logo_full_key text,
    cover_status text,
    cover_mobile_key text,
    cover_tablet_key text,
    cover_desktop_key text
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
    v_org_id uuid;
    v_user_id uuid;
    v_authorized boolean := FALSE;
BEGIN
    v_org_text := pg_catalog.current_setting('app.current_org_id', true);
    v_user_text := pg_catalog.current_setting('app.current_user_id', true);
    v_principal_type := pg_catalog.current_setting('app.current_principal_type', true);
    IF v_org_text IS NULL OR pg_catalog.btrim(v_org_text) = ''
       OR v_user_text IS NULL OR pg_catalog.btrim(v_user_text) = ''
       OR v_principal_type IS NULL OR pg_catalog.btrim(v_principal_type) = '' THEN
        RAISE EXCEPTION 'organization profile principal organization membership is required'
            USING ERRCODE = '42501';
    END IF;
    BEGIN
        v_org_id := v_org_text::uuid;
        v_user_id := v_user_text::uuid;
    EXCEPTION
        WHEN invalid_text_representation THEN
            RAISE EXCEPTION 'organization profile principal organization membership is invalid'
                USING ERRCODE = '42501';
    END;

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
        RAISE EXCEPTION 'organization profile principal organization membership is required'
            USING ERRCODE = '42501';
    END IF;

    RETURN QUERY
    SELECT * FROM app_secure.current_organization_profile_internal();
END;
$function$
"""

_UPDATE_WRAPPER = r"""
CREATE FUNCTION app_secure.update_current_organization_profile(p_patch jsonb)
RETURNS TABLE (
    id uuid,
    name text,
    business_type text,
    tagline text,
    description text,
    year_established smallint,
    website_url text,
    social_links jsonb,
    logo_status text,
    logo_thumb_key text,
    logo_medium_key text,
    logo_full_key text,
    cover_status text,
    cover_mobile_key text,
    cover_tablet_key text,
    cover_desktop_key text
)
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
    v_org_id uuid;
    v_user_id uuid;
    v_authorized boolean := FALSE;
BEGIN
    v_org_text := pg_catalog.current_setting('app.current_org_id', true);
    v_user_text := pg_catalog.current_setting('app.current_user_id', true);
    v_principal_type := pg_catalog.current_setting('app.current_principal_type', true);
    IF v_org_text IS NULL OR pg_catalog.btrim(v_org_text) = ''
       OR v_user_text IS NULL OR pg_catalog.btrim(v_user_text) = ''
       OR v_principal_type IS NULL OR pg_catalog.btrim(v_principal_type) = '' THEN
        RAISE EXCEPTION 'organization profile principal organization membership is required'
            USING ERRCODE = '42501';
    END IF;
    BEGIN
        v_org_id := v_org_text::uuid;
        v_user_id := v_user_text::uuid;
    EXCEPTION
        WHEN invalid_text_representation THEN
            RAISE EXCEPTION 'organization profile principal organization membership is invalid'
                USING ERRCODE = '42501';
    END;

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
        RAISE EXCEPTION 'organization profile principal organization membership is required'
            USING ERRCODE = '42501';
    END IF;

    RETURN QUERY
    SELECT *
    FROM app_secure.update_current_organization_profile_internal(p_patch);
END;
$function$
"""


def _set_security_owner(bind) -> None:
    bind.execute(sa.text("SET LOCAL ROLE app_security_owner"))
    if _scalar(bind, "SELECT current_user::text") != _SECURITY_OWNER:
        raise RuntimeError("failed to enter app_security_owner")


def _reset_role(bind) -> None:
    bind.execute(sa.text("RESET ROLE"))
    if _scalar(bind, "SELECT current_user::text") != _MIGRATION_OWNER:
        raise RuntimeError("failed to restore migration_owner")


def upgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    _require_predecessor(bind)

    op.execute(
        "GRANT SELECT ("
        + ", ".join(_ORG_USER_SELECT_COLUMNS)
        + ") ON TABLE public.organization_users TO app_security_owner"
    )

    _set_security_owner(bind)
    try:
        op.execute(
            "ALTER FUNCTION app_secure.current_organization_profile() "
            "RENAME TO current_organization_profile_internal"
        )
        op.execute(
            "ALTER FUNCTION app_secure.update_current_organization_profile(jsonb) "
            "RENAME TO update_current_organization_profile_internal"
        )
        op.execute(
            "REVOKE EXECUTE ON FUNCTION "
            "app_secure.current_organization_profile_internal() FROM app_runtime"
        )
        op.execute(
            "REVOKE EXECUTE ON FUNCTION "
            "app_secure.update_current_organization_profile_internal(jsonb) FROM app_runtime"
        )
        op.execute(_READ_WRAPPER)
        op.execute(_UPDATE_WRAPPER)
        op.execute(
            "REVOKE ALL ON FUNCTION app_secure.current_organization_profile() FROM PUBLIC"
        )
        op.execute(
            "REVOKE ALL ON FUNCTION app_secure.update_current_organization_profile(jsonb) FROM PUBLIC"
        )
        op.execute(
            "GRANT EXECUTE ON FUNCTION app_secure.current_organization_profile() TO app_runtime"
        )
        op.execute(
            "GRANT EXECUTE ON FUNCTION app_secure.update_current_organization_profile(jsonb) TO app_runtime"
        )
    finally:
        _reset_role(bind)

    _require_forward(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    _require_forward(bind)

    _set_security_owner(bind)
    try:
        op.execute(
            "DROP FUNCTION app_secure.update_current_organization_profile(jsonb) RESTRICT"
        )
        op.execute("DROP FUNCTION app_secure.current_organization_profile() RESTRICT")
        op.execute(
            "ALTER FUNCTION app_secure.current_organization_profile_internal() "
            "RENAME TO current_organization_profile"
        )
        op.execute(
            "ALTER FUNCTION app_secure.update_current_organization_profile_internal(jsonb) "
            "RENAME TO update_current_organization_profile"
        )
        op.execute(
            "GRANT EXECUTE ON FUNCTION app_secure.current_organization_profile() TO app_runtime"
        )
        op.execute(
            "GRANT EXECUTE ON FUNCTION app_secure.update_current_organization_profile(jsonb) TO app_runtime"
        )
    finally:
        _reset_role(bind)

    op.execute(
        "REVOKE SELECT ("
        + ", ".join(_ORG_USER_SELECT_COLUMNS)
        + ") ON TABLE public.organization_users FROM app_security_owner"
    )

    _require_predecessor(bind)
