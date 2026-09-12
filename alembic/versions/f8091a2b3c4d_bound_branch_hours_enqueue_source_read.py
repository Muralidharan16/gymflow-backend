"""Bound branch-hours enqueue validation reads under FORCE RLS.

Revision ID: f8091a2b3c4d
Revises: e7f8091a2b3c
Create Date: 2026-08-11

The e7 enqueue functions are owned by ``app_security_owner`` and deliberately
run with ``row_security=on``.  The predecessor 8192 revision already grants that
role only ``org_branches(id, org_id)`` SELECT columns, but FORCE RLS still needs
an explicit policy.  This revision adds only the tenant-bound policy required
for those validation reads.  It does not add table privileges, schema CREATE,
or any application-runtime capability.
"""

from alembic import op
import sqlalchemy as sa


revision = "f8091a2b3c4d"
down_revision = "e7f8091a2b3c"
branch_labels = None
depends_on = None

_POLICY = "branch_hours_internal_enqueue_branch_read"
_RELATION = "public.org_branches"


def _policy_names(bind) -> set[str]:
    return set(
        bind.execute(
            sa.text(
                """
                SELECT polname::text
                FROM pg_catalog.pg_policy
                WHERE polrelid = CAST(:relation AS regclass)
                """
            ),
            {"relation": _RELATION},
        ).scalars().all()
    )


def _require_migration_owner(bind) -> None:
    identity = bind.execute(
        sa.text(
            """
            SELECT session_user::text, current_user::text,
                   rolsuper, rolinherit, rolcreatedb, rolcreaterole,
                   rolreplication, rolbypassrls
            FROM pg_catalog.pg_roles
            WHERE rolname = current_user
            """
        )
    ).one()
    if identity[0] != "migration_owner" or identity[1] != "migration_owner":
        raise RuntimeError("f8091 branch-hours enqueue policy requires migration_owner")
    if any(bool(value) for value in identity[2:]):
        raise RuntimeError("migration_owner violates the reduced role contract")


def upgrade() -> None:
    bind = op.get_bind()
    _require_migration_owner(bind)
    if _POLICY in _policy_names(bind):
        raise RuntimeError("f8091 enqueue branch-read policy already exists")

    direct_columns = set(
        bind.execute(
            sa.text(
                """
                SELECT column_name::text
                FROM information_schema.column_privileges
                WHERE table_schema = 'public'
                  AND table_name = 'org_branches'
                  AND grantee = 'app_security_owner'
                  AND privilege_type = 'SELECT'
                """
            )
        ).scalars().all()
    )
    if not {"id", "org_id"}.issubset(direct_columns):
        raise RuntimeError(
            "f8091 requires predecessor 8192 bounded org_branches(id, org_id) SELECT"
        )

    op.execute(
        """
        CREATE POLICY branch_hours_internal_enqueue_branch_read
        ON public.org_branches
        FOR SELECT TO app_security_owner
        USING (
            org_id = CASE
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
        )
        """
    )

    if _POLICY not in _policy_names(bind):
        raise RuntimeError("f8091 failed to install enqueue branch-read policy")
    if bind.execute(
        sa.text(
            "SELECT pg_catalog.has_schema_privilege('app_security_owner', 'public', 'CREATE')"
        )
    ).scalar_one():
        raise RuntimeError("f8091 detected leaked app_security_owner schema CREATE")


def downgrade() -> None:
    bind = op.get_bind()
    _require_migration_owner(bind)
    if _POLICY not in _policy_names(bind):
        raise RuntimeError("f8091 downgrade policy drift")
    op.execute(
        "DROP POLICY branch_hours_internal_enqueue_branch_read ON public.org_branches"
    )
