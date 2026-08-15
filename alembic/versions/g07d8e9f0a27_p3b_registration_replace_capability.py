"""P3B: add atomic registration envelope replacement capability.

Revision ID: g07d8e9f0a27
Revises: f07d8e9f0a26
Create Date: 2026-08-15

Existing registrations are replaced only by their tenant-bound registration id.
The API runtime receives EXECUTE only.  The security owner receives exact
column UPDATE privileges on masked metadata and the FORCE-RLS payload relation.
Changing identifier material invalidates any previous verification state.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "g07d8e9f0a27"
down_revision = "f07d8e9f0a26"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_API = "app_runtime"
_REGISTRATION = "public.organization_registrations"
_PAYLOAD = "public.organization_registration_payloads_secure"
_KEY_SCOPE = "organization_registrations"
_FUNCTION_NAME = "replace_organization_registration_envelope"
_REGISTRATION_UPDATE_COLUMNS = {
    "id_number_encrypted",
    "id_number_masked",
    "entity_type",
    "crypto_version",
    "is_verified",
    "verified_at",
    "updated_at",
}
_PAYLOAD_UPDATE_COLUMNS = {
    "payload_encrypted",
    "key_version",
    "key_scope",
    "schema_version",
    "updated_at",
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
        raise RuntimeError("P3B registration replace migration requires migration_owner")
    if any(bool(value) for value in current[2:]):
        raise RuntimeError("migration_owner violates the reduced role contract")
    if not _scalar(
        bind,
        "SELECT pg_catalog.pg_has_role(session_user, :role_name, 'SET')",
        {"role_name": _SECURITY_OWNER},
    ):
        raise RuntimeError("migration_owner lacks bounded SET to app_security_owner")


def _column_acl(bind, relation: str, role_name: str) -> set[tuple[str, str]]:
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
            {"relation": relation, "role_name": role_name},
        ).all()
    }


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
              AND procedure_data.proname = :function_name
              AND procedure_data.pronargs = 7
              AND procedure_data.prokind = 'f'
            """
        ),
        {"function_name": _FUNCTION_NAME, "api_role": _API},
    ).mappings().one_or_none()


def _principal_guard_sql() -> str:
    return """
    v_org_text := pg_catalog.current_setting('app.current_org_id', true);
    v_user_text := pg_catalog.current_setting('app.current_user_id', true);
    v_principal_type := pg_catalog.current_setting('app.current_principal_type', true);
    v_role := pg_catalog.current_setting('app.current_role', true);
    v_gym := pg_catalog.current_setting('app.current_gym_id', true);

    IF v_org_text IS NULL OR pg_catalog.btrim(v_org_text) = ''
       OR v_user_text IS NULL OR pg_catalog.btrim(v_user_text) = ''
       OR v_principal_type IS NULL OR pg_catalog.btrim(v_principal_type) = '' THEN
        RAISE EXCEPTION 'registration replace principal organization membership is required'
            USING ERRCODE = '42501';
    END IF;
    BEGIN
        v_org_id := v_org_text::uuid;
        v_user_id := v_user_text::uuid;
    EXCEPTION
        WHEN invalid_text_representation THEN
            RAISE EXCEPTION 'registration replace principal organization membership is invalid'
                USING ERRCODE = '42501';
    END;

    IF v_role IS NULL
       OR pg_catalog.btrim(v_role) NOT IN ('owner', 'admin')
       OR (v_gym IS NOT NULL AND pg_catalog.btrim(v_gym) <> '') THEN
        RAISE EXCEPTION 'registration replace admin context is required'
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
        RAISE EXCEPTION 'registration replace principal organization membership is required'
            USING ERRCODE = '42501';
    END IF;
"""


_REPLACE_FUNCTION = f"""
CREATE FUNCTION app_secure.replace_organization_registration_envelope(
    p_registration_id uuid,
    p_id_type text,
    p_id_number_masked text,
    p_country_code text,
    p_entity_type text,
    p_payload_encrypted bytea,
    p_key_version integer
)
RETURNS TABLE (
    id uuid,
    id_type text,
    id_number_masked text,
    country_code text,
    entity_type text,
    is_verified boolean,
    verified_at timestamp with time zone
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
    v_role text;
    v_gym text;
    v_org_id uuid;
    v_user_id uuid;
    v_authorized boolean := FALSE;
    v_id_type text;
    v_country_code text;
    v_entity_type text;
    v_mask text;
    v_active_key boolean;
BEGIN
{_principal_guard_sql()}
    IF p_registration_id IS NULL THEN
        RAISE EXCEPTION 'registration id is required' USING ERRCODE = '22023';
    END IF;

    v_id_type := pg_catalog.upper(pg_catalog.btrim(p_id_type));
    IF p_id_type IS NULL OR p_id_type <> v_id_type
       OR pg_catalog.length(v_id_type) < 1
       OR pg_catalog.length(v_id_type) > 20 THEN
        RAISE EXCEPTION 'registration id type is not canonical'
            USING ERRCODE = '22023';
    END IF;

    v_country_code := pg_catalog.upper(pg_catalog.btrim(p_country_code));
    IF p_country_code IS NULL OR p_country_code <> v_country_code
       OR pg_catalog.length(v_country_code) <> 2 THEN
        RAISE EXCEPTION 'registration country code is not canonical'
            USING ERRCODE = '22023';
    END IF;

    v_mask := pg_catalog.btrim(p_id_number_masked);
    IF p_id_number_masked IS NULL OR p_id_number_masked <> v_mask
       OR pg_catalog.length(v_mask) < 1
       OR pg_catalog.length(v_mask) > 50 THEN
        RAISE EXCEPTION 'registration masked identifier is invalid'
            USING ERRCODE = '22023';
    END IF;

    IF p_entity_type IS NULL THEN
        v_entity_type := NULL;
    ELSE
        v_entity_type := pg_catalog.upper(pg_catalog.btrim(p_entity_type));
        IF p_entity_type <> v_entity_type OR pg_catalog.length(v_entity_type) <> 1 THEN
            RAISE EXCEPTION 'registration entity type is not canonical'
                USING ERRCODE = '22023';
        END IF;
    END IF;

    IF p_payload_encrypted IS NULL
       OR pg_catalog.octet_length(p_payload_encrypted) < 32
       OR p_key_version IS NULL
       OR p_key_version < 1 THEN
        RAISE EXCEPTION 'registration encrypted payload is invalid'
            USING ERRCODE = '22023';
    END IF;
    IF (
        pg_catalog.get_byte(p_payload_encrypted, 0)::bigint * 16777216
        + pg_catalog.get_byte(p_payload_encrypted, 1)::bigint * 65536
        + pg_catalog.get_byte(p_payload_encrypted, 2)::bigint * 256
        + pg_catalog.get_byte(p_payload_encrypted, 3)::bigint
    ) <> p_key_version::bigint THEN
        RAISE EXCEPTION 'registration encrypted payload key version mismatch'
            USING ERRCODE = '22023';
    END IF;

    SELECT EXISTS (
        SELECT 1
        FROM public.encryption_key_registry AS key_data
        WHERE key_data.tenant_id = v_org_id
          AND key_data.table_name = '{_KEY_SCOPE}'
          AND key_data.key_version = p_key_version
          AND key_data.key_status = 'ACTIVE'
    ) INTO v_active_key;
    IF NOT v_active_key THEN
        RAISE EXCEPTION 'registration ACTIVE data key is required'
            USING ERRCODE = '23503';
    END IF;

    UPDATE public.organization_registrations AS registration
       SET id_number_encrypted = NULL,
           id_number_masked = v_mask,
           entity_type = v_entity_type,
           crypto_version = 1,
           is_verified = FALSE,
           verified_at = NULL,
           updated_at = pg_catalog.clock_timestamp()
     WHERE registration.id = p_registration_id
       AND registration.org_id = v_org_id
       AND registration.id_type = v_id_type
       AND registration.country_code = v_country_code;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'registration replacement target does not exist'
            USING ERRCODE = '23503';
    END IF;

    INSERT INTO public.organization_registration_payloads_secure (
        registration_id,
        tenant_id,
        payload_encrypted,
        key_version,
        key_scope,
        schema_version
    ) VALUES (
        p_registration_id,
        v_org_id,
        p_payload_encrypted,
        p_key_version,
        '{_KEY_SCOPE}',
        1
    )
    ON CONFLICT (registration_id) DO UPDATE
       SET payload_encrypted = EXCLUDED.payload_encrypted,
           key_version = EXCLUDED.key_version,
           key_scope = EXCLUDED.key_scope,
           schema_version = EXCLUDED.schema_version,
           updated_at = pg_catalog.clock_timestamp();

    RETURN QUERY SELECT
        p_registration_id,
        v_id_type,
        v_mask,
        v_country_code,
        v_entity_type,
        FALSE,
        NULL::timestamp with time zone;
END;
$function$
"""


def _require_predecessor(bind) -> None:
    if _function_row(bind) is not None:
        raise RuntimeError("P3B registration replace capability already exists")
    registration_updates = {
        column for column, privilege in _column_acl(bind, _REGISTRATION, _SECURITY_OWNER)
        if privilege == "UPDATE"
    }
    payload_updates = {
        column for column, privilege in _column_acl(bind, _PAYLOAD, _SECURITY_OWNER)
        if privilege == "UPDATE"
    }
    if registration_updates & _REGISTRATION_UPDATE_COLUMNS:
        raise RuntimeError("unexpected predecessor registration UPDATE column ACL")
    if payload_updates & _PAYLOAD_UPDATE_COLUMNS:
        raise RuntimeError("unexpected predecessor payload UPDATE column ACL")


def _require_forward(bind) -> None:
    registration_updates = {
        column for column, privilege in _column_acl(bind, _REGISTRATION, _SECURITY_OWNER)
        if privilege == "UPDATE"
    }
    payload_updates = {
        column for column, privilege in _column_acl(bind, _PAYLOAD, _SECURITY_OWNER)
        if privilege == "UPDATE"
    }
    if registration_updates != _REGISTRATION_UPDATE_COLUMNS:
        raise RuntimeError("P3B registration replace UPDATE column ACL drift")
    if payload_updates != _PAYLOAD_UPDATE_COLUMNS:
        raise RuntimeError("P3B secure payload UPDATE column ACL drift")

    row = _function_row(bind)
    if row is None:
        raise RuntimeError("P3B registration replace capability is missing")
    if row["owner_name"] != _SECURITY_OWNER or not bool(row["prosecdef"]):
        raise RuntimeError("P3B registration replace capability owner/security drift")
    if row["volatility"] != "v":
        raise RuntimeError("P3B registration replace capability volatility drift")
    if set(row["proconfig"] or []) != {"search_path=pg_catalog", "row_security=on"}:
        raise RuntimeError("P3B registration replace capability setting drift")
    if not bool(row["api_execute"]) or bool(row["public_execute"]):
        raise RuntimeError("P3B registration replace capability EXECUTE ACL drift")
    source = " ".join(str(row["source"]).split()).lower()
    for token in (
        "app.current_org_id",
        "app.current_user_id",
        "app.current_principal_type",
        "app.current_role",
        "public.organization_registrations",
        "public.organization_registration_payloads_secure",
        "public.encryption_key_registry",
        "key_status = 'active'",
        "id_number_encrypted = null",
        "is_verified = false",
        "verified_at = null",
        "on conflict (registration_id) do update",
        _KEY_SCOPE,
    ):
        if token not in source:
            raise RuntimeError(f"P3B registration replace capability lost token {token}")


def _install_function(bind) -> None:
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
        bind.execute(sa.text(_REPLACE_FUNCTION))
        bind.execute(sa.text(
            "REVOKE ALL ON FUNCTION "
            "app_secure.replace_organization_registration_envelope("
            "uuid,text,text,text,text,bytea,integer) FROM PUBLIC"
        ))
        bind.execute(sa.text(
            "GRANT EXECUTE ON FUNCTION "
            "app_secure.replace_organization_registration_envelope("
            "uuid,text,text,text,text,bytea,integer) TO app_runtime"
        ))
    finally:
        bind.execute(sa.text("RESET ROLE"))
    if not had_create:
        bind.execute(sa.text("REVOKE CREATE ON SCHEMA app_secure FROM app_security_owner"))


def upgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    _require_predecessor(bind)

    registration_columns = ", ".join(sorted(_REGISTRATION_UPDATE_COLUMNS))
    payload_columns = ", ".join(sorted(_PAYLOAD_UPDATE_COLUMNS))
    bind.execute(sa.text(
        f"GRANT UPDATE ({registration_columns}) ON TABLE {_REGISTRATION} "
        "TO app_security_owner"
    ))
    bind.execute(sa.text(
        f"GRANT UPDATE ({payload_columns}) ON TABLE {_PAYLOAD} "
        "TO app_security_owner"
    ))
    _install_function(bind)
    _require_forward(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    _require_forward(bind)

    bind.execute(sa.text("SET LOCAL ROLE app_security_owner"))
    try:
        bind.execute(sa.text(
            "DROP FUNCTION app_secure.replace_organization_registration_envelope("
            "uuid,text,text,text,text,bytea,integer)"
        ))
    finally:
        bind.execute(sa.text("RESET ROLE"))

    registration_columns = ", ".join(sorted(_REGISTRATION_UPDATE_COLUMNS))
    payload_columns = ", ".join(sorted(_PAYLOAD_UPDATE_COLUMNS))
    bind.execute(sa.text(
        f"REVOKE UPDATE ({payload_columns}) ON TABLE {_PAYLOAD} "
        "FROM app_security_owner"
    ))
    bind.execute(sa.text(
        f"REVOKE UPDATE ({registration_columns}) ON TABLE {_REGISTRATION} "
        "FROM app_security_owner"
    ))
    _require_predecessor(bind)
