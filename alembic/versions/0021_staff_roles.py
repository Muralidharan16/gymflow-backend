"""Branch Staff Role Assignment Subsystem

Revision ID: 0021_staff_roles
Revises: 0020_contacts_hardened
Create Date: 2026-05-23

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '0021_staff_roles'
down_revision = '0020_contacts_hardened'
branch_labels = None
depends_on = None


# RB1L8D1A_APP_PRIVATE_OWNER_CONTEXT_HELPERS_START
# Frozen revision-local contract. Do not import this logic from mutable shared code.
_RB1L8D1A_TARGET_OWNER = "app_rls_executor"
_RB1L8D1A_SCHEMA = "app_private"
_RB1L8D1A_FUNCTIONS = (
    "app_private.handle_user_deactivation_cascade()",
    "app_private.log_branch_staff_role_audit()",
)


def _rb1l8d1a_bind():
    return op.get_bind()


def _rb1l8d1a_identity(bind):
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


def _rb1l8d1a_require_migration_owner(bind):
    identity = _rb1l8d1a_identity(bind)
    if identity["session_user_name"] != "migration_owner":
        raise RuntimeError(
            "RB1L8D1A requires session_user=migration_owner; "
            f"observed {identity['session_user_name']!r}."
        )
    if identity["current_user_name"] != "migration_owner":
        raise RuntimeError(
            "RB1L8D1A requires current_user=migration_owner; "
            f"observed {identity['current_user_name']!r}."
        )


def _rb1l8d1a_require_set_capability(bind, role_name):
    allowed = bind.execute(
        sa.text(
            """
            SELECT pg_catalog.pg_has_role(
                current_user,
                CAST(:role_name AS name),
                'SET'
            )
            """
        ),
        {"role_name": role_name},
    ).scalar_one()
    if allowed is not True:
        raise RuntimeError(
            "migration_owner lacks SET capability to " f"{role_name}."
        )


def _rb1l8d1a_has_schema_privilege(bind, role_name, privilege):
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
            "schema_name": _RB1L8D1A_SCHEMA,
            "privilege": privilege,
        },
    ).scalar_one() is True


def _rb1l8d1a_require_migration_owner_schema_capabilities(bind):
    for privilege in ("CREATE", "USAGE"):
        if not _rb1l8d1a_has_schema_privilege(
            bind,
            "migration_owner",
            privilege,
        ):
            raise RuntimeError(
                "migration_owner lacks effective "
                f"{privilege} on app_private."
            )


def _rb1l8d1a_direct_acl_rows(bind, grantee_name):
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
              AND acl_data.grantee = (
                    SELECT role_data.oid
                    FROM pg_catalog.pg_roles AS role_data
                    WHERE role_data.rolname = :grantee_name
              )
              AND acl_data.privilege_type IN ('CREATE', 'USAGE')
            ORDER BY
                grantor_role.rolname,
                grantee_role.rolname,
                acl_data.privilege_type,
                acl_data.is_grantable
            """
        ),
        {
            "schema_name": _RB1L8D1A_SCHEMA,
            "grantee_name": grantee_name,
        },
    ).mappings().all()
    result = []
    for row in rows:
        if row["grantor_name"] is None or row["grantee_name"] is None:
            raise RuntimeError(
                "RB1L8D1A found an ACL row with an unknown grantor or grantee."
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


def _rb1l8d1a_public_create_rows(bind):
    rows = bind.execute(
        sa.text(
            """
            SELECT
                grantor_role.rolname::text AS grantor_name,
                acl_data.privilege_type::text AS privilege_type,
                acl_data.is_grantable AS is_grantable
            FROM pg_catalog.pg_namespace AS namespace_data
            CROSS JOIN LATERAL pg_catalog.aclexplode(
                namespace_data.nspacl
            ) AS acl_data
            LEFT JOIN pg_catalog.pg_roles AS grantor_role
              ON grantor_role.oid = acl_data.grantor
            WHERE namespace_data.nspname = :schema_name
              AND acl_data.grantee = 0
              AND acl_data.privilege_type = 'CREATE'
            ORDER BY grantor_role.rolname, acl_data.is_grantable
            """
        ),
        {"schema_name": _RB1L8D1A_SCHEMA},
    ).all()
    return tuple(rows)


def _rb1l8d1a_reject_public_create(bind):
    rows = _rb1l8d1a_public_create_rows(bind)
    if rows:
        raise RuntimeError(
            "PUBLIC has direct/effective CREATE on app_private; "
            f"refusing migration mutation: {rows!r}."
        )


def _rb1l8d1a_expected_acl_delta(before, privilege):
    return tuple(
        sorted(
            before
            + (
                (
                    "migration_owner",
                    _RB1L8D1A_TARGET_OWNER,
                    privilege,
                    False,
                ),
            )
        )
    )


def _rb1l8d1a_verify_exact_acl_rows(bind, expected, stage):
    observed = tuple(
        sorted(_rb1l8d1a_direct_acl_rows(bind, _RB1L8D1A_TARGET_OWNER))
    )
    expected = tuple(sorted(expected))
    if observed != expected:
        raise RuntimeError(
            f"RB1L8D1A direct ACL drift at {stage}: "
            f"observed={observed!r}, expected={expected!r}."
        )


def _rb1l8d1a_preflight_upgrade_owner_context(bind):
    _rb1l8d1a_require_migration_owner(bind)
    _rb1l8d1a_require_set_capability(bind, _RB1L8D1A_TARGET_OWNER)
    _rb1l8d1a_require_migration_owner_schema_capabilities(bind)
    _rb1l8d1a_reject_public_create(bind)


def _rb1l8d1a_prepare_upgrade_owner_context(bind):
    _rb1l8d1a_preflight_upgrade_owner_context(bind)

    before = tuple(
        sorted(_rb1l8d1a_direct_acl_rows(bind, _RB1L8D1A_TARGET_OWNER))
    )
    added_usage = False
    added_create = False
    current = before

    if not _rb1l8d1a_has_schema_privilege(
        bind,
        _RB1L8D1A_TARGET_OWNER,
        "USAGE",
    ):
        bind.execute(
            sa.text(
                "GRANT USAGE ON SCHEMA app_private "
                "TO app_rls_executor"
            )
        )
        current = _rb1l8d1a_expected_acl_delta(current, "USAGE")
        _rb1l8d1a_verify_exact_acl_rows(
            bind,
            current,
            "temporary USAGE grant",
        )
        added_usage = True

    direct_create = [row for row in current if row[2] == "CREATE"]
    if not direct_create:
        bind.execute(
            sa.text(
                "GRANT CREATE ON SCHEMA app_private "
                "TO app_rls_executor"
            )
        )
        current = _rb1l8d1a_expected_acl_delta(current, "CREATE")
        _rb1l8d1a_verify_exact_acl_rows(
            bind,
            current,
            "temporary CREATE grant",
        )
        added_create = True

    return {
        "before": before,
        "added_usage": added_usage,
        "added_create": added_create,
    }


def _rb1l8d1a_require_target_owner_capabilities(bind):
    for privilege in ("CREATE", "USAGE"):
        if not _rb1l8d1a_has_schema_privilege(
            bind,
            _RB1L8D1A_TARGET_OWNER,
            privilege,
        ):
            raise RuntimeError(
                f"{_RB1L8D1A_TARGET_OWNER} lacks effective "
                f"{privilege} on app_private."
            )


def _rb1l8d1a_execute_as_owner(bind, sql):
    _rb1l8d1a_require_migration_owner(bind)
    _rb1l8d1a_require_set_capability(bind, _RB1L8D1A_TARGET_OWNER)
    bind.execute(sa.text("SET LOCAL ROLE app_rls_executor"))
    identity = _rb1l8d1a_identity(bind)
    if identity["session_user_name"] != "migration_owner":
        raise RuntimeError("SET LOCAL ROLE changed session_user unexpectedly.")
    if identity["current_user_name"] != _RB1L8D1A_TARGET_OWNER:
        raise RuntimeError("SET LOCAL ROLE did not enter app_rls_executor.")
    bind.execute(sa.text(sql))
    bind.execute(sa.text("RESET ROLE"))
    _rb1l8d1a_require_migration_owner(bind)


def _rb1l8d1a_finalize_upgrade_owner_context(bind, state):
    _rb1l8d1a_require_migration_owner(bind)
    if state["added_create"]:
        bind.execute(
            sa.text(
                "REVOKE CREATE ON SCHEMA app_private "
                "FROM app_rls_executor"
            )
        )
    if state["added_usage"]:
        bind.execute(
            sa.text(
                "REVOKE USAGE ON SCHEMA app_private "
                "FROM app_rls_executor"
            )
        )
    _rb1l8d1a_verify_exact_acl_rows(
        bind,
        state["before"],
        "upgrade owner-context cleanup",
    )
    _rb1l8d1a_reject_public_create(bind)


def _rb1l8d1a_resolve_function_owner(bind, signature):
    row = bind.execute(
        sa.text(
            """
            SELECT
                procedure_data.oid AS procedure_oid,
                owner_role.rolname::text AS owner_name
            FROM pg_catalog.pg_proc AS procedure_data
            JOIN pg_catalog.pg_roles AS owner_role
              ON owner_role.oid = procedure_data.proowner
            WHERE procedure_data.oid = pg_catalog.to_regprocedure(:signature)
            """
        ),
        {"signature": signature},
    ).mappings().all()
    if len(row) != 1:
        raise RuntimeError(
            f"Expected exactly one function for {signature!r}; "
            f"observed {len(row)}."
        )
    owner_name = row[0]["owner_name"]
    if owner_name != _RB1L8D1A_TARGET_OWNER:
        raise RuntimeError(
            f"Unexpected owner for {signature!r}: {owner_name!r}."
        )
    return owner_name


def _rb1l8d1a_drop_owned_functions(bind):
    _rb1l8d1a_require_migration_owner(bind)
    _rb1l8d1a_require_set_capability(bind, _RB1L8D1A_TARGET_OWNER)
    _rb1l8d1a_reject_public_create(bind)

    before = tuple(
        sorted(_rb1l8d1a_direct_acl_rows(bind, _RB1L8D1A_TARGET_OWNER))
    )
    for signature in _RB1L8D1A_FUNCTIONS:
        _rb1l8d1a_resolve_function_owner(bind, signature)

    added_usage = False
    current = before
    if not _rb1l8d1a_has_schema_privilege(
        bind,
        _RB1L8D1A_TARGET_OWNER,
        "USAGE",
    ):
        bind.execute(
            sa.text(
                "GRANT USAGE ON SCHEMA app_private "
                "TO app_rls_executor"
            )
        )
        current = _rb1l8d1a_expected_acl_delta(current, "USAGE")
        _rb1l8d1a_verify_exact_acl_rows(
            bind,
            current,
            "downgrade temporary USAGE grant",
        )
        added_usage = True

    bind.execute(sa.text("SET LOCAL ROLE app_rls_executor"))
    identity = _rb1l8d1a_identity(bind)
    if identity["session_user_name"] != "migration_owner":
        raise RuntimeError("Downgrade SET LOCAL ROLE changed session_user.")
    if identity["current_user_name"] != _RB1L8D1A_TARGET_OWNER:
        raise RuntimeError("Downgrade did not enter app_rls_executor.")
    bind.execute(
        sa.text(
            "DROP FUNCTION "
            "app_private.log_branch_staff_role_audit()"
        )
    )
    bind.execute(
        sa.text(
            "DROP FUNCTION "
            "app_private.handle_user_deactivation_cascade()"
        )
    )
    bind.execute(sa.text("RESET ROLE"))
    _rb1l8d1a_require_migration_owner(bind)

    if added_usage:
        bind.execute(
            sa.text(
                "REVOKE USAGE ON SCHEMA app_private "
                "FROM app_rls_executor"
            )
        )
    _rb1l8d1a_verify_exact_acl_rows(
        bind,
        before,
        "downgrade owner-context cleanup",
    )
    _rb1l8d1a_reject_public_create(bind)
# RB1L8D1A_APP_PRIVATE_OWNER_CONTEXT_HELPERS_END


def upgrade():
    preflight_bind = _rb1l8d1a_bind()
    _rb1l8d1a_preflight_upgrade_owner_context(preflight_bind)

    # 1. Create lookup enum for branch staff roles
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'branch_staff_role_enum') THEN
                CREATE TYPE public.branch_staff_role_enum AS ENUM ('manager', 'trainer', 'receptionist', 'auditor');
            END IF;
        END$$;
    """)

    # 2. Create organization_users table
    op.execute("""
        CREATE TABLE public.organization_users (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id UUID NOT NULL REFERENCES public.organizations(id) ON DELETE RESTRICT,
            name VARCHAR(120) NOT NULL,
            email CITEXT NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            phone VARCHAR(20) NULL,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            is_verified BOOLEAN NOT NULL DEFAULT FALSE,
            token_version INT NOT NULL DEFAULT 1,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            deleted_at TIMESTAMPTZ NULL,
            deleted_by UUID NULL,
            CONSTRAINT uq_org_users_email UNIQUE (org_id, email),
            CONSTRAINT uq_org_users_pair UNIQUE (id, org_id)
        );
    """)

    # 3. Create branch_staff_roles table
    op.execute("""
        CREATE TABLE public.branch_staff_roles (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id UUID NOT NULL,
            branch_id UUID NOT NULL,
            user_id UUID NOT NULL,
            role public.branch_staff_role_enum NOT NULL,
            assigned_by UUID NULL,
            assigned_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            effective_from TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            effective_to TIMESTAMPTZ NULL,
            revoked_at TIMESTAMPTZ NULL,
            revoked_by UUID NULL,
            metadata JSONB NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            deleted_at TIMESTAMPTZ NULL,
            deleted_by UUID NULL,
            
            CONSTRAINT chk_temporal_bounds CHECK (effective_to IS NULL OR effective_from < effective_to),
            CONSTRAINT chk_revocation_info CHECK (
                (revoked_at IS NULL AND revoked_by IS NULL) OR
                (revoked_at IS NOT NULL)
            ),
            CONSTRAINT fk_branch_staff_branch_org FOREIGN KEY (branch_id, org_id) REFERENCES public.org_branches(id, org_id) ON DELETE RESTRICT,
            CONSTRAINT fk_branch_staff_user_org FOREIGN KEY (user_id, org_id) REFERENCES public.organization_users(id, org_id) ON DELETE RESTRICT,
            CONSTRAINT fk_branch_staff_assigned_by FOREIGN KEY (assigned_by, org_id) REFERENCES public.organization_users(id, org_id) ON DELETE SET NULL,
            CONSTRAINT fk_branch_staff_revoked_by FOREIGN KEY (revoked_by, org_id) REFERENCES public.organization_users(id, org_id) ON DELETE SET NULL
        );
    """)

    # 4. Enable btree_gist extension and create overlap exclusion constraint
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist;")
    op.execute("""
        ALTER TABLE public.branch_staff_roles
        ADD CONSTRAINT exclude_overlapping_staff_assignments
        EXCLUDE USING gist (
            branch_id WITH =,
            user_id WITH =,
            role WITH =,
            tstzrange(effective_from, COALESCE(effective_to, 'infinity'::timestamptz)) WITH &&
        )
        WHERE (deleted_at IS NULL AND revoked_at IS NULL);
    """)

    # 5. Create transactional partial indexes
    # These tables are created in this revision and are empty here, so keeping
    # index creation in the revision transaction preserves failure atomicity.
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS ix_org_users_email_lower_active
        ON public.organization_users (email) WHERE (deleted_at IS NULL);
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_branch_staff_user_active
        ON public.branch_staff_roles (user_id, role)
        WHERE (deleted_at IS NULL AND revoked_at IS NULL);
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_branch_staff_branch_active
        ON public.branch_staff_roles (branch_id, role)
        WHERE (deleted_at IS NULL AND revoked_at IS NULL);
    """)

    # 6. Setup RLS Policies
    op.execute("ALTER TABLE public.organization_users ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.organization_users FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.branch_staff_roles ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.branch_staff_roles FORCE ROW LEVEL SECURITY;")

    op.execute("""
        CREATE POLICY tenant_isolation_org_users ON public.organization_users
        FOR ALL
        USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID)
        WITH CHECK (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID);
    """)

    op.execute("""
        CREATE POLICY tenant_isolation_staff_roles ON public.branch_staff_roles
        FOR ALL
        USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID)
        WITH CHECK (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID);
    """)

    # 7. Grant Permissions to app_rls_executor and app_user (if roles exist)
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON public.organization_users TO app_rls_executor;")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON public.branch_staff_roles TO app_rls_executor;")
    op.execute("GRANT SELECT, INSERT, UPDATE ON public.organization_users TO app_user;")
    op.execute("GRANT SELECT, INSERT, UPDATE ON public.branch_staff_roles TO app_user;")

    # 8. Create trigger functions
    bind = _rb1l8d1a_bind()
    owner_state = _rb1l8d1a_prepare_upgrade_owner_context(bind)
    op.execute("""
        CREATE OR REPLACE FUNCTION app_private.handle_user_deactivation_cascade()
        RETURNS TRIGGER 
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $$
        BEGIN
            IF OLD.is_active = TRUE AND NEW.is_active = FALSE THEN
                UPDATE public.branch_staff_roles
                SET revoked_at = clock_timestamp(),
                    revoked_by = NULLIF(current_setting('app.current_user_id', true), '')::UUID
                WHERE user_id = NEW.id
                  AND revoked_at IS NULL
                  AND deleted_at IS NULL;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER trg_user_deactivation_cascade
            AFTER UPDATE OF is_active ON public.organization_users
            FOR EACH ROW
            WHEN (NEW.is_active = FALSE)
            EXECUTE FUNCTION app_private.handle_user_deactivation_cascade();
    """)
    _rb1l8d1a_require_target_owner_capabilities(bind)
    op.execute("ALTER FUNCTION app_private.handle_user_deactivation_cascade() OWNER TO app_rls_executor;")
    _rb1l8d1a_execute_as_owner(
        bind,
        "REVOKE ALL ON FUNCTION "
        "app_private.handle_user_deactivation_cascade() "
        "FROM PUBLIC",
    )

    op.execute("""
        CREATE OR REPLACE FUNCTION app_private.log_branch_staff_role_audit()
        RETURNS TRIGGER 
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $$
        DECLARE
            current_actor UUID := NULLIF(current_setting('app.current_user_id', true), '')::UUID;
            audit_action TEXT;
            audit_diff JSONB;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                audit_action := 'staff_role_assigned';
                audit_diff := jsonb_build_object(
                    'role_assignment_id', NEW.id,
                    'user_id', NEW.user_id,
                    'role', NEW.role,
                    'effective_from', NEW.effective_from,
                    'effective_to', NEW.effective_to
                );
                
                INSERT INTO public.branch_audit_log (
                    branch_id, org_id, actor_id, action, reason, diff, created_at
                ) VALUES (
                    NEW.branch_id,
                    NEW.org_id,
                    COALESCE(current_actor, NEW.assigned_by),
                    audit_action,
                    'Staff role assigned to branch',
                    audit_diff,
                    clock_timestamp()
                );
            ELSIF TG_OP = 'UPDATE' THEN
                -- Log revocation
                IF OLD.revoked_at IS NULL AND NEW.revoked_at IS NOT NULL THEN
                    audit_action := 'staff_role_revoked';
                    audit_diff := jsonb_build_object(
                        'role_assignment_id', NEW.id,
                        'user_id', NEW.user_id,
                        'role', NEW.role,
                        'revoked_at', NEW.revoked_at,
                        'revoked_by', NEW.revoked_by
                    );
                    
                    INSERT INTO public.branch_audit_log (
                        branch_id, org_id, actor_id, action, reason, diff, created_at
                    ) VALUES (
                        NEW.branch_id,
                        NEW.org_id,
                        COALESCE(current_actor, NEW.revoked_by),
                        audit_action,
                        'Staff role assignment revoked',
                        audit_diff,
                        clock_timestamp()
                    );
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER trg_audit_branch_staff_roles
            AFTER INSERT OR UPDATE ON public.branch_staff_roles
            FOR EACH ROW
            EXECUTE FUNCTION app_private.log_branch_staff_role_audit();
    """)
    _rb1l8d1a_require_target_owner_capabilities(bind)
    op.execute("ALTER FUNCTION app_private.log_branch_staff_role_audit() OWNER TO app_rls_executor;")
    _rb1l8d1a_execute_as_owner(
        bind,
        "REVOKE ALL ON FUNCTION "
        "app_private.log_branch_staff_role_audit() "
        "FROM PUBLIC",
    )
    _rb1l8d1a_finalize_upgrade_owner_context(bind, owner_state)

    # 9. Triggers were created before function ownership transfer in section 8.


def downgrade():
    # Neither organization_users nor branch_staff_roles exists in the 0020
    # predecessor. Any row therefore represents business/authz state that the
    # predecessor cannot encode. Refuse to destroy it implicitly.
    op.execute("""
        DO $rb1l8d1a_downgrade_data_contract$
        BEGIN
            IF EXISTS (SELECT 1 FROM public.branch_staff_roles LIMIT 1) THEN
                RAISE EXCEPTION
                    '0021 downgrade blocked: public.branch_staff_roles contains data not representable by predecessor';
            END IF;
            IF EXISTS (SELECT 1 FROM public.organization_users LIMIT 1) THEN
                RAISE EXCEPTION
                    '0021 downgrade blocked: public.organization_users contains data not representable by predecessor';
            END IF;
        END
        $rb1l8d1a_downgrade_data_contract$;
    """)

    op.execute("DROP TRIGGER IF EXISTS trg_audit_branch_staff_roles ON public.branch_staff_roles;")
    op.execute("DROP TRIGGER IF EXISTS trg_user_deactivation_cascade ON public.organization_users;")
    
    _rb1l8d1a_drop_owned_functions(_rb1l8d1a_bind())
    
    op.execute("DROP POLICY IF EXISTS tenant_isolation_staff_roles ON public.branch_staff_roles;")
    op.execute("DROP POLICY IF EXISTS tenant_isolation_org_users ON public.organization_users;")

    # RESTRICT is deliberate: an unknown later/external dependency must stop
    # rollback instead of being erased implicitly.
    op.execute("DROP TABLE public.branch_staff_roles RESTRICT;")
    op.execute("DROP TABLE public.organization_users RESTRICT;")
    op.execute("DROP TYPE public.branch_staff_role_enum RESTRICT;")
