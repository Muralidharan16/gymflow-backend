"""P3A: authorize exact first-branch INSERT RETURNING columns for auth.

Revision ID: c57d8e9f0a1f
Revises: c47d8e9f0a1e
Create Date: 2026-08-14

The verified onboarding flow creates the tenant's first ``org_branches`` row
through ``auth_runtime``. The established branch boundary intentionally grants
that role INSERT+UPDATE but no table SELECT. SQLAlchemy's insert emits RETURNING
for the persisted generated/timestamp values ``search_normalized_name``,
``created_at`` and ``updated_at``; PostgreSQL therefore also requires SELECT on
those returned columns.

Grant only those three column reads. The relation remains ENABLE+FORCE RLS and
``auth_runtime`` retains exactly its predecessor INSERT+UPDATE table ACL. No
broad table SELECT or additional DML is introduced. Downgrade restores the exact
c47 predecessor column ACL state.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "c57d8e9f0a1f"
down_revision = "c47d8e9f0a1e"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_AUTH_ROLE = "auth_runtime"
_BRANCHES = "public.org_branches"
_EXPECTED_TABLE_ACL = {"INSERT", "UPDATE"}
_RETURNING_COLUMNS = ("search_normalized_name", "created_at", "updated_at")


def _direct_table_privileges(bind) -> set[str]:
    return set(
        bind.execute(
            sa.text(
                """
                SELECT DISTINCT acl.privilege_type::text
                FROM pg_catalog.pg_class AS relation_data
                JOIN pg_catalog.pg_namespace AS namespace_data
                  ON namespace_data.oid = relation_data.relnamespace
                CROSS JOIN LATERAL pg_catalog.aclexplode(relation_data.relacl) AS acl
                JOIN pg_catalog.pg_roles AS grantee ON grantee.oid = acl.grantee
                WHERE namespace_data.nspname = 'public'
                  AND relation_data.relname = 'org_branches'
                  AND grantee.rolname = :role_name
                """
            ),
            {"role_name": _AUTH_ROLE},
        ).scalars().all()
    )


def _direct_column_privileges(bind) -> set[tuple[str, str]]:
    return set(
        bind.execute(
            sa.text(
                """
                SELECT attribute_data.attname::text, acl.privilege_type::text
                FROM pg_catalog.pg_class AS relation_data
                JOIN pg_catalog.pg_namespace AS namespace_data
                  ON namespace_data.oid = relation_data.relnamespace
                JOIN pg_catalog.pg_attribute AS attribute_data
                  ON attribute_data.attrelid = relation_data.oid
                 AND attribute_data.attnum > 0
                 AND NOT attribute_data.attisdropped
                CROSS JOIN LATERAL pg_catalog.aclexplode(attribute_data.attacl) AS acl
                JOIN pg_catalog.pg_roles AS grantee ON grantee.oid = acl.grantee
                WHERE namespace_data.nspname = 'public'
                  AND relation_data.relname = 'org_branches'
                  AND grantee.rolname = :role_name
                """
            ),
            {"role_name": _AUTH_ROLE},
        ).all()
    )


def _require_identity_and_relation(bind) -> None:
    row = bind.execute(
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
    if row["session_name"] != _MIGRATION_OWNER or row["current_name"] != _MIGRATION_OWNER:
        raise RuntimeError("P3A auth branch RETURNING migration requires migration_owner")
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
            SELECT pg_catalog.pg_get_userbyid(relowner)::text AS owner_name,
                   relrowsecurity, relforcerowsecurity
            FROM pg_catalog.pg_class
            WHERE oid = CAST(:relation AS regclass)
            """
        ),
        {"relation": _BRANCHES},
    ).mappings().one_or_none()
    if relation is None:
        raise RuntimeError("org_branches is missing")
    if relation["owner_name"] != _MIGRATION_OWNER:
        raise RuntimeError(f"unexpected org_branches owner: {relation['owner_name']!r}")
    if (bool(relation["relrowsecurity"]), bool(relation["relforcerowsecurity"])) != (True, True):
        raise RuntimeError("org_branches must retain ENABLE + FORCE RLS")

    observed_table = _direct_table_privileges(bind)
    if observed_table != _EXPECTED_TABLE_ACL:
        raise RuntimeError(
            "auth_runtime org_branches table ACL drift: "
            f"expected={sorted(_EXPECTED_TABLE_ACL)!r}, observed={sorted(observed_table)!r}"
        )


def _require_predecessor(bind) -> None:
    _require_identity_and_relation(bind)
    observed = _direct_column_privileges(bind)
    if observed:
        raise RuntimeError(
            "c47 predecessor auth_runtime org_branches column ACL drift: "
            f"expected empty, observed={sorted(observed)!r}"
        )


def _require_forward(bind) -> None:
    _require_identity_and_relation(bind)
    expected = {(column, "SELECT") for column in _RETURNING_COLUMNS}
    observed = _direct_column_privileges(bind)
    if observed != expected:
        raise RuntimeError(
            "P3A auth_runtime org_branches column ACL drift: "
            f"expected={sorted(expected)!r}, observed={sorted(observed)!r}"
        )

    # A column SELECT grant must never silently become broad table SELECT.
    broad_select = bind.execute(
        sa.text(
            "SELECT pg_catalog.has_table_privilege(CAST(:role_name AS name), :relation, 'SELECT')"
        ),
        {"role_name": _AUTH_ROLE, "relation": _BRANCHES},
    ).scalar_one()
    if bool(broad_select):
        raise RuntimeError("auth_runtime unexpectedly has broad org_branches SELECT")


def upgrade() -> None:
    bind = op.get_bind()
    _require_predecessor(bind)
    bind.execute(
        sa.text(
            "GRANT SELECT (search_normalized_name, created_at, updated_at) "
            "ON TABLE public.org_branches TO auth_runtime"
        )
    )
    _require_forward(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_forward(bind)
    bind.execute(
        sa.text(
            "REVOKE SELECT (search_normalized_name, created_at, updated_at) "
            "ON TABLE public.org_branches FROM auth_runtime"
        )
    )
    _require_predecessor(bind)
