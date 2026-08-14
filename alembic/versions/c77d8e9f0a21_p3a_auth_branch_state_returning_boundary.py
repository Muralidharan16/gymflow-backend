"""P3A: authorize exact first-branch-state INSERT RETURNING columns for auth.

Revision ID: c77d8e9f0a21
Revises: c67d8e9f0a20
Create Date: 2026-08-14

Verified onboarding creates the initial ``org_branch_state`` row through the
reduced ``auth_runtime`` identity. The certified branch boundary intentionally
grants that role INSERT but no table SELECT or UPDATE. SQLAlchemy emits INSERT
RETURNING for the two server-generated timestamps ``status_changed_at`` and
``updated_at``; PostgreSQL therefore requires SELECT on exactly those returned
columns.

Grant only those two column reads. ``org_branch_state`` remains ENABLE+FORCE
RLS, auth retains exactly INSERT at relation level, and no broad SELECT or
additional DML is introduced. Downgrade restores the exact c67 predecessor
column ACL state.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "c77d8e9f0a21"
down_revision = "c67d8e9f0a20"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_AUTH_ROLE = "auth_runtime"
_BRANCH_STATE = "public.org_branch_state"
_EXPECTED_TABLE_ACL = {"INSERT"}
_RETURNING_COLUMNS = ("status_changed_at", "updated_at")


def _direct_table_privileges(bind) -> set[str]:
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


def _direct_column_privileges(bind) -> set[tuple[str, str, bool, str]]:
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
        raise RuntimeError("P3A branch-state RETURNING migration requires migration_owner")
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

    auth_role = bind.execute(
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
    if auth_role is None:
        raise RuntimeError("auth_runtime is missing")
    if any(bool(auth_role[key]) for key in auth_role):
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
        {"relation": _BRANCH_STATE},
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

    observed_table = _direct_table_privileges(bind)
    if observed_table != _EXPECTED_TABLE_ACL:
        raise RuntimeError(
            "auth_runtime org_branch_state table ACL drift: "
            f"expected={sorted(_EXPECTED_TABLE_ACL)!r}, "
            f"observed={sorted(observed_table)!r}"
        )


def _require_predecessor(bind) -> None:
    _require_identity_and_relation(bind)
    observed = _direct_column_privileges(bind)
    if observed:
        raise RuntimeError(
            "c67 predecessor auth_runtime org_branch_state column ACL drift: "
            f"expected empty, observed={sorted(observed)!r}"
        )


def _require_forward(bind) -> None:
    _require_identity_and_relation(bind)
    expected = {
        (column_name, "SELECT", False, _MIGRATION_OWNER)
        for column_name in _RETURNING_COLUMNS
    }
    observed = _direct_column_privileges(bind)
    if observed != expected:
        raise RuntimeError(
            "P3A auth_runtime org_branch_state column ACL drift: "
            f"expected={sorted(expected)!r}, observed={sorted(observed)!r}"
        )

    broad_select = bind.execute(
        sa.text(
            "SELECT pg_catalog.has_table_privilege("
            "CAST(:role_name AS name), :relation, 'SELECT')"
        ),
        {"role_name": _AUTH_ROLE, "relation": _BRANCH_STATE},
    ).scalar_one()
    if bool(broad_select):
        raise RuntimeError("auth_runtime unexpectedly has broad org_branch_state SELECT")

    for forbidden_privilege in ("UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
        if bool(
            bind.execute(
                sa.text(
                    "SELECT pg_catalog.has_table_privilege("
                    "CAST(:role_name AS name), :relation, :privilege_name)"
                ),
                {
                    "role_name": _AUTH_ROLE,
                    "relation": _BRANCH_STATE,
                    "privilege_name": forbidden_privilege,
                },
            ).scalar_one()
        ):
            raise RuntimeError(
                "auth_runtime unexpectedly has forbidden org_branch_state "
                f"{forbidden_privilege}"
            )


def upgrade() -> None:
    bind = op.get_bind()
    _require_predecessor(bind)
    bind.execute(
        sa.text(
            "GRANT SELECT (status_changed_at, updated_at) "
            "ON TABLE public.org_branch_state TO auth_runtime"
        )
    )
    _require_forward(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_forward(bind)
    bind.execute(
        sa.text(
            "REVOKE SELECT (status_changed_at, updated_at) "
            "ON TABLE public.org_branch_state FROM auth_runtime"
        )
    )
    _require_predecessor(bind)
