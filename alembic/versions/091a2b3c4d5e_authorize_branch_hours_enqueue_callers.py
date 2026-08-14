"""Authorize branch-hours enqueue callers at the FORCE-RLS boundary.

Revision ID: 091a2b3c4d5e
Revises: f8091a2b3c4d
Create Date: 2026-08-11

Custom ``app.*`` GUCs are transaction context, not cryptographic credentials: a
compromised database login can set them.  The e7 enqueue functions therefore
must not be authorized by tenant GUC alone.  This revision replaces their queue
INSERT policy with a canonical typed-principal check:

* signup owners are revalidated through the d6 current-principal validator;
* legacy staff must be revalidated and hold owner/admin role; and
* modern organization users must have an active organization membership plus
  an active branch-manager assignment for branch-scoped events.

Organization-wide enqueue intentionally remains unavailable to modern
organization_user role strings because the application currently has no
canonical org-scoped role relation for that namespace.  That matches d6's
fail-closed organization-hours write policy.

Only the identity/assignment columns needed by the no-login security owner are
granted.  app_runtime still receives no queue or identity-table SELECT access.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "091a2b3c4d5e"
down_revision = "f8091a2b3c4d"
branch_labels = None
depends_on = None

_POLICY = "branch_hours_internal_outbox_insert"
_ROLE_READ_POLICY = "branch_hours_internal_enqueue_role_read"
_SECURITY_OWNER = "app_security_owner"

_MEMBER_COLUMNS = (
    "id",
    "org_id",
    "user_id",
    "membership_status_id",
    "deleted_at",
)
_ROLE_COLUMNS = (
    "organization_member_id",
    "org_id",
    "branch_id",
    "role_id",
    "revoked_at",
    "deleted_at",
    "effective_from",
    "effective_to",
)


def _require_migration_owner(bind) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT session_user::text AS session_name,
                   current_user::text AS current_name,
                   rolsuper, rolinherit, rolcreatedb, rolcreaterole,
                   rolreplication, rolbypassrls
            FROM pg_catalog.pg_roles
            WHERE rolname = current_user
            """
        )
    ).mappings().one()
    if row["session_name"] != "migration_owner" or row["current_name"] != "migration_owner":
        raise RuntimeError("091a enqueue authorization migration requires migration_owner")
    if any(
        bool(row[key])
        for key in (
            "rolsuper",
            "rolinherit",
            "rolcreatedb",
            "rolcreaterole",
            "rolreplication",
            "rolbypassrls",
        )
    ):
        raise RuntimeError("migration_owner violates the reduced role contract")


def _policy_names(bind, relation: str) -> set[str]:
    return set(
        bind.execute(
            sa.text(
                """
                SELECT polname::text
                FROM pg_catalog.pg_policy
                WHERE polrelid = CAST(:relation AS regclass)
                """
            ),
            {"relation": relation},
        ).scalars().all()
    )


def _explicit_column_selects(bind, relation: str, columns: tuple[str, ...]) -> set[str]:
    return set(
        bind.execute(
            sa.text(
                """
                SELECT attribute_data.attname::text
                FROM pg_catalog.pg_attribute AS attribute_data
                CROSS JOIN LATERAL pg_catalog.aclexplode(attribute_data.attacl) AS acl_data
                JOIN pg_catalog.pg_roles AS grantee
                  ON grantee.oid = acl_data.grantee
                WHERE attribute_data.attrelid = CAST(:relation AS regclass)
                  AND attribute_data.attnum > 0
                  AND NOT attribute_data.attisdropped
                  AND attribute_data.attname = ANY(CAST(:columns AS text[]))
                  AND grantee.rolname = :role_name
                  AND acl_data.privilege_type = 'SELECT'
                """
            ),
            {
                "relation": relation,
                "columns": list(columns),
                "role_name": _SECURITY_OWNER,
            },
        ).scalars().all()
    )


def _current_org_expr() -> str:
    return """
        CASE
            WHEN pg_catalog.pg_input_is_valid(
                NULLIF(pg_catalog.current_setting('app.current_org_id', true), ''),
                'uuid'
            )
            THEN CAST(
                NULLIF(pg_catalog.current_setting('app.current_org_id', true), '')
                AS uuid
            )
            ELSE CAST(NULL AS uuid)
        END
    """


def _current_user_expr() -> str:
    return """
        CASE
            WHEN pg_catalog.pg_input_is_valid(
                NULLIF(pg_catalog.current_setting('app.current_user_id', true), ''),
                'uuid'
            )
            THEN CAST(
                NULLIF(pg_catalog.current_setting('app.current_user_id', true), '')
                AS uuid
            )
            ELSE CAST(NULL AS uuid)
        END
    """


def _canonical_org_actor_expr(target_org: str) -> str:
    return f"""
        (
            (
                NULLIF(
                    pg_catalog.current_setting('app.current_principal_type', true),
                    ''
                ) = 'owner'
                AND NULLIF(
                    pg_catalog.current_setting('app.current_role', true),
                    ''
                ) = 'owner'
                AND public.branch_hours_current_nonmember_principal_valid({target_org})
            )
            OR
            (
                NULLIF(
                    pg_catalog.current_setting('app.current_principal_type', true),
                    ''
                ) = 'legacy_gym_owner'
                AND NULLIF(
                    pg_catalog.current_setting('app.current_role', true),
                    ''
                ) IN ('owner', 'admin')
                AND public.branch_hours_current_nonmember_principal_valid({target_org})
            )
        )
    """


def _modern_manager_expr(target_org: str, target_branch: str) -> str:
    current_user = _current_user_expr()
    return f"""
        (
            NULLIF(
                pg_catalog.current_setting('app.current_principal_type', true),
                ''
            ) = 'organization_user'
            AND EXISTS (
                SELECT 1
                FROM public.organization_members AS member_data
                JOIN public.branch_staff_roles AS role_assignment
                  ON role_assignment.organization_member_id = member_data.id
                 AND role_assignment.org_id = member_data.org_id
                WHERE member_data.org_id = {target_org}
                  AND member_data.user_id = {current_user}
                  AND member_data.membership_status_id = 3
                  AND member_data.deleted_at IS NULL
                  AND role_assignment.branch_id = {target_branch}
                  AND role_assignment.role_id = 3
                  AND role_assignment.revoked_at IS NULL
                  AND role_assignment.deleted_at IS NULL
                  AND (
                        role_assignment.effective_from IS NULL
                        OR role_assignment.effective_from <= pg_catalog.clock_timestamp()
                  )
                  AND (
                        role_assignment.effective_to IS NULL
                        OR role_assignment.effective_to > pg_catalog.clock_timestamp()
                  )
            )
        )
    """


def _create_forward_policy() -> None:
    current_org = _current_org_expr()
    org_actor = _canonical_org_actor_expr("transactional_outbox.tenant_id")
    manager = _modern_manager_expr(
        "transactional_outbox.tenant_id",
        "transactional_outbox.branch_id",
    )
    op.execute(
        f"""
        CREATE POLICY branch_hours_internal_outbox_insert
        ON public.transactional_outbox
        FOR INSERT TO app_security_owner
        WITH CHECK (
            tenant_id = {current_org}
            AND parent_event_id IS NULL
            AND event_version = 1
            AND correlation_id IS NOT NULL
            AND (
                (
                    event_type = 'branch_hours.branch_changed'
                    AND branch_id IS NOT NULL
                    AND EXISTS (
                        SELECT 1
                        FROM public.org_branches AS branch_data
                        WHERE branch_data.id = transactional_outbox.branch_id
                          AND branch_data.org_id = transactional_outbox.tenant_id
                    )
                    AND ({org_actor} OR {manager})
                )
                OR
                (
                    event_type = 'branch_hours.organization_changed'
                    AND branch_id IS NULL
                    AND {org_actor}
                )
            )
        )
        """
    )


def _restore_e7_policy() -> None:
    current_org = _current_org_expr()
    op.execute(
        f"""
        CREATE POLICY branch_hours_internal_outbox_insert
        ON public.transactional_outbox
        FOR INSERT TO app_security_owner
        WITH CHECK (
            tenant_id = {current_org}
            AND parent_event_id IS NULL
            AND event_version = 1
            AND correlation_id IS NOT NULL
            AND (
                (
                    event_type = 'branch_hours.branch_changed'
                    AND branch_id IS NOT NULL
                    AND EXISTS (
                        SELECT 1
                        FROM public.org_branches AS branch_data
                        WHERE branch_data.id = transactional_outbox.branch_id
                          AND branch_data.org_id = transactional_outbox.tenant_id
                    )
                )
                OR
                (
                    event_type = 'branch_hours.organization_changed'
                    AND branch_id IS NULL
                )
            )
        )
        """
    )


def upgrade() -> None:
    bind = op.get_bind()
    _require_migration_owner(bind)

    for relation in (
        "public.transactional_outbox",
        "public.organization_members",
        "public.branch_staff_roles",
    ):
        if bind.execute(
            sa.text("SELECT pg_catalog.to_regclass(:relation) IS NOT NULL"),
            {"relation": relation},
        ).scalar_one() is not True:
            raise RuntimeError(f"091a required relation missing: {relation}")

    if _POLICY not in _policy_names(bind, "public.transactional_outbox"):
        raise RuntimeError("091a predecessor e7 enqueue policy is absent")
    if _ROLE_READ_POLICY in _policy_names(bind, "public.branch_staff_roles"):
        raise RuntimeError("091a role-read policy already exists")

    if _explicit_column_selects(
        bind, "public.organization_members", _MEMBER_COLUMNS
    ):
        raise RuntimeError(
            "091a refuses pre-existing explicit app_security_owner member-column grants"
        )
    if _explicit_column_selects(
        bind, "public.branch_staff_roles", _ROLE_COLUMNS
    ):
        raise RuntimeError(
            "091a refuses pre-existing explicit app_security_owner role-column grants"
        )

    op.execute(
        "GRANT SELECT (id, org_id, user_id, membership_status_id, deleted_at) "
        "ON TABLE public.organization_members TO app_security_owner"
    )
    op.execute(
        "GRANT SELECT (organization_member_id, org_id, branch_id, role_id, "
        "revoked_at, deleted_at, effective_from, effective_to) "
        "ON TABLE public.branch_staff_roles TO app_security_owner"
    )

    # organization_members' predecessor policy already applies to all roles and
    # is tenant-bound by app.current_org_id. branch_staff_roles has additional
    # reader gates, so give only the no-login security owner a dedicated
    # tenant-bound SELECT path for this enqueue validation.
    op.execute(
        f"""
        CREATE POLICY branch_hours_internal_enqueue_role_read
        ON public.branch_staff_roles
        FOR SELECT TO app_security_owner
        USING (
            org_id = {_current_org_expr()}
            AND deleted_at IS NULL
        )
        """
    )

    op.execute("DROP POLICY branch_hours_internal_outbox_insert ON public.transactional_outbox")
    _create_forward_policy()

    if bind.execute(
        sa.text(
            "SELECT pg_catalog.has_table_privilege('app_runtime', "
            "'public.organization_members', 'SELECT')"
        )
    ).scalar_one() is not True:
        # app_runtime already needs canonical membership SELECT for its own RLS;
        # fail if the predecessor application contract unexpectedly vanished.
        raise RuntimeError("091a predecessor app_runtime membership SELECT drifted")
    if bind.execute(
        sa.text(
            "SELECT pg_catalog.has_table_privilege('app_runtime', "
            "'public.transactional_outbox', 'INSERT')"
        )
    ).scalar_one():
        raise RuntimeError("091a leaked direct outbox INSERT to app_runtime")
    if _POLICY not in _policy_names(bind, "public.transactional_outbox"):
        raise RuntimeError("091a failed to install authorized enqueue policy")
    if _ROLE_READ_POLICY not in _policy_names(bind, "public.branch_staff_roles"):
        raise RuntimeError("091a failed to install role validation policy")


def downgrade() -> None:
    bind = op.get_bind()
    _require_migration_owner(bind)
    if _POLICY not in _policy_names(bind, "public.transactional_outbox"):
        raise RuntimeError("091a downgrade enqueue-policy drift")
    if _ROLE_READ_POLICY not in _policy_names(bind, "public.branch_staff_roles"):
        raise RuntimeError("091a downgrade role-policy drift")

    op.execute("DROP POLICY branch_hours_internal_outbox_insert ON public.transactional_outbox")
    _restore_e7_policy()
    op.execute(
        "DROP POLICY branch_hours_internal_enqueue_role_read ON public.branch_staff_roles"
    )
    op.execute(
        "REVOKE SELECT (id, org_id, user_id, membership_status_id, deleted_at) "
        "ON TABLE public.organization_members FROM app_security_owner"
    )
    op.execute(
        "REVOKE SELECT (organization_member_id, org_id, branch_id, role_id, "
        "revoked_at, deleted_at, effective_from, effective_to) "
        "ON TABLE public.branch_staff_roles FROM app_security_owner"
    )
