"""RBAC Hardening Phase 2 — Reference Tables (Replacing ENUMs)

Phase 2 of the v18.0 hardening plan.

Creates:
  • public.staff_roles          — role registry (replaces branch_staff_role_enum)
  • public.scope_types          — scope registry (org / branch / region / global)
  • public.membership_statuses  — membership lifecycle states
  • public.permissions          — permission code registry
  • public.audit_key_registry   — cryptographic key rotation governance

Design choices:
  • All code columns enforce lowercase via CHECK constraint (no CITEXT dependency).
  • All tables seeded with system data in this migration.
  • These tables are intentionally small (SMALLINT PKs) — designed for hot cache.
  • No ENUMs used anywhere. Future values added via INSERT, zero-downtime.

Does NOT touch existing branch_staff_roles or organization_users tables yet.
That is Phase 4 (Expand step of expand/contract migration).

Revision ID: 0023_rbac_p2_ref_tables
Revises: 0022_rbac_p1_roles
Create Date: 2026-05-23
"""

from alembic import op

revision = "0023_rbac_p2_ref_tables"
down_revision = "0022_rbac_p1_roles"
branch_labels = None
depends_on = None


def upgrade() -> None:

    # ── 1. staff_roles ────────────────────────────────────────────────────
    # Canonical role registry. Replaces branch_staff_role_enum.
    # hierarchy_level drives RLS permission comparisons (e.g. >= 80 for admin).
    op.execute("""
        CREATE TABLE public.staff_roles (
            id              SMALLINT    PRIMARY KEY,
            code            VARCHAR(32) UNIQUE NOT NULL,
            hierarchy_level SMALLINT    NOT NULL,
            is_system       BOOLEAN     NOT NULL DEFAULT TRUE,
            CONSTRAINT chk_staff_role_code_lower CHECK (code = lower(code)),
            CONSTRAINT chk_staff_role_level_range CHECK (hierarchy_level BETWEEN 1 AND 100)
        );
    """)

    op.execute("""
        COMMENT ON TABLE public.staff_roles IS
            'Canonical role registry. Replaces branch_staff_role_enum. '
            'Add new roles via INSERT only — never ALTER TYPE. '
            'hierarchy_level used in RLS GUC comparisons.';
    """)

    op.execute("""
        INSERT INTO public.staff_roles (id, code, hierarchy_level, is_system) VALUES
            (1, 'owner',        100, TRUE),
            (2, 'admin',         80, TRUE),
            (3, 'manager',       60, TRUE),
            (4, 'trainer',       40, TRUE),
            (5, 'receptionist',  20, TRUE),
            (6, 'auditor',       10, TRUE);
    """)

    # Grant: app_runtime reads only; no writes (system table mutated via migration)
    op.execute("GRANT SELECT ON public.staff_roles TO app_runtime, audit_writer, readonly_analytics;")

    # ── 2. scope_types ────────────────────────────────────────────────────
    # Scope of a role assignment: branch-level, org-wide, region, or global.
    op.execute("""
        CREATE TABLE public.scope_types (
            id   SMALLINT    PRIMARY KEY,
            code VARCHAR(32) UNIQUE NOT NULL,
            CONSTRAINT chk_scope_type_code_lower CHECK (code = lower(code))
        );
    """)

    op.execute("""
        COMMENT ON TABLE public.scope_types IS
            'Role assignment scopes. Default scope is branch (id=2). '
            'region and global reserved for future franchise/multi-region use.';
    """)

    op.execute("""
        INSERT INTO public.scope_types (id, code) VALUES
            (1, 'organization'),
            (2, 'branch'),
            (3, 'region'),
            (4, 'global');
    """)

    op.execute("GRANT SELECT ON public.scope_types TO app_runtime, audit_writer, readonly_analytics;")

    # ── 3. membership_statuses ────────────────────────────────────────────
    # Valid lifecycle states for organization_members (created in Phase 3).
    # Order matters for state-machine trigger logic.
    op.execute("""
        CREATE TABLE public.membership_statuses (
            id   SMALLINT    PRIMARY KEY,
            code VARCHAR(32) UNIQUE NOT NULL,
            CONSTRAINT chk_membership_status_code_lower CHECK (code = lower(code))
        );
    """)

    op.execute("""
        COMMENT ON TABLE public.membership_statuses IS
            'Membership lifecycle states. '
            'Valid transitions enforced by trg_membership_state_transition trigger (Phase 3). '
            'id=5 (revoked) is terminal — no re-activation permitted.';
    """)

    op.execute("""
        INSERT INTO public.membership_statuses (id, code) VALUES
            (1, 'pending'),
            (2, 'invited'),
            (3, 'active'),
            (4, 'suspended'),
            (5, 'revoked'),
            (6, 'expired');
    """)

    op.execute("GRANT SELECT ON public.membership_statuses TO app_runtime, audit_writer, readonly_analytics;")

    # ── 4. permissions ────────────────────────────────────────────────────
    # Atomic permission codes used in permission snapshots and RLS precomputation.
    # Format: <domain>.<action>  e.g. 'staff_roles.read', 'branch.suspend'
    op.execute("""
        CREATE TABLE public.permissions (
            id          SMALLINT    PRIMARY KEY,
            code        VARCHAR(64) UNIQUE NOT NULL,
            description TEXT        NULL,
            CONSTRAINT chk_permission_code_lower CHECK (code = lower(code))
        );
    """)

    op.execute("""
        COMMENT ON TABLE public.permissions IS
            'Atomic permission codes. Format: <domain>.<action>. '
            'Used in compiled_permissions JSONB snapshots and RLS precomputation. '
            'Add new permissions via INSERT only — never rename existing codes.';
    """)

    op.execute("""
        INSERT INTO public.permissions (id, code, description) VALUES
            (1,  'staff_roles.read',        'View staff role assignments at a branch'),
            (2,  'staff_roles.assign',      'Assign a staff role to a member'),
            (3,  'staff_roles.revoke',      'Revoke a staff role from a member'),
            (4,  'branch.read',             'View branch details'),
            (5,  'branch.update',           'Update branch settings'),
            (6,  'branch.suspend',          'Suspend a branch'),
            (7,  'branch.delete',           'Soft-delete a branch'),
            (8,  'members.read',            'View organization member list'),
            (9,  'members.invite',          'Invite a new member to the organization'),
            (10, 'members.suspend',         'Suspend a member'),
            (11, 'members.revoke',          'Revoke membership'),
            (12, 'audit.read',              'Read audit logs'),
            (13, 'org.settings.read',       'View organization settings'),
            (14, 'org.settings.update',     'Update organization settings');
    """)

    op.execute("GRANT SELECT ON public.permissions TO app_runtime, audit_writer, readonly_analytics;")

    # ── 5. audit_key_registry ─────────────────────────────────────────────
    # Cryptographic key rotation governance for audit log signing.
    # key_version is referenced by branch_audit_log.hash_key_version.
    op.execute("""
        CREATE TABLE public.audit_key_registry (
            key_version         SMALLINT    PRIMARY KEY,
            kms_key_alias       VARCHAR(128) NOT NULL,
            algorithm           VARCHAR(32)  NOT NULL DEFAULT 'aes-256-gcm',
            digest_algorithm    VARCHAR(32)  NOT NULL DEFAULT 'sha-256',
            signature_algorithm VARCHAR(32)  NOT NULL DEFAULT 'hmac-sha-256',
            rotation_date       TIMESTAMPTZ  NOT NULL DEFAULT clock_timestamp(),
            retirement_date     TIMESTAMPTZ  NULL,
            is_active           BOOLEAN      NOT NULL DEFAULT TRUE,
            CONSTRAINT chk_audit_key_one_active EXCLUDE (is_active WITH =)
                WHERE (is_active = TRUE)  -- only one active key at a time
                DEFERRABLE INITIALLY IMMEDIATE
        );
    """)

    op.execute("""
        COMMENT ON TABLE public.audit_key_registry IS
            'Cryptographic signing key lifecycle registry. '
            'key_version referenced by branch_audit_log.hash_key_version. '
            'Rotation: INSERT new version, set retirement_date on old, '
            'then UPDATE new is_active=TRUE in same transaction (deferred constraint).';
    """)

    # Seed the initial key version (v1 — local HMAC, dev/staging)
    op.execute("""
        INSERT INTO public.audit_key_registry
            (key_version, kms_key_alias, algorithm, digest_algorithm, signature_algorithm, is_active)
        VALUES
            (1, 'local/audit-signing-key-v1', 'aes-256-gcm', 'sha-256', 'hmac-sha-256', TRUE);
    """)

    # audit_writer and app_runtime can read the active key version for hash labelling
    op.execute("GRANT SELECT ON public.audit_key_registry TO app_runtime, audit_writer, readonly_analytics;")

    # ── 6. Indexes on reference tables ────────────────────────────────────
    # These tables are tiny but accessed very frequently in joins.
    # Covering index on code → id lookup (most common access pattern).
    op.execute("CREATE INDEX ix_staff_roles_code ON public.staff_roles(code);")
    op.execute("CREATE INDEX ix_scope_types_code ON public.scope_types(code);")
    op.execute("CREATE INDEX ix_membership_statuses_code ON public.membership_statuses(code);")
    op.execute("CREATE INDEX ix_permissions_code ON public.permissions(code);")


def downgrade() -> None:
    # The predecessor has no representation for these registries. A rollback is
    # therefore safe only while every table is still in the exact revision-owned
    # seed state. Any operationally added/edited role, permission, lifecycle
    # state, or audit key must block the downgrade instead of being discarded.
    op.execute("""
        DO $$
        BEGIN
            IF (
                SELECT array_agg((id, code, hierarchy_level, is_system)::text ORDER BY id)
                FROM public.staff_roles
            ) IS DISTINCT FROM ARRAY[
                '(1,owner,100,t)', '(2,admin,80,t)', '(3,manager,60,t)',
                '(4,trainer,40,t)', '(5,receptionist,20,t)', '(6,auditor,10,t)'
            ]::text[] THEN
                RAISE EXCEPTION
                    '0023 downgrade blocked: public.staff_roles differs from its pristine seed state';
            END IF;

            IF (
                SELECT array_agg((id, code)::text ORDER BY id)
                FROM public.scope_types
            ) IS DISTINCT FROM ARRAY[
                '(1,organization)', '(2,branch)', '(3,region)', '(4,global)'
            ]::text[] THEN
                RAISE EXCEPTION
                    '0023 downgrade blocked: public.scope_types differs from its pristine seed state';
            END IF;

            IF (
                SELECT array_agg((id, code)::text ORDER BY id)
                FROM public.membership_statuses
            ) IS DISTINCT FROM ARRAY[
                '(1,pending)', '(2,invited)', '(3,active)', '(4,suspended)',
                '(5,revoked)', '(6,expired)'
            ]::text[] THEN
                RAISE EXCEPTION
                    '0023 downgrade blocked: public.membership_statuses differs from its pristine seed state';
            END IF;

            IF (
                SELECT array_agg((id, code, description)::text ORDER BY id)
                FROM public.permissions
            ) IS DISTINCT FROM ARRAY[
                '(1,staff_roles.read,"View staff role assignments at a branch")',
                '(2,staff_roles.assign,"Assign a staff role to a member")',
                '(3,staff_roles.revoke,"Revoke a staff role from a member")',
                '(4,branch.read,"View branch details")',
                '(5,branch.update,"Update branch settings")',
                '(6,branch.suspend,"Suspend a branch")',
                '(7,branch.delete,"Soft-delete a branch")',
                '(8,members.read,"View organization member list")',
                '(9,members.invite,"Invite a new member to the organization")',
                '(10,members.suspend,"Suspend a member")',
                '(11,members.revoke,"Revoke membership")',
                '(12,audit.read,"Read audit logs")',
                '(13,org.settings.read,"View organization settings")',
                '(14,org.settings.update,"Update organization settings")'
            ]::text[] THEN
                RAISE EXCEPTION
                    '0023 downgrade blocked: public.permissions differs from its pristine seed state';
            END IF;

            IF (SELECT count(*) FROM public.audit_key_registry) <> 1
               OR NOT EXISTS (
                    SELECT 1
                    FROM public.audit_key_registry
                    WHERE key_version = 1
                      AND kms_key_alias = 'local/audit-signing-key-v1'
                      AND algorithm = 'aes-256-gcm'
                      AND digest_algorithm = 'sha-256'
                      AND signature_algorithm = 'hmac-sha-256'
                      AND retirement_date IS NULL
                      AND is_active IS TRUE
               ) THEN
                RAISE EXCEPTION
                    '0023 downgrade blocked: public.audit_key_registry differs from its pristine seed state';
            END IF;
        END
        $$;
    """)

    # These indexes and tables are revision-owned. RESTRICT is deliberate:
    # unexpected dependencies are production state and must stop rollback.
    op.execute("DROP INDEX public.ix_permissions_code RESTRICT;")
    op.execute("DROP INDEX public.ix_membership_statuses_code RESTRICT;")
    op.execute("DROP INDEX public.ix_scope_types_code RESTRICT;")
    op.execute("DROP INDEX public.ix_staff_roles_code RESTRICT;")

    op.execute("DROP TABLE public.audit_key_registry RESTRICT;")
    op.execute("DROP TABLE public.permissions RESTRICT;")
    op.execute("DROP TABLE public.membership_statuses RESTRICT;")
    op.execute("DROP TABLE public.scope_types RESTRICT;")
    op.execute("DROP TABLE public.staff_roles RESTRICT;")
