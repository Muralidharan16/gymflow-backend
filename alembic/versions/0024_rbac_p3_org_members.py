"""RBAC Hardening Phase 3 — organization_members Table

Phase 3 of the v18.0 hardening plan.

Creates:
  • public.organization_members
      — The core tenancy boundary separating global identity from
        scoped authorization.
      — References organization_users(id, org_id) via composite FK.
      — Holds membership lifecycle state (references membership_statuses).
      — Exposes UNIQUE (id, org_id) for downstream composite FKs
        (branch_staff_roles Phase 5).

  • app_private.touch_updated_at()
      — Generic BEFORE UPDATE trigger to auto-maintain updated_at.

  • app_private.enforce_membership_state_transition()
      — State machine guard: prevents invalid lifecycle transitions.
        Terminal state: revoked (id=5) cannot transition back to pending (id=1).

  • RLS policy: tenant_isolation_organization_members
      — Fail-closed: requires app.current_org_id GUC to be explicitly set.
      — Excludes soft-deleted rows from all reads and writes.

Does NOT modify branch_staff_roles. That is Phase 5 (Expand step).

Revision ID: 0024_rbac_p3_org_members
Revises: 0023_rbac_p2_ref_tables
Create Date: 2026-05-23
"""

from alembic import op
import sqlalchemy as sa

revision = "0024_rbac_p3_org_members"
down_revision = "0023_rbac_p2_ref_tables"
branch_labels = None
depends_on = None


# RB1M1A_0024_APP_PRIVATE_OWNER_CONTEXT_HELPERS_START
# Frozen revision-local contract. Do not import this logic from another migration.
_RB1M1A_SCHEMA = "app_private"
_RB1M1A_TARGET_OWNER = "app_security_owner"
_RB1M1A_FUNCTIONS = (
    "app_private.touch_updated_at()",
    "app_private.enforce_membership_state_transition()",
)
_RB1M1A_TRIGGER_MAP = (
    (
        "trg_touch_organization_members_updated_at",
        "app_private.touch_updated_at()",
    ),
    (
        "trg_membership_state_transition",
        "app_private.enforce_membership_state_transition()",
    ),
)


def _rb1m1a_identity(bind):
    row = bind.execute(
        sa.text(
            """
            SELECT
                session_user::text AS session_user_name,
                current_user::text AS current_user_name
            """
        )
    ).mappings().one()
    return {
        "session_user_name": row["session_user_name"],
        "current_user_name": row["current_user_name"],
    }


def _rb1m1a_require_migration_owner(bind):
    identity = _rb1m1a_identity(bind)
    if identity["session_user_name"] != "migration_owner":
        raise RuntimeError(
            "RB1M1A requires session_user=migration_owner; "
            f"observed {identity['session_user_name']!r}."
        )
    if identity["current_user_name"] != "migration_owner":
        raise RuntimeError(
            "RB1M1A requires current_user=migration_owner; "
            f"observed {identity['current_user_name']!r}."
        )


def _rb1m1a_has_schema_privilege(bind, role_name, privilege):
    return bind.execute(
        sa.text(
            """
            SELECT pg_catalog.has_schema_privilege(
                CAST(:role_name AS name),
                CAST(:schema_name AS name),
                :privilege
            )
            """
        ),
        {
            "role_name": role_name,
            "schema_name": _RB1M1A_SCHEMA,
            "privilege": privilege,
        },
    ).scalar_one() is True


def _rb1m1a_preflight(bind, *, require_functions):
    """Read-only validation before any revision-0024 catalog mutation."""
    _rb1m1a_require_migration_owner(bind)
    row = bind.execute(
        sa.text(
            """
            SELECT
                namespace_data.oid IS NOT NULL AS schema_exists,
                pg_catalog.pg_get_userbyid(
                    namespace_data.nspowner
                )::text AS schema_owner_name,
                EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_roles AS role_data
                    WHERE role_data.rolname = :target_owner
                ) AS target_role_exists,
                pg_catalog.pg_has_role(
                    session_user,
                    CAST(:target_owner AS name),
                    'SET'
                ) AS can_set_target_owner,
                pg_catalog.has_schema_privilege(
                    current_user,
                    CAST(:schema_name AS name),
                    'CREATE'
                ) AS migration_owner_can_create,
                pg_catalog.has_schema_privilege(
                    current_user,
                    CAST(:schema_name AS name),
                    'USAGE'
                ) AS migration_owner_can_use,
                pg_catalog.has_schema_privilege(
                    CAST(:target_owner AS name),
                    CAST(:schema_name AS name),
                    'USAGE'
                ) AS target_owner_can_use
            FROM (SELECT 1) AS singleton
            LEFT JOIN pg_catalog.pg_namespace AS namespace_data
              ON namespace_data.nspname = :schema_name
            """
        ),
        {
            "schema_name": _RB1M1A_SCHEMA,
            "target_owner": _RB1M1A_TARGET_OWNER,
        },
    ).mappings().one()
    if not row["schema_exists"]:
        raise RuntimeError("Required schema app_private is absent.")
    if row["schema_owner_name"] != "migration_owner":
        raise RuntimeError(
            "app_private must be owned by migration_owner; "
            f"observed {row['schema_owner_name']!r}."
        )
    if not row["target_role_exists"]:
        raise RuntimeError("Required managed role app_security_owner is absent.")
    if not row["can_set_target_owner"]:
        raise RuntimeError(
            "migration_owner cannot SET ROLE app_security_owner."
        )
    if not row["migration_owner_can_create"]:
        raise RuntimeError(
            "migration_owner lacks CREATE on app_private required for "
            "temporary target-owner capability."
        )
    if not row["migration_owner_can_use"]:
        raise RuntimeError("migration_owner lacks USAGE on app_private.")
    if not row["target_owner_can_use"]:
        raise RuntimeError("app_security_owner lacks USAGE on app_private.")

    function_rows = bind.execute(
        sa.text(
            """
            SELECT
                requested.signature,
                procedure_data.oid IS NOT NULL AS function_exists,
                owner_role.rolname::text AS owner_name
            FROM (
                VALUES
                    ('app_private.touch_updated_at()'::text),
                    ('app_private.enforce_membership_state_transition()'::text)
            ) AS requested(signature)
            LEFT JOIN pg_catalog.pg_proc AS procedure_data
              ON procedure_data.oid = pg_catalog.to_regprocedure(
                    requested.signature
                 )
            LEFT JOIN pg_catalog.pg_roles AS owner_role
              ON owner_role.oid = procedure_data.proowner
            ORDER BY requested.signature
            """
        )
    ).mappings().all()
    if len(function_rows) != len(_RB1M1A_FUNCTIONS):
        raise RuntimeError("Revision-0024 function preflight returned drift.")
    for function_row in function_rows:
        if function_row["function_exists"]:
            if function_row["owner_name"] != _RB1M1A_TARGET_OWNER:
                raise RuntimeError(
                    "Pre-existing revision-0024 function has unexpected "
                    f"owner: {function_row!r}."
                )
        elif require_functions:
            raise RuntimeError(
                "Required revision-0024 function is absent during downgrade: "
                f"{function_row['signature']}."
            )


def _rb1m1a_direct_create_acl_rows(bind):
    rows = bind.execute(
        sa.text(
            """
            SELECT
                grantor_role.rolname::text AS grantor_name,
                grantee_role.rolname::text AS grantee_name,
                acl_data.privilege_type::text AS privilege_type,
                acl_data.is_grantable AS is_grantable
            FROM pg_catalog.pg_namespace AS namespace_data
            CROSS JOIN LATERAL pg_catalog.aclexplode(
                namespace_data.nspacl
            ) AS acl_data
            LEFT JOIN pg_catalog.pg_roles AS grantor_role
              ON grantor_role.oid = acl_data.grantor
            LEFT JOIN pg_catalog.pg_roles AS grantee_role
              ON grantee_role.oid = acl_data.grantee
            WHERE namespace_data.nspname = :schema_name
              AND grantee_role.rolname = :grantee_name
              AND acl_data.privilege_type = 'CREATE'
            ORDER BY
                grantor_role.rolname,
                grantee_role.rolname,
                acl_data.privilege_type,
                acl_data.is_grantable
            """
        ),
        {
            "schema_name": _RB1M1A_SCHEMA,
            "grantee_name": _RB1M1A_TARGET_OWNER,
        },
    ).mappings().all()
    result = []
    for row in rows:
        if row["grantor_name"] is None or row["grantee_name"] is None:
            raise RuntimeError(
                "Revision-0024 found an ACL row with an unknown role."
            )
        result.append(
            (
                row["grantor_name"],
                row["grantee_name"],
                row["privilege_type"],
                bool(row["is_grantable"]),
            )
        )
    return tuple(result)


def _rb1m1a_verify_create_acl_rows(bind, expected, stage):
    observed = tuple(sorted(_rb1m1a_direct_create_acl_rows(bind)))
    expected = tuple(sorted(expected))
    if observed != expected:
        raise RuntimeError(
            f"Revision-0024 app_private CREATE ACL drift at {stage}: "
            f"observed={observed!r}, expected={expected!r}."
        )


def _rb1m1a_prepare_owner_transfer(bind):
    _rb1m1a_require_migration_owner(bind)
    before = tuple(sorted(_rb1m1a_direct_create_acl_rows(bind)))
    added_create = False
    expected = before
    if not _rb1m1a_has_schema_privilege(
        bind,
        _RB1M1A_TARGET_OWNER,
        "CREATE",
    ):
        bind.execute(
            sa.text(
                "GRANT CREATE ON SCHEMA app_private "
                "TO app_security_owner"
            )
        )
        expected = tuple(
            sorted(
                before
                + (
                    (
                        "migration_owner",
                        _RB1M1A_TARGET_OWNER,
                        "CREATE",
                        False,
                    ),
                )
            )
        )
        _rb1m1a_verify_create_acl_rows(
            bind,
            expected,
            "temporary CREATE grant",
        )
        added_create = True
    if not _rb1m1a_has_schema_privilege(
        bind,
        _RB1M1A_TARGET_OWNER,
        "CREATE",
    ):
        raise RuntimeError(
            "app_security_owner lacks effective CREATE on app_private "
            "after temporary capability preparation."
        )
    return {"before": before, "added_create": added_create}


def _rb1m1a_restore_owner_transfer(bind, state):
    _rb1m1a_require_migration_owner(bind)
    if state["added_create"]:
        bind.execute(
            sa.text(
                "REVOKE CREATE ON SCHEMA app_private "
                "FROM app_security_owner"
            )
        )
    _rb1m1a_verify_create_acl_rows(
        bind,
        state["before"],
        "owner-transfer restoration",
    )


def _rb1m1a_assert_function_owner(bind, signature):
    owner_name = bind.execute(
        sa.text(
            """
            SELECT owner_role.rolname::text
            FROM pg_catalog.pg_proc AS procedure_data
            JOIN pg_catalog.pg_roles AS owner_role
              ON owner_role.oid = procedure_data.proowner
            WHERE procedure_data.oid = pg_catalog.to_regprocedure(:signature)
            """
        ),
        {"signature": signature},
    ).scalar_one()
    if owner_name != _RB1M1A_TARGET_OWNER:
        raise RuntimeError(
            f"Unexpected owner for {signature!r}: {owner_name!r}."
        )


def _rb1m1a_verify_function_contracts(bind):
    rows = bind.execute(
        sa.text(
            """
            SELECT
                procedure_data.oid::regprocedure::text AS signature,
                owner_role.rolname::text AS owner_name,
                procedure_data.prosecdef AS security_definer,
                COALESCE(
                    array_to_string(procedure_data.proconfig, ','),
                    '<NULL>'
                ) AS function_config,
                (
                    SELECT count(*)
                    FROM pg_catalog.aclexplode(
                        COALESCE(
                            procedure_data.proacl,
                            pg_catalog.acldefault(
                                'f',
                                procedure_data.proowner
                            )
                        )
                    ) AS function_acl
                    WHERE function_acl.grantee = 0
                      AND function_acl.privilege_type = 'EXECUTE'
                ) AS public_execute_count
            FROM pg_catalog.pg_proc AS procedure_data
            JOIN pg_catalog.pg_roles AS owner_role
              ON owner_role.oid = procedure_data.proowner
            WHERE procedure_data.oid = ANY (
                ARRAY[
                    pg_catalog.to_regprocedure(
                        'app_private.touch_updated_at()'
                    ),
                    pg_catalog.to_regprocedure(
                        'app_private.enforce_membership_state_transition()'
                    )
                ]
            )
            ORDER BY signature
            """
        )
    ).mappings().all()
    if len(rows) != 2:
        raise RuntimeError(
            f"Expected two protected revision-0024 functions; found {len(rows)}."
        )
    for row in rows:
        if row["owner_name"] != _RB1M1A_TARGET_OWNER:
            raise RuntimeError(f"Function owner drift: {row!r}.")
        if row["security_definer"] is not True:
            raise RuntimeError(f"SECURITY DEFINER drift: {row!r}.")
        if row["function_config"] != "search_path=pg_catalog":
            raise RuntimeError(f"Function search_path drift: {row!r}.")
        if int(row["public_execute_count"]) != 0:
            raise RuntimeError(f"PUBLIC EXECUTE drift: {row!r}.")

    trigger_rows = bind.execute(
        sa.text(
            """
            SELECT
                trigger_data.tgname::text AS trigger_name,
                procedure_data.oid::regprocedure::text AS signature
            FROM pg_catalog.pg_trigger AS trigger_data
            JOIN pg_catalog.pg_proc AS procedure_data
              ON procedure_data.oid = trigger_data.tgfoid
            JOIN pg_catalog.pg_class AS relation_data
              ON relation_data.oid = trigger_data.tgrelid
            JOIN pg_catalog.pg_namespace AS namespace_data
              ON namespace_data.oid = relation_data.relnamespace
            WHERE namespace_data.nspname = 'public'
              AND relation_data.relname = 'organization_members'
              AND trigger_data.tgname IN (
                    'trg_touch_organization_members_updated_at',
                    'trg_membership_state_transition'
              )
            ORDER BY trigger_data.tgname
            """
        )
    ).all()
    expected = tuple(sorted(_RB1M1A_TRIGGER_MAP))
    observed = tuple(sorted((row[0], row[1]) for row in trigger_rows))
    if observed != expected:
        raise RuntimeError(
            "Revision-0024 trigger mapping drift: "
            f"observed={observed!r}, expected={expected!r}."
        )


def _rb1m1a_run_as_app_security_owner(bind, sql):
    _rb1m1a_require_migration_owner(bind)
    can_set = bind.execute(
        sa.text(
            """
            SELECT pg_catalog.pg_has_role(
                session_user,
                'app_security_owner',
                'SET'
            )
            """
        )
    ).scalar_one()
    if can_set is not True:
        raise RuntimeError(
            "migration_owner cannot SET ROLE app_security_owner."
        )
    bind.execute(sa.text("SET LOCAL ROLE app_security_owner"))
    identity = _rb1m1a_identity(bind)
    if identity["session_user_name"] != "migration_owner":
        raise RuntimeError("SET LOCAL ROLE changed session_user.")
    if identity["current_user_name"] != "app_security_owner":
        raise RuntimeError("SET LOCAL ROLE did not enter app_security_owner.")
    try:
        bind.execute(sa.text(sql))
    finally:
        bind.execute(sa.text("RESET ROLE"))
        _rb1m1a_require_migration_owner(bind)
# RB1M1A_0024_APP_PRIVATE_OWNER_CONTEXT_HELPERS_END


def upgrade() -> None:

    bind = op.get_bind()
    _rb1m1a_preflight(bind, require_functions=False)

    # ── 1. organization_members table ─────────────────────────────────────
    # References organization_users (existing user identity table) and
    # organizations. membership_status_id references the new ref table.
    op.execute("""
        CREATE TABLE public.organization_members (
            id                   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id               UUID        NOT NULL
                                 REFERENCES public.organizations(id) ON DELETE RESTRICT,
            user_id              UUID        NOT NULL
                                 REFERENCES public.organization_users(id) ON DELETE RESTRICT,
            membership_status_id SMALLINT    NOT NULL DEFAULT 1
                                 REFERENCES public.membership_statuses(id) ON DELETE RESTRICT,
            permission_version   BIGINT      NOT NULL DEFAULT 1,
            region_id            UUID        NULL,

            created_at           TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at           TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            deleted_at           TIMESTAMPTZ NULL,
            deleted_by           UUID        NULL
                                 REFERENCES public.organization_users(id) ON DELETE RESTRICT,

            -- Natural uniqueness: one membership record per user per org
            CONSTRAINT uq_org_member_user  UNIQUE (org_id, user_id),

            -- Composite candidate key: required for composite FKs from branch_staff_roles
            -- (organization_member_id, org_id) -> (id, org_id)
            CONSTRAINT uq_org_member_pair  UNIQUE (id, org_id)
        );
    """)

    # RB1M2U_0024_EXISTING_USER_SEED_START
    # organization_users predates organization_members and is already FORCE-RLS
    # protected. Seed the new tenancy identity under a transaction-local owner
    # maintenance window before enabling RLS on the new table.
    op.execute("""
        DO $rb1m2u_0024_preflight$
        DECLARE
            v_owner TEXT;
            v_rls BOOLEAN;
            v_force BOOLEAN;
        BEGIN
            IF session_user <> 'migration_owner'
               OR current_user <> 'migration_owner'
            THEN
                RAISE EXCEPTION
                    '0024 seed requires session_user=current_user=migration_owner'
                    USING ERRCODE = '42501';
            END IF;

            SELECT
                pg_catalog.pg_get_userbyid(c.relowner),
                c.relrowsecurity,
                c.relforcerowsecurity
            INTO v_owner, v_rls, v_force
            FROM pg_catalog.pg_class AS c
            WHERE c.oid = 'public.organization_users'::regclass;

            IF v_owner IS DISTINCT FROM 'migration_owner'
               OR v_rls IS NOT TRUE
               OR v_force IS NOT TRUE
            THEN
                RAISE EXCEPTION
                    '0024 predecessor organization_users security contract drift: '
                    'owner=%, rls=%, force=%',
                    v_owner, v_rls, v_force
                    USING ERRCODE = '42501';
            END IF;
        END
        $rb1m2u_0024_preflight$;
    """)

    op.execute(
        "LOCK TABLE public.organization_users "
        "IN SHARE ROW EXCLUSIVE MODE;"
    )
    op.execute(
        "ALTER TABLE public.organization_users "
        "NO FORCE ROW LEVEL SECURITY;"
    )

    op.execute("""
        INSERT INTO public.organization_members (
            org_id,
            user_id,
            membership_status_id,
            permission_version,
            created_at,
            updated_at,
            deleted_at,
            deleted_by
        )
        SELECT
            ou.org_id,
            ou.id,
            CASE
                WHEN ou.deleted_at IS NOT NULL THEN 5
                WHEN ou.is_active = TRUE THEN 3
                ELSE 4
            END,
            1,
            ou.created_at,
            ou.updated_at,
            ou.deleted_at,
            ou.deleted_by
        FROM public.organization_users AS ou;
    """)

    op.execute("""
        DO $rb1m2u_0024_verify$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM public.organization_users AS ou
                LEFT JOIN public.organization_members AS om
                  ON om.org_id = ou.org_id
                 AND om.user_id = ou.id
                WHERE om.id IS NULL
                   OR om.membership_status_id IS DISTINCT FROM
                      CASE
                          WHEN ou.deleted_at IS NOT NULL THEN 5
                          WHEN ou.is_active = TRUE THEN 3
                          ELSE 4
                      END
                   OR om.created_at IS DISTINCT FROM ou.created_at
                   OR om.updated_at IS DISTINCT FROM ou.updated_at
                   OR om.deleted_at IS DISTINCT FROM ou.deleted_at
                   OR om.deleted_by IS DISTINCT FROM ou.deleted_by
            ) THEN
                RAISE EXCEPTION
                    '0024 organization_members seed verification failed';
            END IF;

            IF (
                SELECT count(*) FROM public.organization_members
            ) <> (
                SELECT count(*) FROM public.organization_users
            ) THEN
                RAISE EXCEPTION
                    '0024 organization_members seed cardinality mismatch';
            END IF;
        END
        $rb1m2u_0024_verify$;
    """)

    op.execute(
        "ALTER TABLE public.organization_users "
        "FORCE ROW LEVEL SECURITY;"
    )
    # RB1M2U_0024_EXISTING_USER_SEED_END

    op.execute("""
        COMMENT ON TABLE public.organization_members IS
            'Core tenancy boundary: separates global identity (organization_users) '
            'from scoped authorization (branch_staff_roles). '
            'One row per user per organization. '
            'uq_org_member_pair supports composite FK from branch_staff_roles — do not drop.';
    """)

    op.execute("""
        COMMENT ON CONSTRAINT uq_org_member_pair ON public.organization_members IS
            'Required for composite FK (organization_member_id, org_id) from branch_staff_roles. Do not drop.';
    """)

    # ── 2. Indexes ────────────────────────────────────────────────────────

    # Primary access path: find active members in an org by user
    op.execute("""
        CREATE INDEX ix_org_members_active
        ON public.organization_members(org_id, user_id)
        WHERE deleted_at IS NULL;
    """)

    # Status filter (for suspension/revocation batch operations)
    op.execute("""
        CREATE INDEX ix_org_members_status
        ON public.organization_members(org_id, membership_status_id)
        WHERE deleted_at IS NULL;
    """)

    # ── 3. touch_updated_at trigger function ──────────────────────────────
    # Generic reusable trigger: updates updated_at on any row modification.
    # Owned by app_security_owner; revoked from PUBLIC.
    owner_context_state = _rb1m1a_prepare_owner_transfer(bind)

    op.execute("""
        CREATE OR REPLACE FUNCTION app_private.touch_updated_at()
        RETURNS TRIGGER
        STRICT
        VOLATILE
        PARALLEL UNSAFE
        SECURITY DEFINER
        SET search_path = pg_catalog
        LANGUAGE plpgsql
        AS $$
        BEGIN
            NEW.updated_at := clock_timestamp();
            RETURN NEW;
        END;
        $$;
    """)

    op.execute("REVOKE ALL ON FUNCTION app_private.touch_updated_at() FROM PUBLIC;")
    op.execute("""
        COMMENT ON FUNCTION app_private.touch_updated_at() IS
            'Generic BEFORE UPDATE trigger to auto-maintain updated_at column. '
            'Attach with: CREATE TRIGGER ... BEFORE UPDATE ON <table> FOR EACH ROW EXECUTE FUNCTION app_private.touch_updated_at()';
    """)

    op.execute("""
        CREATE TRIGGER trg_touch_organization_members_updated_at
            BEFORE UPDATE ON public.organization_members
            FOR EACH ROW
            EXECUTE FUNCTION app_private.touch_updated_at();
    """)
    op.execute("ALTER FUNCTION app_private.touch_updated_at() OWNER TO app_security_owner;")
    _rb1m1a_assert_function_owner(
        bind,
        "app_private.touch_updated_at()",
    )

    # ── 4. State machine transition guard ─────────────────────────────────
    # Enforces valid membership lifecycle transitions at the DB layer.
    # Terminal rule: revoked (id=5) cannot go back to pending (id=1).
    # Extend this function as more transition rules are needed.
    op.execute("""
        CREATE OR REPLACE FUNCTION app_private.enforce_membership_state_transition()
        RETURNS TRIGGER
        STRICT
        VOLATILE
        PARALLEL UNSAFE
        SECURITY DEFINER
        SET search_path = pg_catalog
        LANGUAGE plpgsql
        AS $$
        BEGIN
            -- Rule 1: revoked is terminal — cannot transition back to pending
            IF OLD.membership_status_id = 5 AND NEW.membership_status_id = 1 THEN
                RAISE EXCEPTION
                    'Invalid membership state transition: revoked (%) -> pending (%) is not permitted. '
                    'Revoked membership is terminal.',
                    OLD.membership_status_id, NEW.membership_status_id
                USING ERRCODE = 'check_violation';
            END IF;

            -- Rule 2: expired is terminal — cannot be reactivated directly
            IF OLD.membership_status_id = 6 AND NEW.membership_status_id = 3 THEN
                RAISE EXCEPTION
                    'Invalid membership state transition: expired (%) -> active (%) is not permitted. '
                    'Create a new membership record instead.',
                    OLD.membership_status_id, NEW.membership_status_id
                USING ERRCODE = 'check_violation';
            END IF;

            RETURN NEW;
        END;
        $$;
    """)

    op.execute("REVOKE ALL ON FUNCTION app_private.enforce_membership_state_transition() FROM PUBLIC;")
    op.execute("""
        COMMENT ON FUNCTION app_private.enforce_membership_state_transition() IS
            'State machine guard for organization_members.membership_status_id. '
            'Terminal states: revoked (5) and expired (6) cannot be directly reactivated. '
            'Extend this function to add more transition rules.';
    """)

    op.execute("""
        CREATE TRIGGER trg_membership_state_transition
            BEFORE UPDATE OF membership_status_id ON public.organization_members
            FOR EACH ROW
            WHEN (OLD.membership_status_id IS DISTINCT FROM NEW.membership_status_id)
            EXECUTE FUNCTION app_private.enforce_membership_state_transition();
    """)
    op.execute("ALTER FUNCTION app_private.enforce_membership_state_transition() OWNER TO app_security_owner;")
    _rb1m1a_assert_function_owner(
        bind,
        "app_private.enforce_membership_state_transition()",
    )

    _rb1m1a_restore_owner_transfer(bind, owner_context_state)
    _rb1m1a_verify_function_contracts(bind)

    # ── 5. RLS policies ───────────────────────────────────────────────────
    op.execute("ALTER TABLE public.organization_members ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.organization_members FORCE ROW LEVEL SECURITY;")

    # Fail-closed: current_setting(..., false) raises an error if GUC is not set.
    # deleted_at IS NULL enforces soft-delete in both read and write paths.
    op.execute("""
        CREATE POLICY tenant_isolation_organization_members
        ON public.organization_members
        FOR ALL
        USING (
            org_id    = current_setting('app.current_org_id', false)::uuid
            AND deleted_at IS NULL
        )
        WITH CHECK (
            org_id    = current_setting('app.current_org_id', false)::uuid
            AND deleted_at IS NULL
        );
    """)

    # ── 6. Grants ─────────────────────────────────────────────────────────
    op.execute("""
        GRANT SELECT, INSERT, UPDATE ON public.organization_members
        TO app_runtime;
    """)
    # audit_writer needs read access to snapshot actor membership details
    op.execute("GRANT SELECT ON public.organization_members TO audit_writer;")
    op.execute("GRANT SELECT ON public.organization_members TO readonly_analytics;")

    # Grant sequence for BIGINT permission_version (not a serial, but good practice)
    # No sequence needed — permission_version is a plain BIGINT updated by app.


def downgrade() -> None:
    bind = op.get_bind()
    _rb1m1a_preflight(bind, require_functions=True)

    # Both relations are FORCE-RLS protected at this boundary. The migration
    # owner must inspect the complete cross-tenant dataset to prove that the
    # 0024 state is losslessly representable by 0023. Validate the exact owner
    # and RLS posture before opening a transaction-local owner maintenance
    # window; RLS remains enabled throughout and FORCE is restored before any
    # teardown continues.
    op.execute("""
        DO $rb1m1a_downgrade_rls_preflight$
        DECLARE
            relation_name TEXT;
            owner_name TEXT;
            rls_enabled BOOLEAN;
            force_rls BOOLEAN;
        BEGIN
            IF session_user <> 'migration_owner'
               OR current_user <> 'migration_owner'
            THEN
                RAISE EXCEPTION
                    '0024 downgrade inspection requires session_user=current_user=migration_owner'
                    USING ERRCODE = '42501';
            END IF;

            FOREACH relation_name IN ARRAY ARRAY[
                'organization_users',
                'organization_members'
            ]
            LOOP
                SELECT
                    pg_catalog.pg_get_userbyid(c.relowner),
                    c.relrowsecurity,
                    c.relforcerowsecurity
                INTO owner_name, rls_enabled, force_rls
                FROM pg_catalog.pg_class AS c
                JOIN pg_catalog.pg_namespace AS n
                  ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relname = relation_name
                  AND c.relkind = 'r';

                IF owner_name IS DISTINCT FROM 'migration_owner'
                   OR rls_enabled IS NOT TRUE
                   OR force_rls IS NOT TRUE
                THEN
                    RAISE EXCEPTION
                        '0024 downgrade RLS contract drift for public.%: owner=%, rls=%, force=%',
                        relation_name, owner_name, rls_enabled, force_rls
                        USING ERRCODE = '42501';
                END IF;
            END LOOP;
        END
        $rb1m1a_downgrade_rls_preflight$;
    """)

    op.execute(
        "LOCK TABLE public.organization_users, public.organization_members "
        "IN SHARE ROW EXCLUSIVE MODE;"
    )
    op.execute(
        "ALTER TABLE public.organization_users "
        "NO FORCE ROW LEVEL SECURITY;"
    )
    op.execute(
        "ALTER TABLE public.organization_members "
        "NO FORCE ROW LEVEL SECURITY;"
    )

    # organization_members was backfilled from predecessor-owned
    # organization_users. Crossing back to 0023 is lossless only while every
    # row is still exactly derivable from that predecessor state. Any extra
    # membership, status/permission change, region assignment, or independent
    # lifecycle timestamp would otherwise be silently discarded.
    op.execute("""
        DO $rb1m1a_downgrade_data_contract$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM public.organization_users AS ou
                FULL OUTER JOIN public.organization_members AS om
                  ON om.org_id = ou.org_id
                 AND om.user_id = ou.id
                WHERE ou.id IS NULL
                   OR om.id IS NULL
                   OR om.membership_status_id IS DISTINCT FROM
                      CASE
                          WHEN ou.deleted_at IS NOT NULL THEN 5
                          WHEN ou.is_active = TRUE THEN 3
                          ELSE 4
                      END
                   OR om.permission_version IS DISTINCT FROM 1
                   OR om.region_id IS NOT NULL
                   OR om.created_at IS DISTINCT FROM ou.created_at
                   OR om.updated_at IS DISTINCT FROM ou.updated_at
                   OR om.deleted_at IS DISTINCT FROM ou.deleted_at
                   OR om.deleted_by IS DISTINCT FROM ou.deleted_by
            ) THEN
                RAISE EXCEPTION
                    '0024 downgrade blocked: organization_members contains state not representable by predecessor organization_users';
            END IF;
        END
        $rb1m1a_downgrade_data_contract$;
    """)

    op.execute(
        "ALTER TABLE public.organization_users "
        "FORCE ROW LEVEL SECURITY;"
    )
    op.execute(
        "ALTER TABLE public.organization_members "
        "FORCE ROW LEVEL SECURITY;"
    )

    op.execute("""
        DO $rb1m1a_downgrade_rls_restore$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM pg_catalog.pg_class AS c
                JOIN pg_catalog.pg_namespace AS n
                  ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relname IN ('organization_users', 'organization_members')
                  AND c.relkind = 'r'
                  AND (
                      pg_catalog.pg_get_userbyid(c.relowner) <> 'migration_owner'
                      OR c.relrowsecurity IS NOT TRUE
                      OR c.relforcerowsecurity IS NOT TRUE
                  )
            ) OR (
                SELECT count(*)
                FROM pg_catalog.pg_class AS c
                JOIN pg_catalog.pg_namespace AS n
                  ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relname IN ('organization_users', 'organization_members')
                  AND c.relkind = 'r'
            ) <> 2 THEN
                RAISE EXCEPTION
                    '0024 downgrade failed to restore exact FORCE-RLS owner posture'
                    USING ERRCODE = '42501';
            END IF;
        END
        $rb1m1a_downgrade_rls_restore$;
    """)

    op.execute("DROP TRIGGER IF EXISTS trg_membership_state_transition ON public.organization_members;")
    op.execute("DROP TRIGGER IF EXISTS trg_touch_organization_members_updated_at ON public.organization_members;")

    op.execute("DROP POLICY IF EXISTS tenant_isolation_organization_members ON public.organization_members;")

    op.execute("DROP INDEX IF EXISTS ix_org_members_status;")
    op.execute("DROP INDEX IF EXISTS ix_org_members_active;")

    # RESTRICT makes any unmodelled dependency a hard rollback failure.
    op.execute("DROP TABLE public.organization_members RESTRICT;")

    _rb1m1a_run_as_app_security_owner(
        bind,
        "DROP FUNCTION IF EXISTS "
        "app_private.enforce_membership_state_transition();",
    )
    _rb1m1a_run_as_app_security_owner(
        bind,
        "DROP FUNCTION IF EXISTS app_private.touch_updated_at();",
    )
