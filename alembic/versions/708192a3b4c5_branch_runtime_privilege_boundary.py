"""Establish least-privilege branch runtime/bootstrap ACLs.

Revision ID: 708192a3b4c5
Revises: 6f708192a3b4
Create Date: 2026-08-10

The branch tables predate the reduced PostgreSQL runtime identities.  Once the
application stopped connecting as an owner-equivalent login, two legitimate
production paths were exposed as missing object capabilities:

* steady-state branch APIs read branch metadata and read/update branch state via
  the ordinary application pool; and
* verified onboarding creates the first principal branch, links its address,
  and inserts the initial branch state via the dedicated auth/bootstrap pool.

This revision grants only those operations.  It does not grant DELETE,
TRUNCATE, REFERENCES, TRIGGER, schema CREATE, ownership, or RLS bypass.  Both
relations must already have ENABLE + FORCE RLS so object ACLs never replace the
tenant policy boundary.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "708192a3b4c5"
down_revision = "6f708192a3b4"
branch_labels = None
depends_on = None


_MIGRATION_OWNER = "migration_owner"
_RUNTIME_ROLE = "app_runtime"
_AUTH_ROLE = "auth_runtime"

_BRANCHES = "public.org_branches"
_BRANCH_STATE = "public.org_branch_state"

_RUNTIME_PRIVILEGES = {
    _BRANCHES: {"SELECT"},
    _BRANCH_STATE: {"SELECT", "UPDATE"},
}
_AUTH_BOOTSTRAP_PRIVILEGES = {
    _BRANCHES: {"INSERT", "UPDATE"},
    _BRANCH_STATE: {"INSERT"},
}
_FORBIDDEN_PRIVILEGES = {"DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"}


def _scalar(bind, sql: str, params: dict[str, object] | None = None):
    return bind.execute(sa.text(sql), params or {}).scalar_one()


def _direct_privileges(bind, role_name: str, relation: str) -> set[str]:
    schema_name, relation_name = relation.split(".", 1)
    return set(
        bind.execute(
            sa.text(
                """
                SELECT DISTINCT acl.privilege_type::text
                FROM pg_catalog.pg_class AS relation_data
                JOIN pg_catalog.pg_namespace AS namespace_data
                  ON namespace_data.oid = relation_data.relnamespace
                CROSS JOIN LATERAL pg_catalog.aclexplode(
                    COALESCE(
                        relation_data.relacl,
                        pg_catalog.acldefault('r', relation_data.relowner)
                    )
                ) AS acl
                JOIN pg_catalog.pg_roles AS grantee
                  ON grantee.oid = acl.grantee
                WHERE namespace_data.nspname = :schema_name
                  AND relation_data.relname = :relation_name
                  AND grantee.rolname = :role_name
                """
            ),
            {
                "schema_name": schema_name,
                "relation_name": relation_name,
                "role_name": role_name,
            },
        ).scalars().all()
    )


def _require_identity_and_roles(bind) -> None:
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
        raise RuntimeError("branch runtime migration requires migration_owner")
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

    rows = bind.execute(
        sa.text(
            """
            SELECT
                rolname,
                rolcanlogin,
                rolsuper,
                rolinherit,
                rolcreatedb,
                rolcreaterole,
                rolreplication,
                rolbypassrls
            FROM pg_catalog.pg_roles
            WHERE rolname IN (:runtime_role, :auth_role)
            """
        ),
        {"runtime_role": _RUNTIME_ROLE, "auth_role": _AUTH_ROLE},
    ).mappings().all()
    by_name = {row["rolname"]: row for row in rows}
    if set(by_name) != {_RUNTIME_ROLE, _AUTH_ROLE}:
        raise RuntimeError("required branch runtime roles are missing")

    for role_name, row in by_name.items():
        if any(
            bool(row[key])
            for key in (
                "rolcanlogin",
                "rolsuper",
                "rolinherit",
                "rolcreatedb",
                "rolcreaterole",
                "rolreplication",
                "rolbypassrls",
            )
        ):
            raise RuntimeError(
                f"managed role {role_name} violates NOLOGIN/NOINHERIT/NOBYPASSRLS"
            )


def _require_relation_security(bind) -> None:
    for relation in (_BRANCHES, _BRANCH_STATE):
        row = bind.execute(
            sa.text(
                """
                SELECT
                    pg_catalog.pg_get_userbyid(relation_data.relowner)::text
                        AS owner_name,
                    relation_data.relrowsecurity,
                    relation_data.relforcerowsecurity
                FROM pg_catalog.pg_class AS relation_data
                WHERE relation_data.oid = CAST(:relation AS regclass)
                """
            ),
            {"relation": relation},
        ).mappings().one_or_none()
        if row is None:
            raise RuntimeError(f"required branch relation is missing: {relation}")
        if row["owner_name"] != _MIGRATION_OWNER:
            raise RuntimeError(
                f"unexpected owner for {relation}: {row['owner_name']!r}"
            )
        if not row["relrowsecurity"] or not row["relforcerowsecurity"]:
            raise RuntimeError(
                f"{relation} must retain ENABLE + FORCE ROW LEVEL SECURITY"
            )


def _require_no_public_dml(bind) -> None:
    for relation in (_BRANCHES, _BRANCH_STATE):
        schema_name, relation_name = relation.split(".", 1)
        rows = bind.execute(
            sa.text(
                """
                SELECT DISTINCT acl.privilege_type::text
                FROM pg_catalog.pg_class AS relation_data
                JOIN pg_catalog.pg_namespace AS namespace_data
                  ON namespace_data.oid = relation_data.relnamespace
                CROSS JOIN LATERAL pg_catalog.aclexplode(
                    COALESCE(
                        relation_data.relacl,
                        pg_catalog.acldefault('r', relation_data.relowner)
                    )
                ) AS acl
                WHERE namespace_data.nspname = :schema_name
                  AND relation_data.relname = :relation_name
                  AND acl.grantee = 0
                """
            ),
            {"schema_name": schema_name, "relation_name": relation_name},
        ).scalars().all()
        observed = set(rows)
        forbidden = {
            "SELECT",
            "INSERT",
            "UPDATE",
            "DELETE",
            "TRUNCATE",
            "REFERENCES",
            "TRIGGER",
        }
        leaked = observed & forbidden
        if leaked:
            raise RuntimeError(
                f"PUBLIC unexpectedly has {sorted(leaked)!r} on {relation}"
            )


def _require_predecessor_acl(bind) -> None:
    # These direct branch-table ACLs are introduced for the first time here.
    for role_name in (_RUNTIME_ROLE, _AUTH_ROLE):
        for relation in (_BRANCHES, _BRANCH_STATE):
            observed = _direct_privileges(bind, role_name, relation)
            if observed:
                raise RuntimeError(
                    "branch runtime predecessor ACL drift: "
                    f"{role_name} has {sorted(observed)!r} on {relation}"
                )


def _verify_final_acl(bind) -> None:
    for role_name, contract in (
        (_RUNTIME_ROLE, _RUNTIME_PRIVILEGES),
        (_AUTH_ROLE, _AUTH_BOOTSTRAP_PRIVILEGES),
    ):
        for relation in (_BRANCHES, _BRANCH_STATE):
            expected = set(contract.get(relation, set()))
            observed = _direct_privileges(bind, role_name, relation)
            if observed != expected:
                raise RuntimeError(
                    "branch runtime direct ACL drift: "
                    f"{role_name} on {relation}: "
                    f"expected={sorted(expected)!r}, observed={sorted(observed)!r}"
                )

            for privilege in expected:
                if not _scalar(
                    bind,
                    """
                    SELECT pg_catalog.has_table_privilege(
                        CAST(:role_name AS name), :relation, :privilege
                    )
                    """,
                    {
                        "role_name": role_name,
                        "relation": relation,
                        "privilege": privilege,
                    },
                ):
                    raise RuntimeError(
                        f"{role_name} lacks required {privilege} on {relation}"
                    )

            for privilege in _FORBIDDEN_PRIVILEGES:
                if _scalar(
                    bind,
                    """
                    SELECT pg_catalog.has_table_privilege(
                        CAST(:role_name AS name), :relation, :privilege
                    )
                    """,
                    {
                        "role_name": role_name,
                        "relation": relation,
                        "privilege": privilege,
                    },
                ):
                    raise RuntimeError(
                        f"{role_name} has forbidden {privilege} on {relation}"
                    )

    for role_name in (_RUNTIME_ROLE, _AUTH_ROLE):
        if _scalar(
            bind,
            """
            SELECT pg_catalog.has_schema_privilege(
                CAST(:role_name AS name), 'public', 'CREATE'
            )
            """,
            {"role_name": role_name},
        ):
            raise RuntimeError(
                f"{role_name} must not have CREATE on public schema"
            )


def _grant_contract() -> None:
    op.execute(
        "GRANT SELECT ON TABLE public.org_branches TO app_runtime"
    )
    op.execute(
        "GRANT SELECT, UPDATE ON TABLE public.org_branch_state TO app_runtime"
    )
    op.execute(
        "GRANT INSERT, UPDATE ON TABLE public.org_branches TO auth_runtime"
    )
    op.execute(
        "GRANT INSERT ON TABLE public.org_branch_state TO auth_runtime"
    )


def _revoke_contract() -> None:
    op.execute(
        "REVOKE SELECT ON TABLE public.org_branches FROM app_runtime"
    )
    op.execute(
        "REVOKE SELECT, UPDATE ON TABLE public.org_branch_state FROM app_runtime"
    )
    op.execute(
        "REVOKE INSERT, UPDATE ON TABLE public.org_branches FROM auth_runtime"
    )
    op.execute(
        "REVOKE INSERT ON TABLE public.org_branch_state FROM auth_runtime"
    )


def upgrade() -> None:
    bind = op.get_bind()
    _require_identity_and_roles(bind)
    _require_relation_security(bind)
    _require_no_public_dml(bind)
    _require_predecessor_acl(bind)
    _grant_contract()
    _verify_final_acl(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_identity_and_roles(bind)
    _require_relation_security(bind)
    _require_no_public_dml(bind)
    _verify_final_acl(bind)
    _revoke_contract()
    _require_predecessor_acl(bind)
