"""Align branch read ACL and lifecycle status visibility.

Revision ID: 92a3b4c5d6e7
Revises: 8192a3b4c5d6
Create Date: 2026-08-11

This revision closes two related read-contract defects without changing any
write privilege:

* the canonical ``security_invoker`` active-branch view was used by the runtime
  repository but app_runtime had never been granted SELECT on the view itself;
* ``p_branch_select`` hid states that BranchAccessGuard intentionally permits
  (manager temporary/renovation reads and owner/admin terminal ledger reads),
  while its status predicates were not an explicit fail-closed allowlist.

Tenant isolation, FORCE RLS, write policies, branch mutation ACLs and worker
role boundaries are deliberately unchanged.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "92a3b4c5d6e7"
down_revision: Union[str, None] = "8192a3b4c5d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_CANONICAL_STATUSES = (
    "active",
    "temporarily_closed",
    "under_renovation",
    "compliance_suspended",
    "permanently_closed",
)


def _preflight_upgrade() -> None:
    op.execute(
        """
        DO $$
        DECLARE
            reloptions text[];
        BEGIN
            IF to_regrole('app_runtime') IS NULL THEN
                RAISE EXCEPTION '92a3 branch read contract requires role app_runtime';
            END IF;

            IF to_regclass('public.v_active_org_branches') IS NULL THEN
                RAISE EXCEPTION '92a3 branch read contract requires public.v_active_org_branches';
            END IF;

            SELECT c.reloptions
              INTO reloptions
              FROM pg_catalog.pg_class AS c
              JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
             WHERE n.nspname = 'public'
               AND c.relname = 'v_active_org_branches'
               AND c.relkind = 'v';

            IF reloptions IS NULL
               OR NOT ('security_invoker=true' = ANY(reloptions)) THEN
                RAISE EXCEPTION
                    '92a3 refuses to grant runtime view access unless v_active_org_branches is security_invoker';
            END IF;

            IF has_table_privilege(
                'app_runtime', 'public.v_active_org_branches', 'SELECT'
            ) THEN
                RAISE EXCEPTION
                    '92a3 predecessor drift: app_runtime already has SELECT on v_active_org_branches';
            END IF;

            IF NOT EXISTS (
                SELECT 1
                  FROM pg_catalog.pg_policy AS p
                  JOIN pg_catalog.pg_class AS c ON c.oid = p.polrelid
                  JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'public'
                   AND c.relname = 'org_branch_state'
                   AND p.polname = 'p_branch_select'
                   AND p.polcmd = 'r'
            ) THEN
                RAISE EXCEPTION
                    '92a3 predecessor drift: p_branch_select is missing';
            END IF;

            IF NOT EXISTS (
                SELECT 1
                  FROM pg_catalog.pg_class AS c
                  JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'public'
                   AND c.relname = 'org_branch_state'
                   AND c.relrowsecurity
                   AND c.relforcerowsecurity
            ) THEN
                RAISE EXCEPTION
                    '92a3 predecessor drift: org_branch_state must have ENABLE + FORCE RLS';
            END IF;
        END
        $$;
        """
    )


def _install_aligned_select_policy() -> None:
    op.execute("DROP POLICY p_branch_select ON public.org_branch_state;")
    op.execute(
        """
        CREATE POLICY p_branch_select ON public.org_branch_state
        FOR SELECT
        USING (
            org_id = NULLIF(
                current_setting('app.current_org_id', true), ''
            )::UUID
            AND (
                (
                    auth.role() = 'trainer'
                    AND status = 'active'
                )
                OR (
                    auth.role() = 'manager'
                    AND status IN (
                        'active',
                        'temporarily_closed',
                        'under_renovation'
                    )
                )
                OR (
                    auth.role() IN ('owner', 'admin', 'org_admin')
                    AND status IN (
                        'active',
                        'temporarily_closed',
                        'under_renovation',
                        'compliance_suspended',
                        'permanently_closed'
                    )
                )
                OR (
                    auth.role() IN (
                        'compliance',
                        'superadmin',
                        'system',
                        'saga_orchestrator',
                        'system_watchdog'
                    )
                    AND status IN (
                        'active',
                        'temporarily_closed',
                        'under_renovation',
                        'compliance_suspended',
                        'permanently_closed'
                    )
                )
            )
        );
        """
    )


def _verify_upgrade() -> None:
    op.execute(
        """
        DO $$
        DECLARE
            policy_count integer;
        BEGIN
            IF NOT has_table_privilege(
                'app_runtime', 'public.v_active_org_branches', 'SELECT'
            ) THEN
                RAISE EXCEPTION
                    '92a3 postcondition failed: app_runtime lacks view SELECT';
            END IF;

            IF has_table_privilege(
                'app_runtime', 'public.v_active_org_branches', 'INSERT'
            ) OR has_table_privilege(
                'app_runtime', 'public.v_active_org_branches', 'UPDATE'
            ) OR has_table_privilege(
                'app_runtime', 'public.v_active_org_branches', 'DELETE'
            ) OR has_table_privilege(
                'app_runtime', 'public.v_active_org_branches', 'TRUNCATE'
            ) THEN
                RAISE EXCEPTION
                    '92a3 postcondition failed: app_runtime has non-SELECT view privilege';
            END IF;

            SELECT count(*)
              INTO policy_count
              FROM pg_catalog.pg_policy AS p
              JOIN pg_catalog.pg_class AS c ON c.oid = p.polrelid
              JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
             WHERE n.nspname = 'public'
               AND c.relname = 'org_branch_state'
               AND p.polname = 'p_branch_select'
               AND p.polcmd = 'r';

            IF policy_count <> 1 THEN
                RAISE EXCEPTION
                    '92a3 postcondition failed: expected exactly one p_branch_select';
            END IF;

            IF NOT EXISTS (
                SELECT 1
                  FROM pg_catalog.pg_class AS c
                  JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'public'
                   AND c.relname = 'org_branch_state'
                   AND c.relrowsecurity
                   AND c.relforcerowsecurity
            ) THEN
                RAISE EXCEPTION
                    '92a3 postcondition failed: FORCE RLS changed unexpectedly';
            END IF;
        END
        $$;
        """
    )


def upgrade() -> None:
    _preflight_upgrade()

    # A security-invoker view still requires an ACL on the view object itself.
    # Underlying table SELECT remains subject to the existing forced RLS policy.
    op.execute(
        "GRANT SELECT ON TABLE public.v_active_org_branches TO app_runtime;"
    )
    _install_aligned_select_policy()
    _verify_upgrade()


def _restore_predecessor_select_policy() -> None:
    op.execute("DROP POLICY p_branch_select ON public.org_branch_state;")
    op.execute(
        """
        CREATE POLICY p_branch_select ON public.org_branch_state
        FOR SELECT
        USING (
            org_id = NULLIF(
                current_setting('app.current_org_id', true), ''
            )::UUID
            AND (
                (
                    auth.role() IN ('manager', 'trainer')
                    AND is_operational = TRUE
                )
                OR (
                    auth.role() IN ('owner', 'admin', 'org_admin')
                    AND status != 'permanently_closed'
                )
                OR auth.role() IN (
                    'compliance',
                    'superadmin',
                    'system',
                    'saga_orchestrator',
                    'system_watchdog'
                )
            )
        );
        """
    )


def downgrade() -> None:
    # Restore the exact predecessor read surface. No predecessor revision granted
    # app_runtime access to the view object.
    _restore_predecessor_select_policy()
    op.execute(
        "REVOKE SELECT ON TABLE public.v_active_org_branches FROM app_runtime;"
    )
