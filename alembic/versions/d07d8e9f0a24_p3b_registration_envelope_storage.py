"""P3B: expand organization registrations for KMS-backed envelope storage.

Revision ID: d07d8e9f0a24
Revises: c97d8e9f0a23
Create Date: 2026-08-15

The legacy registration table remains directly mutable during the P3B expand
window, so new ciphertext is deliberately stored in a separate FORCE-RLS
relation with no runtime ACL. The metadata row records only the masked
identifier and crypto version.

FORCE RLS also applies to migration_owner. This revision therefore never
weakens RLS to inspect tenant data during downgrade. A migration-owner-only,
non-secret marker relation is maintained transactionally by a trigger on the
secure payload table. Downgrade uses that marker plus PostgreSQL constraint
validation to fail closed whenever predecessor representation would lose data.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "d07d8e9f0a24"
down_revision = "c97d8e9f0a23"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_REGISTRATION = "public.organization_registrations"
_KEY_REGISTRY = "public.encryption_key_registry"
_KEY_SCOPE = "organization_registrations"
_PAYLOAD = "public.organization_registration_payloads_secure"
_MARKER = "public.p3b_registration_envelope_rows"
_PAYLOAD_POLICY = "p3b_tenant_isolation_registration_payloads_secure"
_MARKER_FUNCTION_NAME = "track_registration_envelope_row"
_MARKER_TRIGGER = "trg_p3b_track_registration_envelope_row"
_C97_FUNCTION_NAMES = (
    "current_organization_registrations",
    "current_organization_has_registration",
)
_UQ_BUSINESS_KEY = "uq_org_reg_org_country_type"
_UQ_ID_TENANT = "uq_org_reg_id_org"
_UQ_KEY_SCOPE = "uq_key_registry_version_tenant_table"
_CK_CRYPTO = "ck_org_reg_crypto_material"
_CK_CANONICAL = "ck_org_reg_canonical_identity"
_CK_PAYLOAD_SCOPE = "ck_org_reg_payload_key_scope"
_CK_PAYLOAD_ENVELOPE = "ck_org_reg_payload_envelope_key_version"
_FK_PAYLOAD_KEY_SCOPE = "fk_org_reg_payload_key_scope"


def _scalar(bind, sql: str, params: dict[str, object] | None = None):
    return bind.execute(sa.text(sql), params or {}).scalar_one()


def _require_identity(bind) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT session_user::text,
                   current_user::text,
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
    ).one()
    if row[0] != _MIGRATION_OWNER or row[1] != _MIGRATION_OWNER:
        raise RuntimeError("P3B registration envelope storage requires migration_owner")
    if any(bool(value) for value in row[2:]):
        raise RuntimeError("migration_owner violates the reduced role contract")

    security_owner = bind.execute(
        sa.text(
            """
            SELECT rolcanlogin, rolsuper, rolinherit, rolcreatedb,
                   rolcreaterole, rolreplication, rolbypassrls
            FROM pg_catalog.pg_roles
            WHERE rolname = :role_name
            """
        ),
        {"role_name": _SECURITY_OWNER},
    ).one_or_none()
    if security_owner is None or any(bool(value) for value in security_owner):
        raise RuntimeError("app_security_owner violates the reduced role contract")
    if not _scalar(
        bind,
        "SELECT pg_catalog.pg_has_role(session_user, :role_name, 'SET')",
        {"role_name": _SECURITY_OWNER},
    ):
        raise RuntimeError("migration_owner lacks bounded SET to app_security_owner")


def _relation_state(bind, relation: str):
    return bind.execute(
        sa.text(
            """
            SELECT pg_catalog.pg_get_userbyid(relation_data.relowner)::text AS owner_name,
                   relation_data.relrowsecurity,
                   relation_data.relforcerowsecurity
            FROM pg_catalog.pg_class AS relation_data
            WHERE relation_data.oid = pg_catalog.to_regclass(:relation)
            """
        ),
        {"relation": relation},
    ).mappings().one_or_none()


def _column(bind, relation: str, column: str):
    return bind.execute(
        sa.text(
            """
            SELECT pg_catalog.format_type(
                       attribute_data.atttypid,
                       attribute_data.atttypmod
                   )::text AS data_type,
                   NOT attribute_data.attnotnull AS is_nullable,
                   pg_catalog.pg_get_expr(
                       default_data.adbin,
                       default_data.adrelid
                   )::text AS default_expression
            FROM pg_catalog.pg_attribute AS attribute_data
            LEFT JOIN pg_catalog.pg_attrdef AS default_data
              ON default_data.adrelid = attribute_data.attrelid
             AND default_data.adnum = attribute_data.attnum
            WHERE attribute_data.attrelid = pg_catalog.to_regclass(:relation)
              AND attribute_data.attname = :column
              AND attribute_data.attnum > 0
              AND NOT attribute_data.attisdropped
            """
        ),
        {"relation": relation, "column": column},
    ).mappings().one_or_none()


def _constraint_exists(bind, relation: str, name: str) -> bool:
    return bool(
        _scalar(
            bind,
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_constraint AS constraint_data
                WHERE constraint_data.conrelid = pg_catalog.to_regclass(:relation)
                  AND constraint_data.conname = :name
            )
            """,
            {"relation": relation, "name": name},
        )
    )


def _policy_exists(bind, relation: str, name: str) -> bool:
    return bool(
        _scalar(
            bind,
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_policy AS policy_data
                WHERE policy_data.polrelid = pg_catalog.to_regclass(:relation)
                  AND policy_data.polname = :name
            )
            """,
            {"relation": relation, "name": name},
        )
    )


def _function_exists(bind, function_name: str) -> bool:
    # Do not resolve app_secure through to_regprocedure(): reduced
    # migration_owner intentionally has no USAGE on that schema. PostgreSQL
    # system catalogs are sufficient to attest an exact zero-argument function.
    return bool(
        _scalar(
            bind,
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_proc AS procedure_data
                JOIN pg_catalog.pg_namespace AS namespace_data
                  ON namespace_data.oid = procedure_data.pronamespace
                WHERE namespace_data.nspname = 'app_secure'
                  AND procedure_data.proname = :function_name
                  AND procedure_data.pronargs = 0
            )
            """,
            {"function_name": function_name},
        )
    )


def _trigger_exists(bind) -> bool:
    return bool(
        _scalar(
            bind,
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_trigger AS trigger_data
                WHERE trigger_data.tgrelid = pg_catalog.to_regclass(:relation)
                  AND trigger_data.tgname = :trigger_name
                  AND NOT trigger_data.tgisinternal
            )
            """,
            {"relation": _PAYLOAD, "trigger_name": _MARKER_TRIGGER},
        )
    )


def _marker_function_row(bind):
    return bind.execute(
        sa.text(
            """
            SELECT owner_role.rolname::text AS owner_name,
                   procedure_data.prosecdef,
                   procedure_data.provolatile::text AS volatility,
                   procedure_data.proconfig,
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
              AND procedure_data.pronargs = 0
              AND procedure_data.prokind = 'f'
            """
        ),
        {"function_name": _MARKER_FUNCTION_NAME},
    ).mappings().one_or_none()


def _direct_table_acl(bind, role_name: str, relation: str) -> set[str]:
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


def _require_c97_boundary(bind) -> None:
    state = _relation_state(bind, _REGISTRATION)
    if (
        state is None
        or state["owner_name"] != _MIGRATION_OWNER
        or not state["relrowsecurity"]
        or not state["relforcerowsecurity"]
    ):
        raise RuntimeError("P3B c97 registration RLS boundary is not intact")
    if not _policy_exists(
        bind,
        _REGISTRATION,
        "p3b_tenant_isolation_organization_registrations",
    ):
        raise RuntimeError("P3B c97 registration tenant policy is missing")
    for function_name in _C97_FUNCTION_NAMES:
        if not _function_exists(bind, function_name):
            raise RuntimeError(
                f"P3B c97 capability missing: app_secure.{function_name}()"
            )


def _require_predecessor(bind) -> None:
    _require_c97_boundary(bind)
    if _relation_state(bind, _PAYLOAD) is not None:
        raise RuntimeError("P3B secure registration payload relation already exists")
    if _relation_state(bind, _MARKER) is not None:
        raise RuntimeError("P3B envelope marker relation already exists")
    if _marker_function_row(bind) is not None:
        raise RuntimeError("P3B envelope marker function already exists")
    if _column(bind, _REGISTRATION, "crypto_version") is not None:
        raise RuntimeError("P3B registration crypto_version already exists")

    legacy = _column(bind, _REGISTRATION, "id_number_encrypted")
    masked = _column(bind, _REGISTRATION, "id_number_masked")
    if legacy is None or legacy["data_type"] != "text" or legacy["is_nullable"]:
        raise RuntimeError("legacy registration ciphertext column contract drifted")
    if masked is None or masked["data_type"] != "character varying(20)":
        raise RuntimeError("legacy registration mask column contract drifted")

    for relation, name in (
        (_REGISTRATION, _UQ_BUSINESS_KEY),
        (_REGISTRATION, _UQ_ID_TENANT),
        (_REGISTRATION, _CK_CRYPTO),
        (_REGISTRATION, _CK_CANONICAL),
        (_KEY_REGISTRY, _UQ_KEY_SCOPE),
    ):
        if _constraint_exists(bind, relation, name):
            raise RuntimeError(f"unexpected pre-existing P3B constraint {name}")


def _require_forward(bind) -> None:
    _require_c97_boundary(bind)

    crypto = _column(bind, _REGISTRATION, "crypto_version")
    legacy = _column(bind, _REGISTRATION, "id_number_encrypted")
    masked = _column(bind, _REGISTRATION, "id_number_masked")
    if (
        crypto is None
        or crypto["data_type"] != "smallint"
        or crypto["is_nullable"]
        or legacy is None
        or not legacy["is_nullable"]
        or masked is None
        or masked["data_type"] != "character varying(50)"
    ):
        raise RuntimeError("P3B registration envelope metadata contract drifted")

    for name in (_UQ_BUSINESS_KEY, _UQ_ID_TENANT, _CK_CRYPTO, _CK_CANONICAL):
        if not _constraint_exists(bind, _REGISTRATION, name):
            raise RuntimeError(f"P3B registration constraint missing: {name}")
    if not _constraint_exists(bind, _KEY_REGISTRY, _UQ_KEY_SCOPE):
        raise RuntimeError("P3B tenant/domain key binding constraint is missing")

    state = _relation_state(bind, _PAYLOAD)
    if (
        state is None
        or state["owner_name"] != _MIGRATION_OWNER
        or not state["relrowsecurity"]
        or not state["relforcerowsecurity"]
    ):
        raise RuntimeError("P3B secure registration payload RLS/owner contract drifted")
    if not _policy_exists(bind, _PAYLOAD, _PAYLOAD_POLICY):
        raise RuntimeError("P3B secure registration payload tenant policy is missing")
    for name in (
        _CK_PAYLOAD_SCOPE,
        _CK_PAYLOAD_ENVELOPE,
        _FK_PAYLOAD_KEY_SCOPE,
    ):
        if not _constraint_exists(bind, _PAYLOAD, name):
            raise RuntimeError(f"P3B secure registration payload constraint missing: {name}")

    marker_state = _relation_state(bind, _MARKER)
    if (
        marker_state is None
        or marker_state["owner_name"] != _MIGRATION_OWNER
        or marker_state["relrowsecurity"]
        or marker_state["relforcerowsecurity"]
    ):
        raise RuntimeError("P3B envelope marker relation contract drifted")

    if not _trigger_exists(bind):
        raise RuntimeError("P3B envelope marker trigger is missing")
    marker_function = _marker_function_row(bind)
    if (
        marker_function is None
        or marker_function["owner_name"] != _SECURITY_OWNER
        or bool(marker_function["prosecdef"])
        or marker_function["volatility"] != "v"
        or set(marker_function["proconfig"] or [])
        != {"search_path=pg_catalog", "row_security=on"}
        or marker_function["public_execute"]
    ):
        raise RuntimeError("P3B envelope marker function contract drifted")

    for role_name in ("app_runtime", "auth_runtime"):
        if _direct_table_acl(bind, role_name, _PAYLOAD):
            raise RuntimeError(
                f"{role_name} unexpectedly has direct secure registration payload ACL"
            )
        if _direct_table_acl(bind, role_name, _MARKER):
            raise RuntimeError(
                f"{role_name} unexpectedly has direct envelope marker ACL"
            )

    if _direct_table_acl(bind, _SECURITY_OWNER, _PAYLOAD):
        raise RuntimeError("app_security_owner retained direct secure payload ACL")
    if _direct_table_acl(bind, _SECURITY_OWNER, _MARKER) != {"INSERT"}:
        raise RuntimeError("app_security_owner marker ACL must be exactly INSERT")


def _install_marker_trigger(bind) -> None:
    had_create = bool(
        _scalar(
            bind,
            "SELECT pg_catalog.has_schema_privilege(:role_name, 'app_secure', 'CREATE')",
            {"role_name": _SECURITY_OWNER},
        )
    )

    # SECURITY INVOKER keeps the trigger from becoming a privilege-escalation
    # path. migration_owner owns the marker table; the non-login security owner
    # receives exactly INSERT because future bounded writer capabilities execute
    # as this role. TRIGGER and schema CREATE are installation-only privileges.
    bind.execute(
        sa.text(
            "GRANT INSERT ON TABLE public.p3b_registration_envelope_rows "
            "TO app_security_owner"
        )
    )
    bind.execute(
        sa.text(
            "GRANT TRIGGER ON TABLE public.organization_registration_payloads_secure "
            "TO app_security_owner"
        )
    )
    if not had_create:
        bind.execute(sa.text("GRANT CREATE ON SCHEMA app_secure TO app_security_owner"))

    bind.execute(sa.text("SET LOCAL ROLE app_security_owner"))
    try:
        bind.execute(
            sa.text(
                """
                CREATE FUNCTION app_secure.track_registration_envelope_row()
                RETURNS trigger
                LANGUAGE plpgsql
                VOLATILE
                SECURITY INVOKER
                SET search_path = pg_catalog
                SET row_security = on
                AS $function$
                BEGIN
                    INSERT INTO public.p3b_registration_envelope_rows (registration_id)
                    VALUES (NEW.registration_id);
                    RETURN NEW;
                END;
                $function$
                """
            )
        )
        bind.execute(
            sa.text(
                "REVOKE ALL ON FUNCTION "
                "app_secure.track_registration_envelope_row() FROM PUBLIC"
            )
        )
        bind.execute(
            sa.text(
                """
                CREATE TRIGGER trg_p3b_track_registration_envelope_row
                AFTER INSERT ON public.organization_registration_payloads_secure
                FOR EACH ROW
                EXECUTE FUNCTION app_secure.track_registration_envelope_row()
                """
            )
        )
    finally:
        bind.execute(sa.text("RESET ROLE"))

    bind.execute(
        sa.text(
            "REVOKE TRIGGER ON TABLE public.organization_registration_payloads_secure "
            "FROM app_security_owner"
        )
    )
    if not had_create:
        bind.execute(sa.text("REVOKE CREATE ON SCHEMA app_secure FROM app_security_owner"))


def upgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    _require_predecessor(bind)

    # PostgreSQL validates the new CHECK/UNIQUE constraints against the whole
    # relation internally. We intentionally do not bypass FORCE RLS for a
    # migration-owner pre-scan; incompatible legacy rows make this DDL fail.
    bind.execute(
        sa.text(
            """
            ALTER TABLE public.organization_registrations
                ALTER COLUMN id_number_encrypted DROP NOT NULL,
                ALTER COLUMN id_number_masked TYPE varchar(50),
                ADD COLUMN crypto_version smallint NOT NULL DEFAULT 0,
                ADD CONSTRAINT ck_org_reg_crypto_material CHECK (
                    (crypto_version = 0 AND id_number_encrypted IS NOT NULL)
                    OR
                    (crypto_version = 1 AND id_number_encrypted IS NULL)
                ),
                ADD CONSTRAINT ck_org_reg_canonical_identity CHECK (
                    id_type = pg_catalog.upper(pg_catalog.btrim(id_type))
                    AND id_type <> ''
                    AND country_code = pg_catalog.upper(pg_catalog.btrim(country_code))
                    AND pg_catalog.length(country_code) = 2
                ),
                ADD CONSTRAINT uq_org_reg_org_country_type
                    UNIQUE (org_id, country_code, id_type),
                ADD CONSTRAINT uq_org_reg_id_org UNIQUE (id, org_id)
            """
        )
    )
    bind.execute(
        sa.text(
            """
            ALTER TABLE public.encryption_key_registry
                ADD CONSTRAINT uq_key_registry_version_tenant_table
                UNIQUE (key_version, tenant_id, table_name)
            """
        )
    )

    bind.execute(
        sa.text(
            """
            CREATE TABLE public.organization_registration_payloads_secure (
                registration_id uuid NOT NULL,
                tenant_id uuid NOT NULL,
                payload_encrypted bytea NOT NULL,
                key_version integer NOT NULL,
                key_scope varchar(100) NOT NULL DEFAULT 'organization_registrations',
                schema_version smallint NOT NULL DEFAULT 1,
                created_at timestamp with time zone NOT NULL
                    DEFAULT pg_catalog.clock_timestamp(),
                updated_at timestamp with time zone NOT NULL
                    DEFAULT pg_catalog.clock_timestamp(),
                CONSTRAINT pk_organization_registration_payloads_secure
                    PRIMARY KEY (registration_id),
                CONSTRAINT fk_org_reg_payload_registration_tenant
                    FOREIGN KEY (registration_id, tenant_id)
                    REFERENCES public.organization_registrations (id, org_id)
                    ON DELETE CASCADE,
                CONSTRAINT fk_org_reg_payload_key_scope
                    FOREIGN KEY (key_version, tenant_id, key_scope)
                    REFERENCES public.encryption_key_registry
                        (key_version, tenant_id, table_name)
                    ON DELETE RESTRICT,
                CONSTRAINT ck_org_reg_payload_key_scope CHECK (
                    key_scope = 'organization_registrations'
                ),
                CONSTRAINT ck_org_reg_payload_schema_version
                    CHECK (schema_version = 1),
                CONSTRAINT ck_org_reg_payload_envelope_key_version CHECK (
                    pg_catalog.octet_length(payload_encrypted) >= 32
                    AND (
                        pg_catalog.get_byte(payload_encrypted, 0)::bigint * 16777216
                        + pg_catalog.get_byte(payload_encrypted, 1)::bigint * 65536
                        + pg_catalog.get_byte(payload_encrypted, 2)::bigint * 256
                        + pg_catalog.get_byte(payload_encrypted, 3)::bigint
                    ) = key_version::bigint
                )
            ) WITH (fillfactor = 90)
            """
        )
    )
    bind.execute(
        sa.text(
            """
            CREATE INDEX ix_org_reg_payload_tenant_key
                ON public.organization_registration_payloads_secure
                (tenant_id, key_version)
            """
        )
    )
    bind.execute(
        sa.text(
            "REVOKE ALL ON TABLE public.organization_registration_payloads_secure FROM PUBLIC"
        )
    )
    bind.execute(
        sa.text(
            "ALTER TABLE public.organization_registration_payloads_secure ENABLE ROW LEVEL SECURITY"
        )
    )
    bind.execute(
        sa.text(
            "ALTER TABLE public.organization_registration_payloads_secure FORCE ROW LEVEL SECURITY"
        )
    )
    bind.execute(
        sa.text(
            """
            CREATE POLICY p3b_tenant_isolation_registration_payloads_secure
            ON public.organization_registration_payloads_secure
            USING (
                tenant_id = NULLIF(
                    pg_catalog.current_setting('app.current_org_id', true), ''
                )::uuid
            )
            WITH CHECK (
                tenant_id = NULLIF(
                    pg_catalog.current_setting('app.current_org_id', true), ''
                )::uuid
            )
            """
        )
    )

    # The marker is deliberately outside RLS and contains only registration
    # UUIDs. No runtime role can read or mutate it. It exists solely so
    # migration_owner can prove that a downgrade is lossless without bypassing
    # the FORCE-RLS secure payload relation.
    bind.execute(
        sa.text(
            """
            CREATE TABLE public.p3b_registration_envelope_rows (
                registration_id uuid PRIMARY KEY,
                CONSTRAINT fk_p3b_registration_envelope_marker
                    FOREIGN KEY (registration_id)
                    REFERENCES public.organization_registration_payloads_secure
                        (registration_id)
                    ON UPDATE CASCADE
                    ON DELETE CASCADE
            )
            """
        )
    )
    bind.execute(
        sa.text(
            "REVOKE ALL ON TABLE public.p3b_registration_envelope_rows FROM PUBLIC"
        )
    )
    _install_marker_trigger(bind)

    _require_forward(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    _require_forward(bind)

    if _scalar(
        bind,
        "SELECT EXISTS (SELECT 1 FROM public.p3b_registration_envelope_rows)",
    ):
        raise RuntimeError(
            "P3B downgrade would discard KMS-backed registration envelope data"
        )

    # These predecessor conversions are themselves full-relation validation.
    # They fail rather than truncate data or silently convert a crypto-v1 row.
    bind.execute(
        sa.text(
            """
            ALTER TABLE public.organization_registrations
                ALTER COLUMN id_number_masked TYPE varchar(20),
                ALTER COLUMN id_number_encrypted SET NOT NULL
            """
        )
    )

    bind.execute(sa.text("DROP TABLE public.p3b_registration_envelope_rows RESTRICT"))
    bind.execute(
        sa.text(
            "DROP TABLE public.organization_registration_payloads_secure RESTRICT"
        )
    )
    bind.execute(
        sa.text(
            """
            ALTER TABLE public.encryption_key_registry
                DROP CONSTRAINT uq_key_registry_version_tenant_table
            """
        )
    )
    bind.execute(sa.text("SET LOCAL ROLE app_security_owner"))
    try:
        bind.execute(
            sa.text(
                "DROP FUNCTION app_secure.track_registration_envelope_row() RESTRICT"
            )
        )
    finally:
        bind.execute(sa.text("RESET ROLE"))

    bind.execute(
        sa.text(
            """
            ALTER TABLE public.organization_registrations
                DROP CONSTRAINT uq_org_reg_id_org,
                DROP CONSTRAINT uq_org_reg_org_country_type,
                DROP CONSTRAINT ck_org_reg_canonical_identity,
                DROP CONSTRAINT ck_org_reg_crypto_material,
                DROP COLUMN crypto_version
            """
        )
    )

    _require_predecessor(bind)
