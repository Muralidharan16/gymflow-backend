"""P3A: reconcile initial branch-state RLS with the reduced auth bootstrap identity.

Revision ID: c87d8e9f0a22
Revises: c77d8e9f0a21
Create Date: 2026-08-14

C77 intentionally leaves ``auth_runtime`` with INSERT-only authority on
``org_branch_state`` plus SELECT on the two SQLAlchemy RETURNING columns needed
for the first branch-state row.  The older DF590 lifecycle policy still allowed
INSERT only when ``auth.role()`` was ``superadmin`` or ``system``.  Verified
owner onboarding therefore reached the correct reduced auth login and exact
INSERT ACL, but PostgreSQL rejected the canonical initial row at FORCE RLS.

Do not grant broad SELECT/UPDATE and do not add ``owner`` to the ordinary
lifecycle INSERT role set.  Instead, retain the DF590 superadmin/system path and
add one narrowly bounded bootstrap arm that requires all of the following:

* the real database session/current identity is a member of ``auth_runtime``;
* request context is organization-scoped owner context for the current tenant;
* the typed principal is an owner or the preserved legacy gym-owner identity;
* every lifecycle-sensitive field is the canonical initial active/primary state.

The composite branch-state FK still binds ``(branch_id, org_id)`` to an existing
branch in the same organization, while the predecessor tenant policy continues
to enforce ``app.current_org_id``.  This revision changes no role membership or
ACL and does not broaden ordinary owner lifecycle mutation.  Downgrade restores
the exact DF590 insert policy expression used by the c77 predecessor.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "c87d8e9f0a22"
down_revision = "c77d8e9f0a21"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_AUTH_ROLE = "auth_runtime"
_RELATION = "public.org_branch_state"
_POLICY = "p_branch_insert"
_EXPECTED_TABLE_ACL = {"INSERT"}
_EXPECTED_COLUMN_ACL = {
    ("status_changed_at", "SELECT", False, _MIGRATION_OWNER),
    ("updated_at", "SELECT", False, _MIGRATION_OWNER),
}


def _policy_row(bind):
    return bind.execute(
        sa.text(
            """
            SELECT
                policy_data.polcmd::text AS command,
                policy_data.polpermissive AS permissive,
                policy_data.polroles = ARRAY[0::oid] AS public_only,
                pg_catalog.pg_get_expr(
                    policy_data.polqual,
                    policy_data.polrelid,
                    true
                )::text AS using_expr,
                pg_catalog.pg_get_expr(
                    policy_data.polwithcheck,
                    policy_data.polrelid,
                    true
                )::text AS check_expr
            FROM pg_catalog.pg_policy AS policy_data
            WHERE policy_data.polrelid = CAST(:relation AS regclass)
              AND policy_data.polname = :policy_name
            """
        ),
        {"relation": _RELATION, "policy_name": _POLICY},
    ).mappings().one_or_none()


def _direct_table_acl(bind) -> set[str]:
    return set(
        bind.execute(
            sa.text(
                """
                SELECT DISTINCT acl_data.privilege_type::text
                FROM pg_catalog.pg_class AS relation_data
                JOIN pg_catalog.pg_namespace AS namespace_data
                  ON namespace_data.oid = relation_data.relnamespace
                CROSS JOIN LATERAL pg_catalog.aclexplode(relation_data.relacl) AS acl_data
                JOIN pg_catalog.pg_roles AS grantee_role
                  ON grantee_role.oid = acl_data.grantee
                WHERE namespace_data.nspname = 'public'
                  AND relation_data.relname = 'org_branch_state'
                  AND grantee_role.rolname = :role_name
                """
            ),
            {"role_name": _AUTH_ROLE},
        ).scalars().all()
    )


def _direct_column_acl(bind) -> set[tuple[str, str, bool, str]]:
    return {
        (str(row[0]), str(row[1]), bool(row[2]), str(row[3]))
        for row in bind.execute(
            sa.text(
                """
                SELECT
                    attribute_data.attname::text,
                    acl_data.privilege_type::text,
                    acl_data.is_grantable,
                    grantor_role.rolname::text
                FROM pg_catalog.pg_class AS relation_data
                JOIN pg_catalog.pg_namespace AS namespace_data
                  ON namespace_data.oid = relation_data.relnamespace
                JOIN pg_catalog.pg_attribute AS attribute_data
                  ON attribute_data.attrelid = relation_data.oid
                 AND attribute_data.attnum > 0
                 AND NOT attribute_data.attisdropped
                CROSS JOIN LATERAL pg_catalog.aclexplode(attribute_data.attacl) AS acl_data
                JOIN pg_catalog.pg_roles AS grantee_role
                  ON grantee_role.oid = acl_data.grantee
                JOIN pg_catalog.pg_roles AS grantor_role
                  ON grantor_role.oid = acl_data.grantor
                WHERE namespace_data.nspname = 'public'
                  AND relation_data.relname = 'org_branch_state'
                  AND grantee_role.rolname = :role_name
                """
            ),
            {"role_name": _AUTH_ROLE},
        ).all()
    }


def _normalized(value: object) -> str:
    return " ".join(str(value or "").lower().split())


def _require_identity_and_relation(bind) -> None:
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
        raise RuntimeError("c87 requires session_user=current_user=migration_owner")
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
        raise RuntimeError("migration_owner violates the reduced migration contract")

    auth = bind.execute(
        sa.text(
            """
            SELECT rolcanlogin, rolsuper, rolinherit, rolcreatedb, rolcreaterole,
                   rolreplication, rolbypassrls
            FROM pg_catalog.pg_roles
            WHERE rolname = :role_name
            """
        ),
        {"role_name": _AUTH_ROLE},
    ).mappings().one_or_none()
    if auth is None:
        raise RuntimeError("auth_runtime is missing")
    if any(bool(auth[key]) for key in auth):
        raise RuntimeError("auth_runtime violates NOLOGIN/NOINHERIT/NOBYPASSRLS")

    relation = bind.execute(
        sa.text(
            """
            SELECT
                pg_catalog.pg_get_userbyid(relowner)::text AS owner_name,
                relrowsecurity,
                relforcerowsecurity
            FROM pg_catalog.pg_class
            WHERE oid = CAST(:relation AS regclass)
            """
        ),
        {"relation": _RELATION},
    ).mappings().one_or_none()
    if relation is None:
        raise RuntimeError("org_branch_state is missing")
    if relation["owner_name"] != _MIGRATION_OWNER:
        raise RuntimeError(
            f"unexpected org_branch_state owner: {relation['owner_name']!r}"
        )
    if (
        bool(relation["relrowsecurity"]),
        bool(relation["relforcerowsecurity"]),
    ) != (True, True):
        raise RuntimeError("org_branch_state must retain ENABLE + FORCE RLS")

    table_acl = _direct_table_acl(bind)
    if table_acl != _EXPECTED_TABLE_ACL:
        raise RuntimeError(
            "auth_runtime org_branch_state table ACL drift: "
            f"expected={sorted(_EXPECTED_TABLE_ACL)!r}, observed={sorted(table_acl)!r}"
        )
    column_acl = _direct_column_acl(bind)
    if column_acl != _EXPECTED_COLUMN_ACL:
        raise RuntimeError(
            "auth_runtime org_branch_state column ACL drift: "
            f"expected={sorted(_EXPECTED_COLUMN_ACL)!r}, observed={sorted(column_acl)!r}"
        )

    if bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.has_table_privilege("
                "CAST(:role_name AS name), :relation, 'SELECT')"
            ),
            {"role_name": _AUTH_ROLE, "relation": _RELATION},
        ).scalar_one()
    ):
        raise RuntimeError("auth_runtime unexpectedly has broad org_branch_state SELECT")
    for forbidden in ("UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
        if bool(
            bind.execute(
                sa.text(
                    "SELECT pg_catalog.has_table_privilege("
                    "CAST(:role_name AS name), :relation, :privilege_name)"
                ),
                {
                    "role_name": _AUTH_ROLE,
                    "relation": _RELATION,
                    "privilege_name": forbidden,
                },
            ).scalar_one()
        ):
            raise RuntimeError(
                f"auth_runtime unexpectedly has org_branch_state {forbidden}"
            )


def _require_policy_shape(bind, *, forward: bool) -> None:
    row = _policy_row(bind)
    if row is None:
        raise RuntimeError("p_branch_insert policy is missing")
    if row["command"] != "a" or not bool(row["permissive"]):
        raise RuntimeError("p_branch_insert command/permissive posture drifted")
    if row["using_expr"] is not None:
        raise RuntimeError("INSERT policy unexpectedly has USING expression")
    if not bool(row["public_only"]):
        raise RuntimeError("p_branch_insert policy role target is no longer PUBLIC-only")

    source = _normalized(row["check_expr"])
    for token in ("auth.role()", "superadmin", "system"):
        if token not in source:
            raise RuntimeError(f"p_branch_insert lost predecessor token {token!r}")

    bootstrap_tokens = (
        "auth_runtime",
        "pg_has_role",
        "app.current_org_id",
        "app.current_user_id",
        "app.current_principal_type",
        "app.current_gym_id",
        "legacy_gym_owner",
        "search_epoch_ulid",
        "lifecycle_transition_in_progress",
        "worm_archive_status",
    )
    if not forward:
        leaked = [token for token in bootstrap_tokens if token in source]
        if leaked:
            raise RuntimeError(
                "c77 predecessor p_branch_insert already contains c87 bootstrap logic: "
                f"{leaked!r}"
            )
        return

    for token in bootstrap_tokens:
        if token not in source:
            raise RuntimeError(f"c87 p_branch_insert missing bootstrap token {token!r}")
    if "app_runtime" in source:
        raise RuntimeError("c87 p_branch_insert leaked an app_runtime authorization arm")

    # Ordinary lifecycle owner insertion must not be introduced by changing the
    # original role list. Owner appears only inside the auth-runtime bootstrap
    # branch, which is additionally tenant/identity/canonical-state constrained.
    if "current_user = session_user" not in source:
        raise RuntimeError("c87 p_branch_insert lost direct-session identity binding")


def _require_predecessor(bind) -> None:
    _require_identity_and_relation(bind)
    _require_policy_shape(bind, forward=False)


def _require_forward(bind) -> None:
    _require_identity_and_relation(bind)
    _require_policy_shape(bind, forward=True)


_FORWARD_POLICY = r"""
ALTER POLICY p_branch_insert ON public.org_branch_state
WITH CHECK (
    auth.role() IN ('superadmin', 'system')
    OR (
        current_user = session_user
        AND pg_catalog.pg_has_role(session_user, 'auth_runtime', 'MEMBER')
        AND auth.role() = 'owner'
        AND NULLIF(
            pg_catalog.current_setting('app.current_gym_id', true), ''
        ) IS NULL
        AND NULLIF(
            pg_catalog.current_setting('app.current_principal_type', true), ''
        ) IN ('owner', 'legacy_gym_owner')
        AND NULLIF(
            pg_catalog.current_setting('app.current_user_id', true), ''
        ) IS NOT NULL
        AND org_id = NULLIF(
            pg_catalog.current_setting('app.current_org_id', true), ''
        )::uuid
        AND branch_status = 'active'
        AND is_primary IS TRUE
        AND is_active IS TRUE
        AND is_public IS TRUE
        AND status = 'active'
        AND is_operational IS TRUE
        AND status_changed_by IS NULL
        AND status_reason IS NULL
        AND transition_source = 'api'
        AND scheduled_transition_at IS NULL
        AND scheduled_transition_to IS NULL
        AND lifecycle_transition_in_progress IS FALSE
        AND saga_last_checkpoint IS NULL
        AND saga_compensation_strategy IS NULL
        AND watchdog_recovered_at IS NULL
        AND watchdog_recovery_count = 0
        AND search_visibility_version = 1
        AND search_last_synced_at IS NULL
        AND search_sync_failed_at IS NULL
        AND reconciliation_claimed_by IS NULL
        AND reconciliation_claimed_at IS NULL
        AND worm_archive_uri IS NULL
        AND worm_archive_checksum IS NULL
        AND worm_archive_verified_at IS NULL
        AND worm_archive_status IS NULL
        AND version = 1
        AND search_logical_clock = 0
        AND deleted_at IS NULL
        AND archived_at IS NULL
        AND purged_at IS NULL
        AND pg_catalog.char_length(search_epoch_ulid) = 26
        AND search_epoch_ulid ~ '^[0-9A-HJKMNP-TV-Z]{26}$'
    )
)
"""

_PREDECESSOR_POLICY = r"""
ALTER POLICY p_branch_insert ON public.org_branch_state
WITH CHECK (
    auth.role() IN ('superadmin', 'system')
)
"""


def upgrade() -> None:
    bind = op.get_bind()
    _require_predecessor(bind)
    op.execute(_FORWARD_POLICY)
    _require_forward(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_forward(bind)
    op.execute(_PREDECESSOR_POLICY)
    _require_predecessor(bind)
