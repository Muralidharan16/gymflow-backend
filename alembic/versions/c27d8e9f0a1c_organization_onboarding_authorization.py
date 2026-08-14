"""P3A: move onboarding organization mutation behind an auth-only capability.

Revision ID: c27d8e9f0a1c
Revises: c17d8e9f0a1b
Create Date: 2026-08-14

Column-scoping auth_runtime UPDATE is insufficient on the tenant-root
organizations table because it still permits cross-tenant mutation of those
columns. This revision removes direct organizations UPDATE from auth_runtime
and exposes one SECURITY DEFINER onboarding command bound to the canonical
current owner/organization request context.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "c27d8e9f0a1c"
down_revision = "c17d8e9f0a1b"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_AUTH = "auth_runtime"
_API = "app_runtime"
_FUNCTION = "app_secure.complete_current_organization_onboarding_profile(jsonb)"

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
_EXTRA_SECURITY_UPDATE_COLUMNS = (
    "phone",
    "address_line1",
    "address_line2",
    "city",
    "state",
    "pincode",
    "profile_completed",
)
_OWNER_SELECT_COLUMNS = ("id", "org_id", "onboarding_completed")
_PREDECESSOR_OWNER_SELECT_ACL = {
    ("email_verified", "SELECT", False, "migration_owner"),
    ("id", "SELECT", False, "migration_owner"),
    ("onboarding_completed", "SELECT", False, "migration_owner"),
    ("org_id", "SELECT", False, "migration_owner"),
}


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
        raise RuntimeError("P3A onboarding authorization requires migration_owner")
    if any(bool(value) for value in row[2:]):
        raise RuntimeError("migration_owner violates the reduced role contract")

    roles = bind.execute(
        sa.text(
            """
            SELECT rolname, rolcanlogin, rolsuper, rolinherit, rolcreatedb,
                   rolcreaterole, rolreplication, rolbypassrls
            FROM pg_catalog.pg_roles
            WHERE rolname IN (:security_owner, :auth_role, :api_role)
            """
        ),
        {
            "security_owner": _SECURITY_OWNER,
            "auth_role": _AUTH,
            "api_role": _API,
        },
    ).mappings().all()
    by_name = {str(item["rolname"]): item for item in roles}
    if set(by_name) != {_SECURITY_OWNER, _AUTH, _API}:
        raise RuntimeError("required P3A onboarding roles are missing")
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


def _direct_relation_acl(bind, relation: str, role_name: str) -> set[str]:
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
                WHERE relation_data.oid = pg_catalog.to_regclass(:relation)
                  AND grantee_role.rolname = :role_name
                ORDER BY acl_data.privilege_type
                """
            ),
            {"relation": relation, "role_name": role_name},
        ).all()
    }


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


def _direct_column_acl_detail(
    bind, relation: str, role_name: str
) -> set[tuple[str, str, bool, str]]:
    rows = bind.execute(
        sa.text(
            """
            SELECT attribute_data.attname::text,
                   acl_data.privilege_type::text,
                   acl_data.is_grantable,
                   grantor_role.rolname::text
            FROM pg_catalog.pg_attribute AS attribute_data
            CROSS JOIN LATERAL pg_catalog.aclexplode(
                attribute_data.attacl
            ) AS acl_data
            JOIN pg_catalog.pg_roles AS grantee_role
              ON grantee_role.oid = acl_data.grantee
            LEFT JOIN pg_catalog.pg_roles AS grantor_role
              ON grantor_role.oid = acl_data.grantor
            WHERE attribute_data.attrelid = pg_catalog.to_regclass(:relation)
              AND attribute_data.attnum > 0
              AND NOT attribute_data.attisdropped
              AND grantee_role.rolname = :role_name
            ORDER BY attribute_data.attname, acl_data.privilege_type,
                     acl_data.is_grantable, grantor_role.rolname
            """
        ),
        {"relation": relation, "role_name": role_name},
    ).all()
    if any(row[3] is None for row in rows):
        raise RuntimeError("direct column ACL contains an unresolved grantor")
    return {
        (str(row[0]), str(row[1]), bool(row[2]), str(row[3]))
        for row in rows
    }


def _schema_owner(bind) -> str:
    return str(
        _scalar(
            bind,
            """
            SELECT pg_catalog.pg_get_userbyid(namespace_data.nspowner)::text
            FROM pg_catalog.pg_namespace AS namespace_data
            WHERE namespace_data.nspname = 'app_secure'
            """,
        )
    )


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


def _function_row(bind):
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
                       WHERE grantee_role.rolname = :auth_role
                         AND acl_data.privilege_type = 'EXECUTE'
                   ) AS auth_execute,
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
              AND procedure_data.proname = 'complete_current_organization_onboarding_profile'
              AND procedure_data.pronargs = 1
              AND procedure_data.prokind = 'f'
            """
        ),
        {"auth_role": _AUTH, "api_role": _API},
    ).mappings().one_or_none()


def _require_predecessor(bind) -> None:
    if _function_row(bind) is not None:
        raise RuntimeError("P3A onboarding helper already exists before revision")
    if _direct_relation_acl(bind, "public.organizations", _AUTH) != {"INSERT", "SELECT"}:
        raise RuntimeError("auth_runtime organizations relation ACL drift before onboarding boundary")
    expected_auth_columns = {(column, "UPDATE") for column in _AUTH_UPDATE_COLUMNS}
    if _direct_column_acl(bind, "public.organizations", _AUTH) != expected_auth_columns:
        raise RuntimeError("auth_runtime predecessor organization column ACL drift")
    for column in _EXTRA_SECURITY_UPDATE_COLUMNS:
        if (column, "UPDATE") in _direct_column_acl(
            bind, "public.organizations", _SECURITY_OWNER
        ):
            raise RuntimeError(
                f"app_security_owner unexpectedly has UPDATE({column}) before onboarding boundary"
            )
    owner_acl = _direct_column_acl_detail(bind, "public.owners", _SECURITY_OWNER)
    if owner_acl != _PREDECESSOR_OWNER_SELECT_ACL:
        raise RuntimeError(
            "app_security_owner exact predecessor owners column ACL drift"
        )
    if _schema_owner(bind) != _SECURITY_OWNER:
        raise RuntimeError("app_secure schema owner drift before onboarding boundary")
    if _direct_schema_usage(bind, _AUTH):
        raise RuntimeError("auth_runtime unexpectedly has direct app_secure USAGE before onboarding boundary")


def _require_forward(bind) -> None:
    if _direct_relation_acl(bind, "public.organizations", _AUTH) != {"INSERT", "SELECT"}:
        raise RuntimeError("auth_runtime organizations relation ACL drift after onboarding boundary")
    if _direct_column_acl(bind, "public.organizations", _AUTH):
        raise RuntimeError("auth_runtime retained direct organizations column ACL after onboarding boundary")

    security_org_acl = _direct_column_acl(bind, "public.organizations", _SECURITY_OWNER)
    for column in _EXTRA_SECURITY_UPDATE_COLUMNS:
        if (column, "UPDATE") not in security_org_acl:
            raise RuntimeError(f"app_security_owner lacks onboarding UPDATE({column})")
    security_owner_acl = _direct_column_acl_detail(
        bind, "public.owners", _SECURITY_OWNER
    )
    if security_owner_acl != _PREDECESSOR_OWNER_SELECT_ACL:
        raise RuntimeError(
            "app_security_owner owners ACL changed across onboarding boundary"
        )
    for column in _OWNER_SELECT_COLUMNS:
        if (column, "SELECT", False, _MIGRATION_OWNER) not in security_owner_acl:
            raise RuntimeError(f"app_security_owner lacks owners SELECT({column})")

    if _schema_owner(bind) != _SECURITY_OWNER:
        raise RuntimeError("app_secure schema owner drift after onboarding boundary")
    if not _direct_schema_usage(bind, _AUTH):
        raise RuntimeError("auth_runtime lacks direct app_secure USAGE")

    row = _function_row(bind)
    if row is None:
        raise RuntimeError("P3A onboarding helper is missing")
    if row["owner_name"] != _SECURITY_OWNER or not bool(row["prosecdef"]):
        raise RuntimeError("P3A onboarding helper owner/security-definer drift")
    if row["volatility"] != "v":
        raise RuntimeError("P3A onboarding helper must remain VOLATILE")
    if set(row["proconfig"] or []) != {"search_path=pg_catalog", "row_security=on"}:
        raise RuntimeError("P3A onboarding helper session-setting drift")
    if not bool(row["auth_execute"]) or bool(row["api_execute"]) or bool(row["public_execute"]):
        raise RuntimeError("P3A onboarding helper EXECUTE ACL drift")
    source = " ".join(str(row["source"]).split()).lower()
    for token in (
        "app.current_org_id",
        "app.current_user_id",
        "app.current_principal_type",
        "app.current_role",
        "app.current_gym_id",
        "public.owners",
        "owner_data.id = v_user_id",
        "owner_data.org_id = v_org_id",
        "not owner_data.onboarding_completed",
        "public.organizations",
        "organization.id = v_org_id",
        "unknown organization onboarding fields",
    ):
        if token not in source:
            raise RuntimeError(f"P3A onboarding helper source drift: missing {token}")


_CREATE_FUNCTION = r"""
CREATE FUNCTION app_secure.complete_current_organization_onboarding_profile(p_patch jsonb)
RETURNS boolean
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog
SET row_security = on
AS $function$
DECLARE
    v_org_text text;
    v_user_text text;
    v_org_id uuid;
    v_user_id uuid;
    v_role text;
    v_principal_type text;
    v_gym text;
    v_unknown jsonb;
BEGIN
    v_org_text := pg_catalog.current_setting('app.current_org_id', true);
    v_user_text := pg_catalog.current_setting('app.current_user_id', true);
    v_role := pg_catalog.current_setting('app.current_role', true);
    v_principal_type := pg_catalog.current_setting('app.current_principal_type', true);
    v_gym := pg_catalog.current_setting('app.current_gym_id', true);

    IF v_org_text IS NULL OR pg_catalog.btrim(v_org_text) = ''
       OR v_user_text IS NULL OR pg_catalog.btrim(v_user_text) = '' THEN
        RAISE EXCEPTION 'organization onboarding owner/tenant context is required'
            USING ERRCODE = '42501';
    END IF;
    BEGIN
        v_org_id := v_org_text::uuid;
        v_user_id := v_user_text::uuid;
    EXCEPTION
        WHEN invalid_text_representation THEN
            RAISE EXCEPTION 'organization onboarding owner/tenant context is invalid'
                USING ERRCODE = '42501';
    END;

    IF v_role IS DISTINCT FROM 'owner'
       OR v_principal_type IS DISTINCT FROM 'owner'
       OR (v_gym IS NOT NULL AND pg_catalog.btrim(v_gym) <> '') THEN
        RAISE EXCEPTION 'organization onboarding owner context is required'
            USING ERRCODE = '42501';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM public.owners AS owner_data
        WHERE owner_data.id = v_user_id
          AND owner_data.org_id = v_org_id
          AND NOT owner_data.onboarding_completed
    ) THEN
        RAISE EXCEPTION 'organization onboarding owner/tenant pair is not authorized'
            USING ERRCODE = '42501';
    END IF;

    IF p_patch IS NULL OR pg_catalog.jsonb_typeof(p_patch) <> 'object' THEN
        RAISE EXCEPTION 'organization onboarding patch must be a JSON object'
            USING ERRCODE = '22023';
    END IF;
    v_unknown := p_patch - ARRAY[
        'phone', 'address_line1', 'address_line2', 'city', 'state', 'pincode',
        'tagline', 'description', 'year_established', 'website_url', 'social_links'
    ]::text[];
    IF v_unknown <> '{}'::jsonb THEN
        RAISE EXCEPTION 'unknown organization onboarding fields'
            USING ERRCODE = '42501';
    END IF;

    IF NOT (p_patch ? 'phone')
       OR NOT (p_patch ? 'address_line1')
       OR NOT (p_patch ? 'address_line2')
       OR NOT (p_patch ? 'city')
       OR NOT (p_patch ? 'state')
       OR NOT (p_patch ? 'pincode') THEN
        RAISE EXCEPTION 'organization onboarding required profile fields are missing'
            USING ERRCODE = '22023';
    END IF;

    IF p_patch->'phone' = 'null'::jsonb
       OR p_patch->'address_line1' = 'null'::jsonb
       OR p_patch->'city' = 'null'::jsonb
       OR p_patch->'state' = 'null'::jsonb
       OR p_patch->'pincode' = 'null'::jsonb THEN
        RAISE EXCEPTION 'organization onboarding required profile fields cannot be null'
            USING ERRCODE = '22023';
    END IF;
    IF pg_catalog.jsonb_typeof(p_patch->'phone') <> 'string'
       OR pg_catalog.jsonb_typeof(p_patch->'address_line1') <> 'string'
       OR pg_catalog.jsonb_typeof(p_patch->'city') <> 'string'
       OR pg_catalog.jsonb_typeof(p_patch->'state') <> 'string'
       OR pg_catalog.jsonb_typeof(p_patch->'pincode') <> 'string' THEN
        RAISE EXCEPTION 'organization onboarding required profile field type is invalid'
            USING ERRCODE = '22023';
    END IF;
    IF p_patch->'address_line2' <> 'null'::jsonb
       AND pg_catalog.jsonb_typeof(p_patch->'address_line2') <> 'string' THEN
        RAISE EXCEPTION 'organization onboarding address_line2 is invalid'
            USING ERRCODE = '22023';
    END IF;
    IF p_patch ? 'social_links' AND (
        p_patch->'social_links' = 'null'::jsonb
        OR pg_catalog.jsonb_typeof(p_patch->'social_links') <> 'object'
    ) THEN
        RAISE EXCEPTION 'organization onboarding social_links must be an object'
            USING ERRCODE = '22023';
    END IF;
    IF p_patch ? 'year_established'
       AND p_patch->'year_established' <> 'null'::jsonb
       AND pg_catalog.jsonb_typeof(p_patch->'year_established') <> 'number' THEN
        RAISE EXCEPTION 'organization onboarding year_established is invalid'
            USING ERRCODE = '22023';
    END IF;

    UPDATE public.organizations AS organization
    SET phone = p_patch->>'phone',
        address_line1 = p_patch->>'address_line1',
        address_line2 = p_patch->>'address_line2',
        city = p_patch->>'city',
        state = p_patch->>'state',
        pincode = p_patch->>'pincode',
        profile_completed = TRUE,
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
    WHERE organization.id = v_org_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'organization onboarding tenant does not exist'
            USING ERRCODE = '42501';
    END IF;
    RETURN TRUE;
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
        "GRANT UPDATE ("
        + ", ".join(_EXTRA_SECURITY_UPDATE_COLUMNS)
        + ") ON TABLE public.organizations TO app_security_owner"
    )
    _set_security_owner(bind)
    try:
        op.execute("GRANT USAGE ON SCHEMA app_secure TO auth_runtime")
        op.execute(_CREATE_FUNCTION)
        op.execute(
            "REVOKE ALL ON FUNCTION "
            "app_secure.complete_current_organization_onboarding_profile(jsonb) FROM PUBLIC"
        )
        op.execute(
            "GRANT EXECUTE ON FUNCTION "
            "app_secure.complete_current_organization_onboarding_profile(jsonb) TO auth_runtime"
        )
    finally:
        _reset_role(bind)

    op.execute(
        "REVOKE UPDATE ("
        + ", ".join(_AUTH_UPDATE_COLUMNS)
        + ") ON TABLE public.organizations FROM auth_runtime"
    )

    _require_forward(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    _require_forward(bind)

    op.execute(
        "GRANT UPDATE ("
        + ", ".join(_AUTH_UPDATE_COLUMNS)
        + ") ON TABLE public.organizations TO auth_runtime"
    )

    _set_security_owner(bind)
    try:
        op.execute(
            "DROP FUNCTION "
            "app_secure.complete_current_organization_onboarding_profile(jsonb) RESTRICT"
        )
        op.execute("REVOKE USAGE ON SCHEMA app_secure FROM auth_runtime")
    finally:
        _reset_role(bind)

    op.execute(
        "REVOKE UPDATE ("
        + ", ".join(_EXTRA_SECURITY_UPDATE_COLUMNS)
        + ") ON TABLE public.organizations FROM app_security_owner"
    )

    _require_predecessor(bind)
