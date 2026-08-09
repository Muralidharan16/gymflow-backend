"""Establish a typed, append-only audit principal registry.

Revision ID: 4d5e6f708192
Revises: 3c4d5e6f7081
Create Date: 2026-08-09

The application currently has three UUID-bearing human identity domains:
``owners`` (the active owner authentication flow), ``organization_users``
(the modern tenant/RBAC identity), and ``gym_owners`` (legacy staff identity).
A bare UUID in ``app.current_user_id`` is therefore not a sufficient audit
identity contract.

This revision introduces ``public.audit_principals`` as a non-PII, append-only
registry of valid actor identifiers. Address history/audit rows persist both
actor id and actor namespace and reference that registry with a composite FK.
The registry is populated by database triggers on each source identity table,
so application code cannot accidentally create an authenticated identity that
is invisible to the audit subsystem.

The revision deliberately does not merge authentication domains or guess that
two rows with the same email represent one person. That larger identity-domain
convergence can happen independently. Here we make the currently supported
coexistence explicit, typed, and referentially safe.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "4d5e6f708192"
down_revision = "3c4d5e6f7081"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_ALLOWED_TYPES = ("owner", "organization_user", "legacy_gym_owner")


def _scalar(bind, sql: str, params=None):
    return bind.execute(sa.text(sql), params or {}).scalar_one()


def _require_preflight(bind) -> None:
    identity = bind.execute(
        sa.text(
            """
            SELECT
                session_user::text AS session_user_name,
                current_user::text AS current_user_name,
                role_data.rolsuper,
                role_data.rolcreatedb,
                role_data.rolcreaterole,
                role_data.rolinherit,
                role_data.rolreplication,
                role_data.rolbypassrls
            FROM pg_catalog.pg_roles AS role_data
            WHERE role_data.rolname = current_user
            """
        )
    ).mappings().one()
    if identity["session_user_name"] != _MIGRATION_OWNER:
        raise RuntimeError(
            "Audit-principal migration requires session_user=migration_owner."
        )
    if identity["current_user_name"] != _MIGRATION_OWNER:
        raise RuntimeError(
            "Audit-principal migration requires current_user=migration_owner."
        )
    unsafe = {
        key: bool(identity[key])
        for key in (
            "rolsuper",
            "rolcreatedb",
            "rolcreaterole",
            "rolinherit",
            "rolreplication",
            "rolbypassrls",
        )
        if identity[key]
    }
    if unsafe:
        raise RuntimeError(
            f"migration_owner is over-privileged for audit-principal migration: {unsafe!r}"
        )

    missing = bind.execute(
        sa.text(
            """
            SELECT required.relation_name
            FROM (
                VALUES
                    ('public.owners'::text),
                    ('public.organization_users'::text),
                    ('public.gym_owners'::text),
                    ('public.organization_addresses'::text),
                    ('public.branch_address_history'::text),
                    ('public.branch_address_audit_log'::text)
            ) AS required(relation_name)
            WHERE pg_catalog.to_regclass(required.relation_name) IS NULL
            ORDER BY required.relation_name
            """
        )
    ).scalars().all()
    if missing:
        raise RuntimeError(
            f"Required predecessor relations are missing: {tuple(missing)!r}"
        )

    security_role_exists = _scalar(
        bind,
        "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = :role)",
        {"role": _SECURITY_OWNER},
    )
    if security_role_exists is not True:
        raise RuntimeError("Required managed role app_security_owner is absent.")

    can_set = _scalar(
        bind,
        """
        SELECT pg_catalog.pg_has_role(
            session_user,
            CAST(:role AS name),
            'SET'
        )
        """,
        {"role": _SECURITY_OWNER},
    )
    if can_set is not True:
        raise RuntimeError(
            "migration_owner lacks bounded SET capability to app_security_owner."
        )

    schema_state = bind.execute(
        sa.text(
            """
            SELECT
                pg_catalog.pg_get_userbyid(ns.nspowner)::text AS owner_name,
                pg_catalog.has_schema_privilege(
                    CAST(:security_owner AS name),
                    ns.oid,
                    'USAGE'
                ) AS security_owner_usage,
                pg_catalog.has_schema_privilege(
                    current_user,
                    ns.oid,
                    'CREATE'
                ) AS migration_owner_create
            FROM pg_catalog.pg_namespace AS ns
            WHERE ns.nspname = 'app_private'
            """
        ),
        {"security_owner": _SECURITY_OWNER},
    ).mappings().one_or_none()
    if schema_state is None:
        raise RuntimeError("Required schema app_private is absent.")
    if schema_state["owner_name"] != _MIGRATION_OWNER:
        raise RuntimeError(
            "app_private ownership drifted; expected migration_owner, observed "
            f"{schema_state['owner_name']!r}."
        )
    if schema_state["security_owner_usage"] is not True:
        raise RuntimeError("app_security_owner lacks USAGE on app_private.")
    if schema_state["migration_owner_create"] is not True:
        raise RuntimeError("migration_owner lacks CREATE on app_private.")


def _security_owner_has_create(bind) -> bool:
    return bool(
        _scalar(
            bind,
            """
            SELECT pg_catalog.has_schema_privilege(
                CAST(:role AS name),
                CAST('app_private' AS name),
                'CREATE'
            )
            """,
            {"role": _SECURITY_OWNER},
        )
    )


def _create_private_functions(bind) -> None:
    had_create = _security_owner_has_create(bind)
    if not had_create:
        bind.execute(
            sa.text("GRANT CREATE ON SCHEMA app_private TO app_security_owner")
        )

    bind.execute(sa.text("SET LOCAL ROLE app_security_owner"))
    try:
        bind.execute(
            sa.text(
                """
                CREATE FUNCTION app_private.register_audit_principal()
                RETURNS trigger
                LANGUAGE plpgsql
                SECURITY DEFINER
                SET search_path = pg_catalog
                AS $$
                DECLARE
                    v_type text := TG_ARGV[0];
                BEGIN
                    IF v_type NOT IN (
                        'owner',
                        'organization_user',
                        'legacy_gym_owner'
                    ) THEN
                        RAISE EXCEPTION
                            'Unsupported audit principal type: %', v_type;
                    END IF;
                    IF NEW.id IS NULL OR NEW.org_id IS NULL THEN
                        RAISE EXCEPTION
                            'Audit principal source identity requires id and org_id.';
                    END IF;

                    INSERT INTO public.audit_principals (
                        principal_id,
                        org_id,
                        principal_type
                    ) VALUES (
                        NEW.id,
                        NEW.org_id,
                        v_type
                    )
                    ON CONFLICT (principal_id, org_id, principal_type)
                    DO NOTHING;
                    RETURN NEW;
                END;
                $$
                """
            )
        )
        bind.execute(
            sa.text(
                """
                CREATE FUNCTION app_private.prevent_principal_identity_reassignment()
                RETURNS trigger
                LANGUAGE plpgsql
                SECURITY DEFINER
                SET search_path = pg_catalog
                AS $$
                BEGIN
                    IF NEW.id IS DISTINCT FROM OLD.id
                       OR NEW.org_id IS DISTINCT FROM OLD.org_id THEN
                        RAISE EXCEPTION
                            'Authenticated principal id/org_id are immutable.';
                    END IF;
                    RETURN NEW;
                END;
                $$
                """
            )
        )
        bind.execute(
            sa.text(
                """
                CREATE FUNCTION app_private.prevent_audit_principal_mutation()
                RETURNS trigger
                LANGUAGE plpgsql
                SECURITY DEFINER
                SET search_path = pg_catalog
                AS $$
                BEGIN
                    RAISE EXCEPTION 'audit_principals is append-only';
                END;
                $$
                """
            )
        )
        bind.execute(
            sa.text(
                "REVOKE ALL ON FUNCTION app_private.register_audit_principal() FROM PUBLIC"
            )
        )
        bind.execute(
            sa.text(
                "REVOKE ALL ON FUNCTION app_private.prevent_principal_identity_reassignment() FROM PUBLIC"
            )
        )
        bind.execute(
            sa.text(
                "REVOKE ALL ON FUNCTION app_private.prevent_audit_principal_mutation() FROM PUBLIC"
            )
        )
    finally:
        bind.execute(sa.text("RESET ROLE"))
        if not had_create:
            bind.execute(
                sa.text("REVOKE CREATE ON SCHEMA app_private FROM app_security_owner")
            )


def _replace_address_snapshot_functions(bind, *, typed: bool) -> None:
    if typed:
        insert_columns = (
            "address_id, org_id, dek_version, address_line1, address_line2, city, "
            "state_province, country_code, postal_code, formatted_address, "
            "valid_from, changed_by, changed_by_type"
        )
        insert_values_new = (
            "NEW.id, NEW.org_id, NEW.dek_version, NEW.address_line1, NEW.address_line2, "
            "NEW.city, NEW.state_province, NEW.country_code, NEW.postal_code, "
            "NEW.formatted_address, clock_timestamp(), "
            "NULLIF(current_setting('app.current_user_id', true), '')::UUID, "
            "NULLIF(current_setting('app.current_principal_type', true), '')"
        )
        insert_values_old = (
            "OLD.id, OLD.org_id, OLD.dek_version, OLD.address_line1, OLD.address_line2, "
            "OLD.city, OLD.state_province, OLD.country_code, OLD.postal_code, "
            "OLD.formatted_address, v_now, "
            "NULLIF(current_setting('app.current_user_id', true), '')::UUID, "
            "NULLIF(current_setting('app.current_principal_type', true), '')"
        )
        audit_columns = (
            "event_id, address_id, org_id, dek_version, old_address, new_address, "
            "changed_by, changed_by_type, ip_address, user_agent, request_id"
        )
        audit_actor = (
            "NULLIF(current_setting('app.current_user_id', true), '')::UUID, "
            "NULLIF(current_setting('app.current_principal_type', true), ''),"
        )
    else:
        insert_columns = (
            "address_id, org_id, dek_version, address_line1, address_line2, city, "
            "state_province, country_code, postal_code, formatted_address, "
            "valid_from, changed_by"
        )
        insert_values_new = (
            "NEW.id, NEW.org_id, NEW.dek_version, NEW.address_line1, NEW.address_line2, "
            "NEW.city, NEW.state_province, NEW.country_code, NEW.postal_code, "
            "NEW.formatted_address, clock_timestamp(), "
            "NULLIF(current_setting('app.current_user_id', true), '')::UUID"
        )
        insert_values_old = (
            "OLD.id, OLD.org_id, OLD.dek_version, OLD.address_line1, OLD.address_line2, "
            "OLD.city, OLD.state_province, OLD.country_code, OLD.postal_code, "
            "OLD.formatted_address, v_now, "
            "NULLIF(current_setting('app.current_user_id', true), '')::UUID"
        )
        audit_columns = (
            "event_id, address_id, org_id, dek_version, old_address, new_address, "
            "changed_by, ip_address, user_agent, request_id"
        )
        audit_actor = (
            "NULLIF(current_setting('app.current_user_id', true), '')::UUID,"
        )

    bind.execute(
        sa.text(
            f"""
            CREATE OR REPLACE FUNCTION snapshot_address_on_insert()
            RETURNS trigger AS $$
            BEGIN
              IF current_setting('app.skip_history_snapshot', true) = 'true' THEN
                RETURN NEW;
              END IF;

              INSERT INTO branch_address_history ({insert_columns})
              VALUES ({insert_values_new});
              RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )

    bind.execute(
        sa.text(
            f"""
            CREATE OR REPLACE FUNCTION snapshot_address_on_change()
            RETURNS trigger AS $$
            DECLARE
              v_now TIMESTAMPTZ := clock_timestamp();
            BEGIN
              IF NEW._reencryption_in_progress = TRUE THEN
                IF ROW(OLD.city, OLD.state_province, OLD.country_code, OLD.postal_code)
                   IS NOT DISTINCT FROM
                   ROW(NEW.city, NEW.state_province, NEW.country_code, NEW.postal_code) THEN
                  NEW._reencryption_in_progress := FALSE;
                  RETURN NEW;
                END IF;
                RAISE EXCEPTION
                  'plaintext fields mutated during KMS re-encryption pass: address_id=%',
                  OLD.id;
              END IF;

              IF ROW(OLD.address_line1, OLD.address_line2, OLD.city, OLD.state_province,
                     OLD.country_code, OLD.postal_code)
                 IS DISTINCT FROM
                 ROW(NEW.address_line1, NEW.address_line2, NEW.city, NEW.state_province,
                     NEW.country_code, NEW.postal_code) THEN

                UPDATE branch_address_history
                SET valid_to = v_now
                WHERE address_id = OLD.id AND valid_to IS NULL;

                INSERT INTO branch_address_history ({insert_columns})
                VALUES ({insert_values_old});

                INSERT INTO branch_address_audit_log ({audit_columns})
                VALUES (
                  gen_random_uuid(),
                  OLD.id,
                  OLD.org_id,
                  OLD.dek_version,
                  jsonb_build_object(
                    'city', OLD.city,
                    'state', OLD.state_province,
                    'country_code', OLD.country_code,
                    'postal_code', OLD.postal_code,
                    'dek_version', OLD.dek_version,
                    'address_line1_hash', encode(sha256(OLD.address_line1::bytea), 'hex')
                  ),
                  jsonb_build_object(
                    'city', NEW.city,
                    'state', NEW.state_province,
                    'country_code', NEW.country_code,
                    'postal_code', NEW.postal_code,
                    'dek_version', NEW.dek_version,
                    'address_line1_hash', encode(sha256(NEW.address_line1::bytea), 'hex')
                  ),
                  {audit_actor}
                  NULLIF(current_setting('app.ip_address', true), '')::INET,
                  NULLIF(current_setting('app.user_agent', true), ''),
                  NULLIF(current_setting('app.request_id', true), '')::UUID
                );

                INSERT INTO address_change_outbox (
                    address_id, org_id, event_type, payload
                ) VALUES (
                    NEW.id,
                    NEW.org_id,
                    'address_updated',
                    jsonb_build_object('address_id', NEW.id, 'timestamp', v_now)
                );
              END IF;
              RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )


def upgrade() -> None:
    bind = op.get_bind()
    _require_preflight(bind)

    op.create_table(
        "audit_principals",
        sa.Column("principal_id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("principal_type", sa.String(length=32), nullable=False),
        sa.Column(
            "registered_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "principal_type IN ('owner', 'organization_user', 'legacy_gym_owner')",
            name="ck_audit_principals_type",
        ),
        sa.PrimaryKeyConstraint(
            "principal_id",
            "org_id",
            "principal_type",
            name="pk_audit_principals",
        ),
        schema="public",
    )
    op.execute("REVOKE ALL ON TABLE public.audit_principals FROM PUBLIC")
    op.execute(
        "GRANT SELECT, INSERT ON TABLE public.audit_principals TO app_security_owner"
    )

    # Register all predecessor identities before enforcing typed audit FKs.
    for source_table, principal_type in (
        ("owners", "owner"),
        ("organization_users", "organization_user"),
        ("gym_owners", "legacy_gym_owner"),
    ):
        bind.execute(
            sa.text(
                f"""
                INSERT INTO public.audit_principals (
                    principal_id, org_id, principal_type
                )
                SELECT id, org_id, :principal_type
                FROM public.{source_table}
                ON CONFLICT (principal_id, org_id, principal_type) DO NOTHING
                """
            ),
            {"principal_type": principal_type},
        )

    op.add_column(
        "branch_address_history",
        sa.Column("changed_by_type", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "branch_address_audit_log",
        sa.Column("changed_by_type", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "organization_addresses",
        sa.Column("deleted_by_type", sa.String(length=32), nullable=True),
    )

    # History/deleted_by previously had an FK to gym_owners, so their provenance
    # is unambiguous. The audit-log UUID was untyped; resolve only when exactly one
    # source identity matches in the same tenant and fail closed otherwise.
    op.execute(
        """
        UPDATE public.branch_address_history
        SET changed_by_type = 'legacy_gym_owner'
        WHERE changed_by IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE public.organization_addresses
        SET deleted_by_type = 'legacy_gym_owner'
        WHERE deleted_by IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE public.branch_address_audit_log AS audit
        SET changed_by_type = CASE
            WHEN (
                (EXISTS (
                    SELECT 1 FROM public.owners AS actor
                    WHERE actor.id = audit.changed_by
                      AND actor.org_id = audit.org_id
                ))::int
                + (EXISTS (
                    SELECT 1 FROM public.organization_users AS actor
                    WHERE actor.id = audit.changed_by
                      AND actor.org_id = audit.org_id
                ))::int
                + (EXISTS (
                    SELECT 1 FROM public.gym_owners AS actor
                    WHERE actor.id = audit.changed_by
                      AND actor.org_id = audit.org_id
                ))::int
            ) = 1
            THEN CASE
                WHEN EXISTS (
                    SELECT 1 FROM public.owners AS actor
                    WHERE actor.id = audit.changed_by
                      AND actor.org_id = audit.org_id
                ) THEN 'owner'
                WHEN EXISTS (
                    SELECT 1 FROM public.organization_users AS actor
                    WHERE actor.id = audit.changed_by
                      AND actor.org_id = audit.org_id
                ) THEN 'organization_user'
                ELSE 'legacy_gym_owner'
            END
            ELSE NULL
        END
        WHERE audit.changed_by IS NOT NULL
        """
    )
    unresolved = _scalar(
        bind,
        """
        SELECT EXISTS (
            SELECT 1
            FROM public.branch_address_audit_log
            WHERE changed_by IS NOT NULL
              AND changed_by_type IS NULL
        )
        """,
    )
    if unresolved:
        raise RuntimeError(
            "Existing branch_address_audit_log actor UUIDs are missing or ambiguous "
            "across owner/organization_user/legacy_gym_owner identity domains. "
            "Reconcile the data explicitly before upgrading."
        )

    op.execute(
        "ALTER TABLE public.branch_address_history "
        "DROP CONSTRAINT IF EXISTS branch_address_history_changed_by_fkey"
    )
    op.execute(
        "ALTER TABLE public.organization_addresses "
        "DROP CONSTRAINT IF EXISTS organization_addresses_deleted_by_fkey"
    )

    # NOT VALID keeps the blocking phase small on large history tables; validation
    # is explicit and completes before the revision is marked applied.
    constraint_sql = (
        (
            "branch_address_history",
            "ck_branch_address_history_actor_pair",
            "CHECK ((changed_by IS NULL AND changed_by_type IS NULL) OR "
            "(changed_by IS NOT NULL AND changed_by_type IS NOT NULL)) NOT VALID",
        ),
        (
            "branch_address_history",
            "ck_branch_address_history_actor_type",
            "CHECK (changed_by_type IS NULL OR changed_by_type IN "
            "('owner','organization_user','legacy_gym_owner')) NOT VALID",
        ),
        (
            "branch_address_audit_log",
            "ck_branch_address_audit_actor_pair",
            "CHECK ((changed_by IS NULL AND changed_by_type IS NULL) OR "
            "(changed_by IS NOT NULL AND changed_by_type IS NOT NULL)) NOT VALID",
        ),
        (
            "branch_address_audit_log",
            "ck_branch_address_audit_actor_type",
            "CHECK (changed_by_type IS NULL OR changed_by_type IN "
            "('owner','organization_user','legacy_gym_owner')) NOT VALID",
        ),
        (
            "organization_addresses",
            "ck_organization_addresses_deleted_actor_pair",
            "CHECK ((deleted_by IS NULL AND deleted_by_type IS NULL) OR "
            "(deleted_by IS NOT NULL AND deleted_by_type IS NOT NULL)) NOT VALID",
        ),
        (
            "organization_addresses",
            "ck_organization_addresses_deleted_actor_type",
            "CHECK (deleted_by_type IS NULL OR deleted_by_type IN "
            "('owner','organization_user','legacy_gym_owner')) NOT VALID",
        ),
    )
    for table_name, constraint_name, definition in constraint_sql:
        op.execute(
            f"ALTER TABLE public.{table_name} ADD CONSTRAINT "
            f"{constraint_name} {definition}"
        )
        op.execute(
            f"ALTER TABLE public.{table_name} VALIDATE CONSTRAINT {constraint_name}"
        )

    fk_sql = (
        (
            "branch_address_history",
            "fk_branch_address_history_audit_principal",
            "changed_by, org_id, changed_by_type",
        ),
        (
            "branch_address_audit_log",
            "fk_branch_address_audit_audit_principal",
            "changed_by, org_id, changed_by_type",
        ),
        (
            "organization_addresses",
            "fk_organization_addresses_deleted_audit_principal",
            "deleted_by, org_id, deleted_by_type",
        ),
    )
    for table_name, constraint_name, columns in fk_sql:
        op.execute(
            f"ALTER TABLE public.{table_name} ADD CONSTRAINT {constraint_name} "
            f"FOREIGN KEY ({columns}) REFERENCES public.audit_principals "
            "(principal_id, org_id, principal_type) ON DELETE RESTRICT NOT VALID"
        )
        op.execute(
            f"ALTER TABLE public.{table_name} VALIDATE CONSTRAINT {constraint_name}"
        )

    _create_private_functions(bind)

    op.execute(
        "CREATE TRIGGER trg_audit_principals_immutable "
        "BEFORE UPDATE OR DELETE ON public.audit_principals "
        "FOR EACH ROW EXECUTE FUNCTION app_private.prevent_audit_principal_mutation()"
    )

    for table_name, principal_type in (
        ("owners", "owner"),
        ("organization_users", "organization_user"),
        ("gym_owners", "legacy_gym_owner"),
    ):
        op.execute(
            f"CREATE TRIGGER trg_register_audit_principal_{table_name} "
            f"AFTER INSERT ON public.{table_name} FOR EACH ROW "
            f"EXECUTE FUNCTION app_private.register_audit_principal('{principal_type}')"
        )
        op.execute(
            f"CREATE TRIGGER trg_prevent_principal_reassignment_{table_name} "
            f"BEFORE UPDATE OF id, org_id ON public.{table_name} FOR EACH ROW "
            "EXECUTE FUNCTION app_private.prevent_principal_identity_reassignment()"
        )

    _replace_address_snapshot_functions(bind, typed=True)


def downgrade() -> None:
    bind = op.get_bind()
    _require_preflight(bind)

    # The predecessor schema cannot represent owner/organization_user provenance.
    # Refuse a lossy rollback once such audit data exists.
    incompatible = bind.execute(
        sa.text(
            """
            SELECT source, row_count
            FROM (
                SELECT
                    'branch_address_history'::text AS source,
                    count(*)::bigint AS row_count
                FROM public.branch_address_history
                WHERE changed_by IS NOT NULL
                  AND changed_by_type <> 'legacy_gym_owner'
                UNION ALL
                SELECT
                    'branch_address_audit_log',
                    count(*)::bigint
                FROM public.branch_address_audit_log
                WHERE changed_by IS NOT NULL
                  AND changed_by_type <> 'legacy_gym_owner'
                UNION ALL
                SELECT
                    'organization_addresses',
                    count(*)::bigint
                FROM public.organization_addresses
                WHERE deleted_by IS NOT NULL
                  AND deleted_by_type <> 'legacy_gym_owner'
            ) AS counts
            WHERE row_count > 0
            ORDER BY source
            """
        )
    ).all()
    if incompatible:
        raise RuntimeError(
            "Downgrade would discard typed audit-principal provenance; refusing: "
            f"{tuple(incompatible)!r}."
        )

    _replace_address_snapshot_functions(bind, typed=False)

    for table_name in ("owners", "organization_users", "gym_owners"):
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_register_audit_principal_{table_name} "
            f"ON public.{table_name}"
        )
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_prevent_principal_reassignment_{table_name} "
            f"ON public.{table_name}"
        )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_audit_principals_immutable ON public.audit_principals"
    )

    for table_name, constraint_name in (
        ("branch_address_history", "fk_branch_address_history_audit_principal"),
        ("branch_address_audit_log", "fk_branch_address_audit_audit_principal"),
        ("organization_addresses", "fk_organization_addresses_deleted_audit_principal"),
        ("branch_address_history", "ck_branch_address_history_actor_pair"),
        ("branch_address_history", "ck_branch_address_history_actor_type"),
        ("branch_address_audit_log", "ck_branch_address_audit_actor_pair"),
        ("branch_address_audit_log", "ck_branch_address_audit_actor_type"),
        ("organization_addresses", "ck_organization_addresses_deleted_actor_pair"),
        ("organization_addresses", "ck_organization_addresses_deleted_actor_type"),
    ):
        op.execute(
            f"ALTER TABLE public.{table_name} DROP CONSTRAINT IF EXISTS {constraint_name}"
        )

    op.drop_column("organization_addresses", "deleted_by_type")
    op.drop_column("branch_address_audit_log", "changed_by_type")
    op.drop_column("branch_address_history", "changed_by_type")

    op.create_foreign_key(
        "branch_address_history_changed_by_fkey",
        "branch_address_history",
        "gym_owners",
        ["changed_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "organization_addresses_deleted_by_fkey",
        "organization_addresses",
        "gym_owners",
        ["deleted_by"],
        ["id"],
        ondelete="SET NULL",
    )

    op.drop_table("audit_principals", schema="public")

    had_create = _security_owner_has_create(bind)
    if not had_create:
        bind.execute(
            sa.text("GRANT CREATE ON SCHEMA app_private TO app_security_owner")
        )
    bind.execute(sa.text("SET LOCAL ROLE app_security_owner"))
    try:
        bind.execute(
            sa.text("DROP FUNCTION app_private.prevent_audit_principal_mutation()")
        )
        bind.execute(
            sa.text("DROP FUNCTION app_private.prevent_principal_identity_reassignment()")
        )
        bind.execute(sa.text("DROP FUNCTION app_private.register_audit_principal()"))
    finally:
        bind.execute(sa.text("RESET ROLE"))
        if not had_create:
            bind.execute(
                sa.text("REVOKE CREATE ON SCHEMA app_private FROM app_security_owner")
            )
