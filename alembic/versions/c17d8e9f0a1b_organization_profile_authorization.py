"""P3A: bound organization profile access and bootstrap mutation authority.

Revision ID: c17d8e9f0a1b
Revises: b06c7d8e9f0a
Create Date: 2026-08-14

Normal API runtime must not receive base-table SELECT/UPDATE on the tenant-root
``organizations`` table. Profile reads and writes are exposed through
current-tenant SECURITY DEFINER capabilities owned by the reduced
``app_security_owner`` role.

The pre-tenant auth/bootstrap identity historically holds table-wide UPDATE on
``organizations``. P3A narrows only that UPDATE capability to the exact columns
used by onboarding while preserving the already-certified bootstrap
SELECT/INSERT contract.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "c17d8e9f0a1b"
down_revision = "b06c7d8e9f0a"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_API = "app_runtime"
_AUTH = "auth_runtime"
_RELATION = "public.organizations"

_PREDECESSOR_SECURITY_COLUMN_ACL = {
    ("default_currency_code", "SELECT"),
    ("id", "SELECT"),
    ("slug", "SELECT"),
}
_PREDECESSOR_AUTH_RELATION_ACL = {"INSERT", "SELECT", "UPDATE"}
_FORWARD_AUTH_RELATION_ACL = {"INSERT", "SELECT"}

_PROFILE_SELECT_COLUMNS = (
    "name",
    "business_type",
    "tagline",
    "description",
    "year_established",
    "website_url",
    "social_links",
    "logo_status",
    "logo_thumb_key",
    "logo_medium_key",
    "logo_full_key",
    "cover_status",
    "cover_mobile_key",
    "cover_tablet_key",
    "cover_desktop_key",
)

_PROFILE_UPDATE_COLUMNS = (
    "name",
    "business_type",
    "tagline",
    "description",
    "year_established",
    "website_url",
    "social_links",
    "updated_at",
)

_AUTH_UPDATE_COLUMNS = (
    "phone",
    "address_line1",
    "address_line2",
    "city",
    "state",
    "pincode",
    "profile_completed",
    "tagline",
    "description",
    "year_established",
    "website_url",
    "social_links",
    "updated_at",
)


def _scalar(bind, sql: str, params: dict[str, object] | None = None):
    return bind.execute(sa.text(sql), params or {}).scalar_one()


def _require_identity(bind) -> None:
    migration = bind.execute(
        sa.text(
            """
            SELECT session_user::text AS session_name,
                   current_user::text AS current_name,
                   role_data.rolsuper,
                   role_data.rolinherit,
                   role_data.rolcreatedb,
                   role_data.rolcreaterole,
                   role_data.rolreplication,
                   role_data.rolbypassrls
            FROM pg_catalog.pg_roles AS role_data
            WHERE role_data.rolname = current_user
            """
        )
    ).mappings().one()
    if (
        migration["session_name"] != _MIGRATION_OWNER
        or migration["current_name"] != _MIGRATION_OWNER
    ):
        raise RuntimeError("P3A requires session_user=current_user=migration_owner")
    if any(
        bool(migration[key])
        for key in (
            "rolsuper",
            "rolinherit",
            "rolcreatedb",
            "rolcreaterole",
            "rolreplication",
            "rolbypassrls",
        )
    ):
        raise RuntimeError("migration_owner violates the reduced migration contract")

    rows = bind.execute(
        sa.text(
            """
            SELECT rolname, rolcanlogin, rolsuper, rolinherit, rolcreatedb,
                   rolcreaterole, rolreplication, rolbypassrls
            FROM pg_catalog.pg_roles
            WHERE rolname IN (:security_owner, :api_role, :auth_role)
            """
        ),
        {
            "security_owner": _SECURITY_OWNER,
            "api_role": _API,
            "auth_role": _AUTH,
        },
    ).mappings().all()
    roles = {str(row["rolname"]): row for row in rows}
    if set(roles) != {_SECURITY_OWNER, _API, _AUTH}:
        raise RuntimeError("P3A required managed roles are missing")
    for role_name, role in roles.items():
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
            raise RuntimeError(
                f"{role_name} violates the managed NOLOGIN/NOINHERIT/NOBYPASSRLS contract"
            )

    if not _scalar(
        bind,
        "SELECT pg_catalog.pg_has_role(session_user, :role_name, 'SET')",
        {"role_name": _SECURITY_OWNER},
    ):
        raise RuntimeError("migration_owner lacks bounded SET to app_security_owner")


def _direct_relation_acl(bind, role_name: str) -> set[str]:
    return {
        str(row[0])
        for row in bind.execute(
            sa.text(
                """
                SELECT acl_data.privilege_type::text
                FROM pg_catalog.pg_class AS relation_data
                CROSS JOIN LATERAL pg_catalog.aclexplode(
                    relation_data.relacl
                ) AS acl_data
                JOIN pg_catalog.pg_roles AS grantee_role
                  ON grantee_role.oid = acl_data.grantee
                WHERE relation_data.oid = 'public.organizations'::regclass
                  AND grantee_role.rolname = :role_name
                ORDER BY acl_data.privilege_type
                """
            ),
            {"role_name": role_name},
        ).all()
    }


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
                WHERE attribute_data.attrelid = 'public.organizations'::regclass
                  AND attribute_data.attnum > 0
                  AND NOT attribute_data.attisdropped
                  AND grantee_role.rolname = :role_name
                ORDER BY attribute_data.attname, acl_data.privilege_type
                """
            ),
            {"role_name": role_name},
        ).all()
    }


def _direct_schema_usage(bind, role_name: str) -> bool:
    return bool(
        _scalar(
            bind,
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_namespace AS namespace_data
                CROSS JOIN LATERAL pg_catalog.aclexplode(
                    namespace_data.nspacl
                ) AS acl_data
                JOIN pg_catalog.pg_roles AS grantee_role
                  ON grantee_role.oid = acl_data.grantee
                WHERE namespace_data.nspname = 'app_secure'
                  AND grantee_role.rolname = :role_name
                  AND acl_data.privilege_type = 'USAGE'
            )
            """,
            {"role_name": role_name},
        )
    )


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
                   pg_catalog.pg_get_function_identity_arguments(procedure_data.oid)::text AS identity_arguments,
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
    owner = _scalar(
        bind,
        """
        SELECT pg_catalog.pg_get_userbyid(relation_data.relowner)::text
        FROM pg_catalog.pg_class AS relation_data
        WHERE relation_data.oid = 'public.organizations'::regclass
        """,
    )
    if owner != _MIGRATION_OWNER:
        raise RuntimeError(f"unexpected organizations owner before P3A: {owner!r}")
    if not _direct_schema_usage(bind, _API):
        raise RuntimeError("app_runtime must retain historical app_secure USAGE")
    if _function_row(bind, "current_organization_profile", 0) is not None:
        raise RuntimeError("current_organization_profile already exists before P3A")
    if _function_row(bind, "update_current_organization_profile", 1) is not None:
        raise RuntimeError("update_current_organization_profile already exists before P3A")

    if _direct_relation_acl(bind, _SECURITY_OWNER):
        raise RuntimeError("app_security_owner unexpectedly has organizations table ACL")
    if _direct_column_acl(bind, _SECURITY_OWNER) != _PREDECESSOR_SECURITY_COLUMN_ACL:
        raise RuntimeError("app_security_owner predecessor column ACL drift")

    if _direct_relation_acl(bind, _AUTH) != _PREDECESSOR_AUTH_RELATION_ACL:
        raise RuntimeError("auth_runtime predecessor organizations table ACL drift")
    if _direct_column_acl(bind, _AUTH):
        raise RuntimeError("auth_runtime predecessor has unexpected direct column ACL")

    if _direct_relation_acl(bind, _API) or _direct_column_acl(bind, _API):
        raise RuntimeError("app_runtime unexpectedly has direct organizations ACL")


def _expected_security_column_acl() -> set[tuple[str, str]]:
    result = set(_PREDECESSOR_SECURITY_COLUMN_ACL)
    result.update((column, "SELECT") for column in _PROFILE_SELECT_COLUMNS)
    result.update((column, "UPDATE") for column in _PROFILE_UPDATE_COLUMNS)
    return result


def _require_function_contract(
    bind,
    *,
    name: str,
    nargs: int,
    volatility: str,
    required_tokens: tuple[str, ...],
) -> None:
    row = _function_row(bind, name, nargs)
    if row is None:
        raise RuntimeError(f"P3A function {name} is absent")
    if row["owner_name"] != _SECURITY_OWNER or not bool(row["prosecdef"]):
        raise RuntimeError(f"P3A function {name} owner/security-definer drift")
    if row["volatility"] != volatility:
        raise RuntimeError(f"P3A function {name} volatility drift")
    if set(row["proconfig"] or []) != {"search_path=pg_catalog", "row_security=on"}:
        raise RuntimeError(f"P3A function {name} session-setting drift")
    if not bool(row["api_execute"]) or bool(row["public_execute"]):
        raise RuntimeError(f"P3A function {name} EXECUTE ACL drift")
    source = " ".join(str(row["source"]).split()).lower()
    for token in required_tokens:
        if token.lower() not in source:
            raise RuntimeError(f"P3A function {name} source drift: missing {token}")


def _require_forward(bind) -> None:
    if _direct_relation_acl(bind, _SECURITY_OWNER):
        raise RuntimeError("app_security_owner gained organizations table ACL")
    if _direct_column_acl(bind, _SECURITY_OWNER) != _expected_security_column_acl():
        raise RuntimeError("app_security_owner organizations column ACL drift")

    if _direct_relation_acl(bind, _AUTH) != _FORWARD_AUTH_RELATION_ACL:
        raise RuntimeError("auth_runtime retained unexpected organizations table ACL")
    expected_auth_columns = {(column, "UPDATE") for column in _AUTH_UPDATE_COLUMNS}
    if _direct_column_acl(bind, _AUTH) != expected_auth_columns:
        raise RuntimeError("auth_runtime organizations column UPDATE ACL drift")

    if _direct_relation_acl(bind, _API) or _direct_column_acl(bind, _API):
        raise RuntimeError("app_runtime gained direct organizations ACL")

    common_tokens = (
        "app.current_org_id",
        "app.current_role",
        "app.current_gym_id",
        "owner",
        "admin",
        "public.organizations",
    )
    _require_function_contract(
        bind,
        name="current_organization_profile",
        nargs=0,
        volatility="s",
        required_tokens=common_tokens + ("organization.logo_status", "organization.cover_status"),
    )
    _require_function_contract(
        bind,
        name="update_current_organization_profile",
        nargs=1,
        volatility="v",
        required_tokens=common_tokens
        + (
            "p_patch",
            "unknown organization profile fields",
            "year_established",
            "pg_catalog.trunc",
            "pg_catalog.date_part",
            "updated_at",
        ),
    )


_CONTEXT_DECLARATIONS = """
    v_org_text text;
    v_org_id uuid;
    v_role text;
    v_gym text;
"""

_CONTEXT_GUARD = r"""
    v_org_text := pg_catalog.current_setting('app.current_org_id', true);
    IF v_org_text IS NULL OR pg_catalog.btrim(v_org_text) = '' THEN
        RAISE EXCEPTION 'organization profile tenant context is required'
            USING ERRCODE = '42501';
    END IF;
    BEGIN
        v_org_id := v_org_text::uuid;
    EXCEPTION
        WHEN invalid_text_representation THEN
            RAISE EXCEPTION 'organization profile tenant context is invalid'
                USING ERRCODE = '42501';
    END;

    v_role := pg_catalog.current_setting('app.current_role', true);
    v_gym := pg_catalog.current_setting('app.current_gym_id', true);
    IF v_role IS NULL
       OR pg_catalog.btrim(v_role) NOT IN ('owner', 'admin')
       OR (v_gym IS NOT NULL AND pg_catalog.btrim(v_gym) <> '') THEN
        RAISE EXCEPTION 'organization profile admin context is required'
            USING ERRCODE = '42501';
    END IF;
"""

_READ_FUNCTION_SQL = f"""
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
{_CONTEXT_DECLARATIONS}
BEGIN
{_CONTEXT_GUARD}
    RETURN QUERY
    SELECT
        organization.id,
        organization.name::text,
        organization.business_type::text,
        organization.tagline::text,
        organization.description::text,
        organization.year_established,
        organization.website_url::text,
        organization.social_links,
        organization.logo_status::text,
        organization.logo_thumb_key::text,
        organization.logo_medium_key::text,
        organization.logo_full_key::text,
        organization.cover_status::text,
        organization.cover_mobile_key::text,
        organization.cover_tablet_key::text,
        organization.cover_desktop_key::text
    FROM public.organizations AS organization
    WHERE organization.id = v_org_id;
END;
$function$
"""

_UPDATE_FUNCTION_SQL = f"""
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
{_CONTEXT_DECLARATIONS}
    v_unknown jsonb;
BEGIN
{_CONTEXT_GUARD}
    IF p_patch IS NULL OR pg_catalog.jsonb_typeof(p_patch) <> 'object' THEN
        RAISE EXCEPTION 'organization profile patch must be a JSON object'
            USING ERRCODE = '22023';
    END IF;

    v_unknown := p_patch - ARRAY[
        'name',
        'business_type',
        'tagline',
        'description',
        'year_established',
        'website_url',
        'social_links'
    ]::text[];
    IF v_unknown <> '{{}}'::jsonb THEN
        RAISE EXCEPTION 'unknown organization profile fields'
            USING ERRCODE = '42501';
    END IF;

    IF p_patch ? 'name' THEN
        IF p_patch->'name' = 'null'::jsonb
           OR pg_catalog.jsonb_typeof(p_patch->'name') <> 'string'
           OR pg_catalog.char_length(p_patch->>'name') < 2
           OR pg_catalog.char_length(p_patch->>'name') > 100 THEN
            RAISE EXCEPTION 'organization profile name is invalid'
                USING ERRCODE = '22023';
        END IF;
    END IF;
    IF p_patch ? 'business_type'
       AND p_patch->'business_type' <> 'null'::jsonb
       AND (
            pg_catalog.jsonb_typeof(p_patch->'business_type') <> 'string'
            OR pg_catalog.char_length(p_patch->>'business_type') > 50
       ) THEN
        RAISE EXCEPTION 'organization profile business_type is invalid'
            USING ERRCODE = '22023';
    END IF;
    IF p_patch ? 'tagline'
       AND p_patch->'tagline' <> 'null'::jsonb
       AND (
            pg_catalog.jsonb_typeof(p_patch->'tagline') <> 'string'
            OR pg_catalog.char_length(p_patch->>'tagline') > 150
       ) THEN
        RAISE EXCEPTION 'organization profile tagline is invalid'
            USING ERRCODE = '22023';
    END IF;
    IF p_patch ? 'description'
       AND p_patch->'description' <> 'null'::jsonb
       AND pg_catalog.jsonb_typeof(p_patch->'description') <> 'string' THEN
        RAISE EXCEPTION 'organization profile description is invalid'
            USING ERRCODE = '22023';
    END IF;
    IF p_patch ? 'website_url'
       AND p_patch->'website_url' <> 'null'::jsonb
       AND (
            pg_catalog.jsonb_typeof(p_patch->'website_url') <> 'string'
            OR pg_catalog.char_length(p_patch->>'website_url') > 255
       ) THEN
        RAISE EXCEPTION 'organization profile website_url is invalid'
            USING ERRCODE = '22023';
    END IF;
    IF p_patch ? 'year_established'
       AND p_patch->'year_established' <> 'null'::jsonb THEN
        IF pg_catalog.jsonb_typeof(p_patch->'year_established') <> 'number'
           OR (p_patch->>'year_established')::numeric
                <> pg_catalog.trunc((p_patch->>'year_established')::numeric)
           OR (p_patch->>'year_established')::numeric < 1800
           OR (p_patch->>'year_established')::numeric
                > pg_catalog.date_part('year', CURRENT_DATE) THEN
            RAISE EXCEPTION 'organization profile year_established is invalid'
                USING ERRCODE = '22023';
        END IF;
    END IF;
    IF p_patch ? 'social_links' THEN
        IF p_patch->'social_links' = 'null'::jsonb
           OR pg_catalog.jsonb_typeof(p_patch->'social_links') <> 'object' THEN
            RAISE EXCEPTION 'organization profile social_links must be an object'
                USING ERRCODE = '22023';
        END IF;
    END IF;

    IF p_patch = '{{}}'::jsonb THEN
        RETURN QUERY SELECT * FROM app_secure.current_organization_profile();
        RETURN;
    END IF;

    RETURN QUERY
    UPDATE public.organizations AS organization
    SET
        name = CASE WHEN p_patch ? 'name'
                    THEN p_patch->>'name' ELSE organization.name END,
        business_type = CASE WHEN p_patch ? 'business_type'
                    THEN p_patch->>'business_type' ELSE organization.business_type END,
        tagline = CASE WHEN p_patch ? 'tagline'
                    THEN p_patch->>'tagline' ELSE organization.tagline END,
        description = CASE WHEN p_patch ? 'description'
                    THEN p_patch->>'description' ELSE organization.description END,
        year_established = CASE WHEN p_patch ? 'year_established'
                    THEN (p_patch->>'year_established')::smallint
                    ELSE organization.year_established END,
        website_url = CASE WHEN p_patch ? 'website_url'
                    THEN p_patch->>'website_url' ELSE organization.website_url END,
        social_links = CASE WHEN p_patch ? 'social_links'
                    THEN p_patch->'social_links' ELSE organization.social_links END,
        updated_at = pg_catalog.clock_timestamp()
    WHERE organization.id = v_org_id
    RETURNING
        organization.id,
        organization.name::text,
        organization.business_type::text,
        organization.tagline::text,
        organization.description::text,
        organization.year_established,
        organization.website_url::text,
        organization.social_links,
        organization.logo_status::text,
        organization.logo_thumb_key::text,
        organization.logo_medium_key::text,
        organization.logo_full_key::text,
        organization.cover_status::text,
        organization.cover_mobile_key::text,
        organization.cover_tablet_key::text,
        organization.cover_desktop_key::text;
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
        + ", ".join(_PROFILE_SELECT_COLUMNS)
        + ") ON TABLE public.organizations TO app_security_owner"
    )
    op.execute(
        "GRANT UPDATE ("
        + ", ".join(_PROFILE_UPDATE_COLUMNS)
        + ") ON TABLE public.organizations TO app_security_owner"
    )

    _set_security_owner(bind)
    try:
        op.execute(_READ_FUNCTION_SQL)
        op.execute(_UPDATE_FUNCTION_SQL)
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

    op.execute("REVOKE UPDATE ON TABLE public.organizations FROM auth_runtime")
    op.execute(
        "GRANT UPDATE ("
        + ", ".join(_AUTH_UPDATE_COLUMNS)
        + ") ON TABLE public.organizations TO auth_runtime"
    )

    _require_forward(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    _require_forward(bind)

    op.execute(
        "REVOKE UPDATE ("
        + ", ".join(_AUTH_UPDATE_COLUMNS)
        + ") ON TABLE public.organizations FROM auth_runtime"
    )
    op.execute("GRANT UPDATE ON TABLE public.organizations TO auth_runtime")

    _set_security_owner(bind)
    try:
        op.execute(
            "DROP FUNCTION app_secure.update_current_organization_profile(jsonb) RESTRICT"
        )
        op.execute("DROP FUNCTION app_secure.current_organization_profile() RESTRICT")
    finally:
        _reset_role(bind)

    op.execute(
        "REVOKE UPDATE ("
        + ", ".join(_PROFILE_UPDATE_COLUMNS)
        + ") ON TABLE public.organizations FROM app_security_owner"
    )
    op.execute(
        "REVOKE SELECT ("
        + ", ".join(_PROFILE_SELECT_COLUMNS)
        + ") ON TABLE public.organizations FROM app_security_owner"
    )

    _require_predecessor(bind)
