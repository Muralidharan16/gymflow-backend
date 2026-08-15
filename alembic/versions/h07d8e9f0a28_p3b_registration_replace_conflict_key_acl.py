"""P3B: permit only the secure payload conflict key required by replacement.

Revision ID: h07d8e9f0a28
Revises: g07d8e9f0a27
Create Date: 2026-08-15

PostgreSQL's INSERT .. ON CONFLICT (registration_id) DO UPDATE path requires
read authority on the conflict target.  The registration security owner gets
SELECT on registration_id only; ciphertext, tenant ids and key metadata remain
unreadable.  app_runtime receives no direct payload privilege.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "h07d8e9f0a28"
down_revision = "g07d8e9f0a27"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_API = "app_runtime"
_PAYLOAD = "public.organization_registration_payloads_secure"
_EXPECTED_SECURITY_SELECT = {"registration_id"}


def _scalar(bind, sql: str, params: dict[str, object] | None = None):
    return bind.execute(sa.text(sql), params or {}).scalar_one()


def _require_identity(bind) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT session_user::text, current_user::text,
                   role_data.rolsuper, role_data.rolinherit,
                   role_data.rolcreatedb, role_data.rolcreaterole,
                   role_data.rolreplication, role_data.rolbypassrls
            FROM pg_catalog.pg_roles AS role_data
            WHERE role_data.rolname = current_user
            """
        )
    ).one()
    if row[0] != _MIGRATION_OWNER or row[1] != _MIGRATION_OWNER:
        raise RuntimeError("P3B conflict-key ACL migration requires migration_owner")
    if any(bool(value) for value in row[2:]):
        raise RuntimeError("migration_owner violates the reduced role contract")


def _select_columns(bind, role_name: str) -> set[str]:
    return {
        str(row[0])
        for row in bind.execute(
            sa.text(
                """
                SELECT attribute_data.attname::text
                FROM pg_catalog.pg_attribute AS attribute_data
                CROSS JOIN LATERAL pg_catalog.aclexplode(attribute_data.attacl) AS acl_data
                JOIN pg_catalog.pg_roles AS grantee_role
                  ON grantee_role.oid = acl_data.grantee
                WHERE attribute_data.attrelid = pg_catalog.to_regclass(:relation)
                  AND attribute_data.attnum > 0
                  AND NOT attribute_data.attisdropped
                  AND grantee_role.rolname = :role_name
                  AND acl_data.privilege_type = 'SELECT'
                ORDER BY attribute_data.attname
                """
            ),
            {"relation": _PAYLOAD, "role_name": role_name},
        ).all()
    }


def _table_select(bind, role_name: str) -> bool:
    return bool(
        _scalar(
            bind,
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_class AS relation_data
                CROSS JOIN LATERAL pg_catalog.aclexplode(relation_data.relacl) AS acl_data
                JOIN pg_catalog.pg_roles AS grantee_role
                  ON grantee_role.oid = acl_data.grantee
                WHERE relation_data.oid = pg_catalog.to_regclass(:relation)
                  AND grantee_role.rolname = :role_name
                  AND acl_data.privilege_type = 'SELECT'
            )
            """,
            {"relation": _PAYLOAD, "role_name": role_name},
        )
    )


def _require_predecessor(bind) -> None:
    if _select_columns(bind, _SECURITY_OWNER):
        raise RuntimeError("app_security_owner unexpectedly reads secure payload columns")
    if _select_columns(bind, _API) or _table_select(bind, _API):
        raise RuntimeError("app_runtime unexpectedly reads secure registration payloads")
    if _table_select(bind, _SECURITY_OWNER):
        raise RuntimeError("app_security_owner unexpectedly has table-wide payload SELECT")


def _require_forward(bind) -> None:
    if _select_columns(bind, _SECURITY_OWNER) != _EXPECTED_SECURITY_SELECT:
        raise RuntimeError("app_security_owner payload SELECT must be registration_id only")
    if _table_select(bind, _SECURITY_OWNER):
        raise RuntimeError("app_security_owner must not have table-wide payload SELECT")
    if _select_columns(bind, _API) or _table_select(bind, _API):
        raise RuntimeError("app_runtime leaked direct secure payload SELECT")


def upgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    _require_predecessor(bind)
    bind.execute(
        sa.text(
            "GRANT SELECT (registration_id) "
            "ON TABLE public.organization_registration_payloads_secure "
            "TO app_security_owner"
        )
    )
    _require_forward(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    _require_forward(bind)
    bind.execute(
        sa.text(
            "REVOKE SELECT (registration_id) "
            "ON TABLE public.organization_registration_payloads_secure "
            "FROM app_security_owner"
        )
    )
    _require_predecessor(bind)
