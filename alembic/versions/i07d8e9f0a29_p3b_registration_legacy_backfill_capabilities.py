"""P3B: add bounded capabilities for legacy registration re-encryption.

Revision ID: i07d8e9f0a29
Revises: h07d8e9f0a28
Create Date: 2026-08-15

These capabilities exist only for the expand/migrate window.  A bound owner or
organization admin can enumerate legacy ciphertext for its own organization and
atomically convert one unchanged identifier into the KMS envelope format.
Conversion preserves verification because the identifier value is unchanged.
The final P3B contract revokes runtime execution and the two temporary legacy
read columns after proving no crypto_version=0 rows remain.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "i07d8e9f0a29"
down_revision = "h07d8e9f0a28"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_API = "app_runtime"
_REGISTRATION = "public.organization_registrations"
_KEY_SCOPE = "organization_registrations"
_LIST_FUNCTION = "current_legacy_registration_backfill_rows"
_CONVERT_FUNCTION = "convert_legacy_organization_registration_envelope"
_PREDECESSOR_SELECT = {
    "id",
    "org_id",
    "id_type",
    "id_number_masked",
    "country_code",
    "is_verified",
    "verified_at",
}
_FORWARD_SELECT = _PREDECESSOR_SELECT | {"id_number_encrypted", "crypto_version"}


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
        raise RuntimeError("P3B legacy backfill migration requires migration_owner")
    if any(bool(value) for value in row[2:]):
        raise RuntimeError("migration_owner violates the reduced role contract")
    if not _scalar(
        bind,
        "SELECT pg_catalog.pg_has_role(session_user, :role_name, 'SET')",
        {"role_name": _SECURITY_OWNER},
    ):
        raise RuntimeError("migration_owner lacks bounded SET to app_security_owner")


def _select_columns(bind, role_name: str) -> set[str]:
    return {
        str(row[0])
        for row in bind.execute(
            sa.text(
                """
                SELECT attribute_data.attname::text
                FROM pg_catalog.pg_attribute AS attribute_data
                CROSS JOIN LATERAL pg_catalog.aclexplode(attribute_data.attacl) AS acl_data
                JOIN pg_catalog.pg_roles AS grantee_role
                  ON grantee_role.oid = acl_data.grantee
                WHERE attribute_data.attrelid = pg_catalog.to_regclass(:relation)
                  AND attribute_data.attnum > 0
                  AND NOT attribute_data.attisdropped
                  AND grantee_role.rolname = :role_name
                  AND acl_data.privilege_type = 'SELECT'
                ORDER BY attribute_data.attname
                """
            ),
            {"relation": _REGISTRATION, "role_name": role_name},
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


_LIST_SQL = f"""
CREATE FUNCTION app_secure.current_legacy_registration_backfill_rows()
RETURNS TABLE (
    id uuid,
    id_type text,
    id_number_encrypted text,
    id_number_masked text,
    country_code text
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
{_principal_guard_sql('registration legacy backfill read')}
    RETURN QUERY
    SELECT registration.id,
           registration.id_type::text,
           registration.id_number_encrypted::text,
           registration.id_number_masked::text,
           registration.country_code::text
      FROM public.organization_registrations AS registration
     WHERE registration.org_id = v_org_id
       AND registration.crypto_version = 0
       AND registration.id_number_encrypted IS NOT NULL
     ORDER BY registration.id;
END;
$function$
"""

_CONVERT_SQL = f"""
CREATE FUNCTION app_secure.convert_legacy_organization_registration_envelope(
    p_registration_id uuid,
    p_payload_encrypted bytea,
    p_key_version integer
)
RETURNS TABLE (
    id uuid,
    id_type text,
    id_number_masked text,
    country_code text,
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
    v_active_key boolean;
    v_id_type text;
    v_mask text;
    v_country text;
    v_verified boolean;
    v_verified_at timestamp with time zone;
BEGIN
{_principal_guard_sql('registration legacy backfill conversion')}
    IF p_registration_id IS NULL
       OR p_payload_encrypted IS NULL
       OR pg_catalog.octet_length(p_payload_encrypted) < 32
       OR p_key_version IS NULL
       OR p_key_version < 1 THEN
        RAISE EXCEPTION 'legacy registration envelope input is invalid'
            USING ERRCODE = '22023';
    END IF;
    IF (
        pg_catalog.get_byte(p_payload_encrypted, 0)::bigint * 16777216
        + pg_catalog.get_byte(p_payload_encrypted, 1)::bigint * 65536
        + pg_catalog.get_byte(p_payload_encrypted, 2)::bigint * 256
        + pg_catalog.get_byte(p_payload_encrypted, 3)::bigint
    ) <> p_key_version::bigint THEN
        RAISE EXCEPTION 'legacy registration envelope key version mismatch'
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
        RAISE EXCEPTION 'legacy registration ACTIVE data key is required'
            USING ERRCODE = '23503';
    END IF;

    UPDATE public.organization_registrations AS registration
       SET id_number_encrypted = NULL,
           crypto_version = 1,
           updated_at = pg_catalog.clock_timestamp()
     WHERE registration.id = p_registration_id
       AND registration.org_id = v_org_id
       AND registration.crypto_version = 0
       AND registration.id_number_encrypted IS NOT NULL
     RETURNING registration.id_type::text,
               registration.id_number_masked::text,
               registration.country_code::text,
               registration.is_verified,
               registration.verified_at
          INTO v_id_type, v_mask, v_country, v_verified, v_verified_at;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'legacy registration conversion target does not exist'
            USING ERRCODE = 'P0002';
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
    );

    RETURN QUERY SELECT
        p_registration_id,
        v_id_type,
        v_mask,
        v_country,
        v_verified,
        v_verified_at;
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
        bind.execute(sa.text(_LIST_SQL))
        bind.execute(sa.text(_CONVERT_SQL))
        bind.execute(sa.text(
            "REVOKE ALL ON FUNCTION "
            "app_secure.current_legacy_registration_backfill_rows() FROM PUBLIC"
        ))
        bind.execute(sa.text(
            "REVOKE ALL ON FUNCTION "
            "app_secure.convert_legacy_organization_registration_envelope("
            "uuid,bytea,integer) FROM PUBLIC"
        ))
        bind.execute(sa.text(
            "GRANT EXECUTE ON FUNCTION "
            "app_secure.current_legacy_registration_backfill_rows() TO app_runtime"
        ))
        bind.execute(sa.text(
            "GRANT EXECUTE ON FUNCTION "
            "app_secure.convert_legacy_organization_registration_envelope("
            "uuid,bytea,integer) TO app_runtime"
        ))
    finally:
        bind.execute(sa.text("RESET ROLE"))
    if not had_create:
        bind.execute(sa.text("REVOKE CREATE ON SCHEMA app_secure FROM app_security_owner"))


def _require_predecessor(bind) -> None:
    if _select_columns(bind, _SECURITY_OWNER) != _PREDECESSOR_SELECT:
        raise RuntimeError("P3B legacy backfill predecessor registration SELECT drift")
    for name, nargs in ((_LIST_FUNCTION, 0), (_CONVERT_FUNCTION, 3)):
        if _function_row(bind, name, nargs) is not None:
            raise RuntimeError(f"unexpected pre-existing legacy backfill function {name}")


def _require_forward(bind) -> None:
    if _select_columns(bind, _SECURITY_OWNER) != _FORWARD_SELECT:
        raise RuntimeError("P3B legacy backfill registration SELECT drift")
    for name, nargs, volatility in (
        (_LIST_FUNCTION, 0, "s"),
        (_CONVERT_FUNCTION, 3, "v"),
    ):
        row = _function_row(bind, name, nargs)
        if row is None:
            raise RuntimeError(f"P3B legacy backfill function {name} is missing")
        if row["owner_name"] != _SECURITY_OWNER or not bool(row["prosecdef"]):
            raise RuntimeError(f"P3B legacy backfill function {name} owner/security drift")
        if row["volatility"] != volatility:
            raise RuntimeError(f"P3B legacy backfill function {name} volatility drift")
        if set(row["proconfig"] or []) != {"search_path=pg_catalog", "row_security=on"}:
            raise RuntimeError(f"P3B legacy backfill function {name} setting drift")
        if not bool(row["api_execute"]) or bool(row["public_execute"]):
            raise RuntimeError(f"P3B legacy backfill function {name} EXECUTE ACL drift")
        source = " ".join(str(row["source"]).split()).lower()
        for token in (
            "app.current_org_id",
            "app.current_user_id",
            "app.current_principal_type",
            "app.current_role",
            "public.organization_registrations",
        ):
            if token not in source:
                raise RuntimeError(f"P3B legacy backfill function {name} lost token {token}")

    convert_source = " ".join(
        str(_function_row(bind, _CONVERT_FUNCTION, 3)["source"]).split()
    ).lower()
    for token in (
        "crypto_version = 0",
        "id_number_encrypted = null",
        "crypto_version = 1",
        "public.organization_registration_payloads_secure",
        "public.encryption_key_registry",
        "key_status = 'active'",
    ):
        if token not in convert_source:
            raise RuntimeError(f"P3B legacy conversion lost token {token}")
    for forbidden in ("is_verified =", "verified_at ="):
        if forbidden in convert_source:
            raise RuntimeError("P3B legacy conversion must preserve verification metadata")


def upgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    _require_predecessor(bind)
    bind.execute(sa.text(
        "GRANT SELECT (id_number_encrypted, crypto_version) "
        "ON TABLE public.organization_registrations TO app_security_owner"
    ))
    _install_functions(bind)
    _require_forward(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    _require_forward(bind)
    bind.execute(sa.text("SET LOCAL ROLE app_security_owner"))
    try:
        bind.execute(sa.text(
            "DROP FUNCTION app_secure.convert_legacy_organization_registration_envelope("
            "uuid,bytea,integer)"
        ))
        bind.execute(sa.text(
            "DROP FUNCTION app_secure.current_legacy_registration_backfill_rows()"
        ))
    finally:
        bind.execute(sa.text("RESET ROLE"))
    bind.execute(sa.text(
        "REVOKE SELECT (id_number_encrypted, crypto_version) "
        "ON TABLE public.organization_registrations FROM app_security_owner"
    ))
    _require_predecessor(bind)
