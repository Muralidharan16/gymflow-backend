"""P3A: authorize only the canonical initial branch-state bootstrap for auth.

Revision ID: c87d8e9f0a22
Revises: c77d8e9f0a21
Create Date: 2026-08-14

C77 intentionally leaves ``auth_runtime`` with INSERT-only authority on
``org_branch_state`` plus SELECT on the two SQLAlchemy RETURNING columns needed
for the first branch-state row.  The existing lifecycle INSERT policy is owned
by the branch lifecycle control plane and is deliberately scoped to its own
database identities.  Reduced-auth owner onboarding therefore reaches the
correct ACL but is rejected by FORCE RLS when it creates the canonical initial
state row.

P3A must not widen that lifecycle policy (the role/lifecycle matrix is P3D).
This revision leaves the predecessor lifecycle policy unchanged and adds one
separate permissive INSERT policy targeted only at ``auth_runtime``.  Its CHECK
expression binds the real database identity, tenant and typed owner context and
accepts only the canonical active/primary initial state emitted by first-branch
onboarding.  No role membership or relation/column privilege is changed.

Downgrade drops only the P3A-owned bootstrap policy.  Both directions snapshot
the predecessor lifecycle policy and fail if it changes as a side effect.
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
_LIFECYCLE_POLICY = "p_branch_insert"
_BOOTSTRAP_POLICY = "p_branch_insert_auth_bootstrap"
_EXPECTED_TABLE_ACL = {"INSERT"}
_EXPECTED_COLUMN_ACL = {
    ("status_changed_at", "SELECT", False, _MIGRATION_OWNER),
    ("updated_at", "SELECT", False, _MIGRATION_OWNER),
}


def _policy_row(bind, policy_name: str):
    return bind.execute(
        sa.text(
            """
            SELECT
                policy_data.polcmd::text AS command,
                policy_data.polpermissive AS permissive,
                array_to_string(
                    ARRAY(
                        SELECT CASE
                            WHEN role_oid = 0 THEN 'PUBLIC'
                            ELSE pg_catalog.pg_get_userbyid(role_oid)::text
                        END
                        FROM unnest(policy_data.polroles) AS role_oid
                        ORDER BY 1
                    ),
                    ','
                ) AS role_fingerprint,
                pg_catalog.pg_get_expr(
                    policy_data.polqual,
                    policy_data.polrelid,
                    true
                )::text AS using_expr,
                pg_catalog.pg_get_expr(
                    policy_data.polwithcheck,
                    policy_data.polrelid,
                    true
                )::text AS check_expr,
                policy_data.polroles = ARRAY[
                    (SELECT role_data.oid
                     FROM pg_catalog.pg_roles AS role_data
                     WHERE role_data.rolname = :auth_role)
                ]::oid[] AS auth_only
            FROM pg_catalog.pg_policy AS policy_data
            WHERE policy_data.polrelid = CAST(:relation AS regclass)
              AND policy_data.polname = :policy_name
            """
        ),
        {
            "relation": _RELATION,
            "policy_name": policy_name,
            "auth_role": _AUTH_ROLE,
        },
    ).mappings().one_or_none()


def _policy_snapshot(bind, policy_name: str) -> tuple[object, ...] | None:
    row = _policy_row(bind, policy_name)
    if row is None:
        return None
    return (
        str(row["command"]),
        bool(row["permissive"]),
        str(row["role_fingerprint"] or ""),
        None if row["using_expr"] is None else str(row["using_expr"]),
        None if row["check_expr"] is None else str(row["check_expr"]),
    )


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


def _require_lifecycle_policy(bind) -> tuple[object, ...]:
    snapshot = _policy_snapshot(bind, _LIFECYCLE_POLICY)
    if snapshot is None:
        raise RuntimeError("predecessor p_branch_insert lifecycle policy is missing")
    row = _policy_row(bind, _LIFECYCLE_POLICY)
    assert row is not None
    if row["command"] != "a" or not bool(row["permissive"]):
        raise RuntimeError("p_branch_insert command/permissive posture drifted")
    if row["using_expr"] is not None:
        raise RuntimeError("p_branch_insert unexpectedly has USING expression")
    source = _normalized(row["check_expr"])
    for token in ("auth.role()", "superadmin", "system"):
        if token not in source:
            raise RuntimeError(f"p_branch_insert lost lifecycle token {token!r}")
    if "auth_runtime" in source or _BOOTSTRAP_POLICY.lower() in source:
        raise RuntimeError("lifecycle policy already contains P3A bootstrap logic")
    return snapshot


def _require_bootstrap_absent(bind) -> None:
    if _policy_row(bind, _BOOTSTRAP_POLICY) is not None:
        raise RuntimeError(f"{_BOOTSTRAP_POLICY} already exists")


def _require_bootstrap_policy(bind) -> None:
    row = _policy_row(bind, _BOOTSTRAP_POLICY)
    if row is None:
        raise RuntimeError(f"{_BOOTSTRAP_POLICY} is missing")
    if row["command"] != "a" or not bool(row["permissive"]):
        raise RuntimeError("auth bootstrap policy command/permissive posture drifted")
    if row["using_expr"] is not None:
        raise RuntimeError("auth bootstrap INSERT policy unexpectedly has USING")
    if not bool(row["auth_only"]):
        raise RuntimeError(
            "auth bootstrap policy must target exactly auth_runtime and no other role"
        )
    if str(row["role_fingerprint"] or "") != _AUTH_ROLE:
        raise RuntimeError(
            "auth bootstrap policy role fingerprint drifted: "
            f"{row['role_fingerprint']!r}"
        )

    source = _normalized(row["check_expr"])
    required_tokens = (
        "current_user = session_user",
        "auth_runtime",
        "pg_has_role",
        "auth.role()",
        "owner",
        "app.current_org_id",
        "app.current_user_id",
        "app.current_principal_type",
        "app.current_gym_id",
        "legacy_gym_owner",
        "search_epoch_ulid",
        "lifecycle_transition_in_progress",
        "worm_archive_status",
    )
    for token in required_tokens:
        if token not in source:
            raise RuntimeError(f"auth bootstrap policy missing token {token!r}")
    if "app_runtime" in source or "public" in str(row["role_fingerprint"] or "").lower():
        raise RuntimeError("auth bootstrap policy widened beyond auth_runtime")


_BOOTSTRAP_SQL = r"""
CREATE POLICY p_branch_insert_auth_bootstrap
ON public.org_branch_state
AS PERMISSIVE
FOR INSERT
TO auth_runtime
WITH CHECK (
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
"""


def upgrade() -> None:
    bind = op.get_bind()
    _require_identity_and_relation(bind)
    lifecycle_before = _require_lifecycle_policy(bind)
    _require_bootstrap_absent(bind)

    op.execute(_BOOTSTRAP_SQL)

    _require_identity_and_relation(bind)
    _require_bootstrap_policy(bind)
    lifecycle_after = _require_lifecycle_policy(bind)
    if lifecycle_after != lifecycle_before:
        raise RuntimeError("c87 changed the predecessor lifecycle policy")


def downgrade() -> None:
    bind = op.get_bind()
    _require_identity_and_relation(bind)
    lifecycle_before = _require_lifecycle_policy(bind)
    _require_bootstrap_policy(bind)

    op.execute(
        "DROP POLICY p_branch_insert_auth_bootstrap ON public.org_branch_state"
    )

    _require_identity_and_relation(bind)
    _require_bootstrap_absent(bind)
    lifecycle_after = _require_lifecycle_policy(bind)
    if lifecycle_after != lifecycle_before:
        raise RuntimeError("c87 downgrade changed the predecessor lifecycle policy")
