"""Align branch-hours RLS with the application's typed principal domains.

Revision ID: d6e7f8091a2b
Revises: c5d6e7f8091a
Create Date: 2026-08-11

The branch-hours runtime boundary initially treated every authenticated actor as
an ``organization_member``.  That is not the application's identity model.
The active owner-authentication flow uses ``public.owners`` and deliberately
sets ``app.current_principal_type = 'owner'``; modern RBAC users use
``organization_users``/``organization_members``; and legacy staff remain in
``gym_owners``.  The typed audit-principal migration explicitly preserves these
three namespaces.

This revision changes only the application branch-hours policies.  It does not
add table privileges, weaken FORCE RLS, or trust ``app.current_role`` by itself.
A role claim is accepted only after the current UUID/org pair is validated
against the matching source registry:

* owner: verified, onboarding-complete ``owners`` row; role must be ``owner``;
* organization_user: active ``organization_members`` row;
* legacy_gym_owner: active + verified ``gym_owners`` row whose stored role
  matches ``app.current_role``.

Organization-default writes are limited to validated owner/admin identities.
Branch writes allow those same org-level identities or an active modern RBAC
member with an active branch-manager assignment.  Reads require a validated
active principal and same-tenant branch state.

Downgrade restores the exact b4c5 member-only policy semantics.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "d6e7f8091a2b"
down_revision = "c5d6e7f8091a"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_RUNTIME = "app_runtime"

_APPLICATION_POLICIES = {
    "public.organization_operating_hours": {
        "org_hours_read_active_member",
        "org_hours_insert_owner_admin",
        "org_hours_update_owner_admin",
    },
    "public.branch_operating_hours": {
        "branch_hours_read_active_member",
        "branch_hours_insert_authorized",
        "branch_hours_update_authorized",
    },
    "public.branch_special_hours": {
        "branch_special_hours_read_active_member",
        "branch_special_hours_insert_authorized",
        "branch_special_hours_update_authorized",
    },
}

_REQUIRED_OTHER_POLICIES = {
    "public.branch_operating_hours": {
        "internal_branch_hours_soft_delete_update",
    },
    "public.branch_special_hours": {
        "internal_branch_special_hours_soft_delete_update",
    },
    "public.branch_hours_projection": {
        "internal_branch_hours_projection_delete",
        "tenant_isolation_projection",
    },
    "public.branch_hours_audit_log": {
        "internal_branch_hours_audit_insert",
        "tenant_isolation_audit",
    },
}


def _policy_names(bind, relation: str) -> set[str]:
    return set(
        bind.execute(
            sa.text(
                """
                SELECT policy_data.polname::text
                FROM pg_catalog.pg_policy AS policy_data
                WHERE policy_data.polrelid = CAST(:relation AS regclass)
                """
            ),
            {"relation": relation},
        ).scalars().all()
    )


def _require_preflight(bind) -> None:
    identity = bind.execute(
        sa.text(
            """
            SELECT
                session_user::text AS session_name,
                current_user::text AS current_name,
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
    ).mappings().one()
    if (
        identity["session_name"] != _MIGRATION_OWNER
        or identity["current_name"] != _MIGRATION_OWNER
    ):
        raise RuntimeError("d6e7 typed-principal migration requires migration_owner")
    if any(
        bool(identity[key])
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

    required = (
        "public.owners",
        "public.organization_members",
        "public.gym_owners",
        "public.org_branches",
        "public.org_branch_state",
        "public.branch_staff_roles",
        *tuple(_APPLICATION_POLICIES),
        *tuple(_REQUIRED_OTHER_POLICIES),
    )
    missing = bind.execute(
        sa.text(
            """
            SELECT relation_name
            FROM unnest(CAST(:relations AS text[])) AS required(relation_name)
            WHERE pg_catalog.to_regclass(required.relation_name) IS NULL
            ORDER BY relation_name
            """
        ),
        {"relations": sorted(set(required))},
    ).scalars().all()
    if missing:
        raise RuntimeError(
            f"d6e7 required relations are missing: {tuple(missing)!r}"
        )

    for relation, application_names in _APPLICATION_POLICIES.items():
        expected = set(application_names) | _REQUIRED_OTHER_POLICIES.get(relation, set())
        observed = _policy_names(bind, relation)
        if observed != expected:
            raise RuntimeError(
                f"d6e7 predecessor policy drift on {relation}: "
                f"observed={sorted(observed)!r}, expected={sorted(expected)!r}"
            )

    for relation, expected in _REQUIRED_OTHER_POLICIES.items():
        if relation in _APPLICATION_POLICIES:
            continue
        observed = _policy_names(bind, relation)
        if observed != expected:
            raise RuntimeError(
                f"d6e7 protected policy drift on {relation}: "
                f"observed={sorted(observed)!r}, expected={sorted(expected)!r}"
            )


def _uuid_guc(name: str) -> str:
    return f"""
        CASE
            WHEN pg_catalog.pg_input_is_valid(
                NULLIF(pg_catalog.current_setting('{name}', true), ''),
                'uuid'
            )
            THEN CAST(
                NULLIF(pg_catalog.current_setting('{name}', true), '') AS uuid
            )
            ELSE CAST(NULL AS uuid)
        END
    """


def _active_owner_expr(target_org: str) -> str:
    current_user = _uuid_guc("app.current_user_id")
    current_org = _uuid_guc("app.current_org_id")
    return f"""
        (
            NULLIF(pg_catalog.current_setting('app.current_principal_type', true), '') = 'owner'
            AND NULLIF(pg_catalog.current_setting('app.current_role', true), '') = 'owner'
            AND {target_org} = {current_org}
            AND EXISTS (
                SELECT 1
                FROM public.owners AS owner_data
                WHERE owner_data.id = {current_user}
                  AND owner_data.org_id = {target_org}
                  AND owner_data.email_verified IS TRUE
                  AND owner_data.onboarding_completed IS TRUE
            )
        )
    """


def _active_org_user_expr(target_org: str) -> str:
    current_user = _uuid_guc("app.current_user_id")
    current_org = _uuid_guc("app.current_org_id")
    return f"""
        (
            NULLIF(pg_catalog.current_setting('app.current_principal_type', true), '') = 'organization_user'
            AND {target_org} = {current_org}
            AND EXISTS (
                SELECT 1
                FROM public.organization_members AS member_data
                WHERE member_data.org_id = {target_org}
                  AND member_data.user_id = {current_user}
                  AND member_data.membership_status_id = 3
                  AND member_data.deleted_at IS NULL
            )
        )
    """


def _active_legacy_staff_expr(target_org: str) -> str:
    current_user = _uuid_guc("app.current_user_id")
    current_org = _uuid_guc("app.current_org_id")
    return f"""
        (
            NULLIF(pg_catalog.current_setting('app.current_principal_type', true), '') = 'legacy_gym_owner'
            AND {target_org} = {current_org}
            AND EXISTS (
                SELECT 1
                FROM public.gym_owners AS staff_data
                WHERE staff_data.id = {current_user}
                  AND staff_data.org_id = {target_org}
                  AND staff_data.is_active IS TRUE
                  AND staff_data.is_verified IS TRUE
                  AND staff_data.role::text = NULLIF(
                        pg_catalog.current_setting('app.current_role', true), ''
                      )
            )
        )
    """


def _active_principal_expr(target_org: str) -> str:
    return f"""
        (
            {_active_owner_expr(target_org)}
            OR {_active_org_user_expr(target_org)}
            OR {_active_legacy_staff_expr(target_org)}
        )
    """


def _org_write_expr(target_org: str) -> str:
    owner = _active_owner_expr(target_org)
    legacy_admin = f"""
        (
            {_active_legacy_staff_expr(target_org)}
            AND NULLIF(pg_catalog.current_setting('app.current_role', true), '')
                IN ('owner', 'admin')
        )
    """
    # Modern organization-user org-admin semantics are not yet represented by a
    # canonical org-scoped role relation.  Do not authorize them from a JWT role
    # string alone.  They remain eligible for branch-manager writes below.
    return f"({owner} OR {legacy_admin})"


def _branch_read_expr(branch_column: str) -> str:
    current_org = _uuid_guc("app.current_org_id")
    return f"""
        EXISTS (
            SELECT 1
            FROM public.org_branches AS branch_data
            JOIN public.org_branch_state AS branch_state
              ON branch_state.branch_id = branch_data.id
             AND branch_state.org_id = branch_data.org_id
            WHERE branch_data.id = {branch_column}
              AND branch_data.org_id = {current_org}
              AND branch_state.deleted_at IS NULL
              AND branch_state.is_active IS TRUE
              AND {_active_principal_expr('branch_data.org_id')}
        )
    """


def _branch_write_expr(branch_column: str) -> str:
    current_org = _uuid_guc("app.current_org_id")
    current_user = _uuid_guc("app.current_user_id")
    org_level = _org_write_expr("branch_data.org_id")
    modern_manager = f"""
        (
            NULLIF(pg_catalog.current_setting('app.current_principal_type', true), '') = 'organization_user'
            AND EXISTS (
                SELECT 1
                FROM public.organization_members AS member_data
                JOIN public.branch_staff_roles AS role_assignment
                  ON role_assignment.organization_member_id = member_data.id
                 AND role_assignment.org_id = member_data.org_id
                WHERE member_data.org_id = branch_data.org_id
                  AND member_data.user_id = {current_user}
                  AND member_data.membership_status_id = 3
                  AND member_data.deleted_at IS NULL
                  AND role_assignment.branch_id = branch_data.id
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
    return f"""
        EXISTS (
            SELECT 1
            FROM public.org_branches AS branch_data
            JOIN public.org_branch_state AS branch_state
              ON branch_state.branch_id = branch_data.id
             AND branch_state.org_id = branch_data.org_id
            WHERE branch_data.id = {branch_column}
              AND branch_data.org_id = {current_org}
              AND branch_state.deleted_at IS NULL
              AND branch_state.is_active IS TRUE
              AND ({org_level} OR {modern_manager})
        )
    """


def _drop_application_policies() -> None:
    for relation, policy_names in _APPLICATION_POLICIES.items():
        for policy_name in sorted(policy_names):
            op.execute(f"DROP POLICY {policy_name} ON {relation}")


def _create_typed_policies() -> None:
    org_read = _active_principal_expr("organization_operating_hours.org_id")
    org_write = _org_write_expr("organization_operating_hours.org_id")

    op.execute(
        f"""
        CREATE POLICY org_hours_read_active_member
        ON public.organization_operating_hours
        FOR SELECT TO app_runtime
        USING ({org_read})
        """
    )
    op.execute(
        f"""
        CREATE POLICY org_hours_insert_owner_admin
        ON public.organization_operating_hours
        FOR INSERT TO app_runtime
        WITH CHECK ({org_write})
        """
    )
    op.execute(
        f"""
        CREATE POLICY org_hours_update_owner_admin
        ON public.organization_operating_hours
        FOR UPDATE TO app_runtime
        USING ({org_write})
        WITH CHECK ({org_write})
        """
    )

    for relation, prefix in (
        ("public.branch_operating_hours", "branch_hours"),
        ("public.branch_special_hours", "branch_special_hours"),
    ):
        branch_column = f"{relation.split('.', 1)[1]}.branch_id"
        read_expr = _branch_read_expr(branch_column)
        write_expr = _branch_write_expr(branch_column)
        op.execute(
            f"""
            CREATE POLICY {prefix}_read_active_member
            ON {relation}
            FOR SELECT TO app_runtime
            USING ({read_expr})
            """
        )
        op.execute(
            f"""
            CREATE POLICY {prefix}_insert_authorized
            ON {relation}
            FOR INSERT TO app_runtime
            WITH CHECK ({write_expr})
            """
        )
        op.execute(
            f"""
            CREATE POLICY {prefix}_update_authorized
            ON {relation}
            FOR UPDATE TO app_runtime
            USING ({write_expr})
            WITH CHECK ({write_expr})
            """
        )


def _b4_active_member_expr(target_org: str) -> str:
    current_user = _uuid_guc("app.current_user_id")
    current_org = _uuid_guc("app.current_org_id")
    return f"""
        EXISTS (
            SELECT 1
            FROM public.organization_members AS member_data
            WHERE member_data.org_id = {target_org}
              AND member_data.org_id = {current_org}
              AND member_data.user_id = {current_user}
              AND member_data.membership_status_id = 3
              AND member_data.deleted_at IS NULL
        )
    """


def _b4_branch_member_expr(branch_column: str, *, write: bool) -> str:
    current_org = _uuid_guc("app.current_org_id")
    current_user = _uuid_guc("app.current_user_id")
    role_clause = "TRUE"
    if write:
        role_clause = f"""
            (
                NULLIF(pg_catalog.current_setting('app.current_role', true), '')
                    IN ('owner', 'admin')
                OR EXISTS (
                    SELECT 1
                    FROM public.branch_staff_roles AS role_assignment
                    WHERE role_assignment.org_id = branch_data.org_id
                      AND role_assignment.branch_id = branch_data.id
                      AND role_assignment.organization_member_id = member_data.id
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
    return f"""
        EXISTS (
            SELECT 1
            FROM public.org_branches AS branch_data
            JOIN public.org_branch_state AS branch_state
              ON branch_state.branch_id = branch_data.id
             AND branch_state.org_id = branch_data.org_id
            JOIN public.organization_members AS member_data
              ON member_data.org_id = branch_data.org_id
            WHERE branch_data.id = {branch_column}
              AND branch_data.org_id = {current_org}
              AND member_data.user_id = {current_user}
              AND member_data.membership_status_id = 3
              AND member_data.deleted_at IS NULL
              AND branch_state.deleted_at IS NULL
              AND branch_state.is_active = TRUE
              AND {role_clause}
        )
    """


def _restore_b4_policies() -> None:
    org_member = _b4_active_member_expr("organization_operating_hours.org_id")
    org_write = f"""
        {org_member}
        AND NULLIF(pg_catalog.current_setting('app.current_role', true), '')
            IN ('owner', 'admin')
    """
    op.execute(
        f"""
        CREATE POLICY org_hours_read_active_member
        ON public.organization_operating_hours
        FOR SELECT TO app_runtime
        USING ({org_member})
        """
    )
    op.execute(
        f"""
        CREATE POLICY org_hours_insert_owner_admin
        ON public.organization_operating_hours
        FOR INSERT TO app_runtime
        WITH CHECK ({org_write})
        """
    )
    op.execute(
        f"""
        CREATE POLICY org_hours_update_owner_admin
        ON public.organization_operating_hours
        FOR UPDATE TO app_runtime
        USING ({org_write})
        WITH CHECK ({org_write})
        """
    )

    for relation, prefix in (
        ("public.branch_operating_hours", "branch_hours"),
        ("public.branch_special_hours", "branch_special_hours"),
    ):
        branch_column = f"{relation.split('.', 1)[1]}.branch_id"
        read_expr = _b4_branch_member_expr(branch_column, write=False)
        write_expr = _b4_branch_member_expr(branch_column, write=True)
        op.execute(
            f"""
            CREATE POLICY {prefix}_read_active_member
            ON {relation}
            FOR SELECT TO app_runtime
            USING ({read_expr})
            """
        )
        op.execute(
            f"""
            CREATE POLICY {prefix}_insert_authorized
            ON {relation}
            FOR INSERT TO app_runtime
            WITH CHECK ({write_expr})
            """
        )
        op.execute(
            f"""
            CREATE POLICY {prefix}_update_authorized
            ON {relation}
            FOR UPDATE TO app_runtime
            USING ({write_expr})
            WITH CHECK ({write_expr})
            """
        )


def _verify_policy_inventory(bind) -> None:
    for relation, application_names in _APPLICATION_POLICIES.items():
        expected = set(application_names) | _REQUIRED_OTHER_POLICIES.get(relation, set())
        observed = _policy_names(bind, relation)
        if observed != expected:
            raise RuntimeError(
                f"d6e7 final policy inventory drift on {relation}: "
                f"observed={sorted(observed)!r}, expected={sorted(expected)!r}"
            )


def upgrade() -> None:
    bind = op.get_bind()
    _require_preflight(bind)
    _drop_application_policies()
    _create_typed_policies()
    _verify_policy_inventory(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_preflight(bind)
    _drop_application_policies()
    _restore_b4_policies()
    _verify_policy_inventory(bind)
