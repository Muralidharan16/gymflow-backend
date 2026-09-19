"""PAY-2B additive Finance authority capability guard.

Revision ID: zl07d8e9f0a46
Revises: zk07d8e9f0a45
Create Date: 2026-09-19

This migration is deliberately expand-only.  It does not change financial
tables, payment/refund state, provider execution, or existing runtime grants.
It introduces one bounded app_secure capability guard for the dedicated PAY-2
Finance roles after proving those cluster roles were externally bootstrapped
with the exact reduced posture and have no pre-existing database privileges.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "zl07d8e9f0a46"
down_revision = "zk07d8e9f0a45"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_FINANCE_ROLES = (
    "finance_runtime",
    "finance_read_runtime",
    "payment_worker_runtime",
    "refund_runtime",
    "finance_reconciliation_runtime",
    "finance_maintenance_runtime",
)
_FUNCTION = "app_secure.require_finance_capability(text,boolean)"


def _require_reduced_role(bind, role_name: str) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT rolcanlogin, rolsuper, rolinherit, rolcreatedb, rolcreaterole,
                   rolreplication, rolbypassrls
            FROM pg_catalog.pg_roles
            WHERE rolname=:role_name
            """
        ),
        {"role_name": role_name},
    ).mappings().one_or_none()
    if row is None:
        raise RuntimeError(f"PAY-2B required cluster role is absent: {role_name}")
    if (
        row["rolcanlogin"]
        or row["rolsuper"]
        or row["rolinherit"]
        or row["rolcreatedb"]
        or row["rolcreaterole"]
        or row["rolreplication"]
        or row["rolbypassrls"]
    ):
        raise RuntimeError(f"PAY-2B reduced Finance role posture drift: {role_name}")


def _require_identity_contract(bind) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT session_user::text AS session_name,
                   current_user::text AS current_name,
                   rolsuper, rolinherit, rolcreatedb, rolcreaterole,
                   rolreplication, rolbypassrls
            FROM pg_catalog.pg_roles
            WHERE rolname=current_user
            """
        )
    ).mappings().one()
    if row["session_name"] != _MIGRATION_OWNER or row["current_name"] != _MIGRATION_OWNER:
        raise RuntimeError("PAY-2B migration requires migration_owner as session/current user")
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
        raise RuntimeError("PAY-2B migration_owner violates reduced-role contract")

    security_owner = bind.execute(
        sa.text(
            """
            SELECT rolcanlogin, rolsuper, rolinherit, rolcreatedb, rolcreaterole,
                   rolreplication, rolbypassrls
            FROM pg_catalog.pg_roles
            WHERE rolname=:role_name
            """
        ),
        {"role_name": _SECURITY_OWNER},
    ).mappings().one_or_none()
    if security_owner is None or any(bool(security_owner[key]) for key in security_owner):
        raise RuntimeError("PAY-2B app_security_owner violates reduced-role contract")
    if not bind.execute(
        sa.text(
            "SELECT pg_catalog.pg_has_role(:member,:target,'SET')"
        ),
        {"member": _MIGRATION_OWNER, "target": _SECURITY_OWNER},
    ).scalar_one():
        raise RuntimeError("PAY-2B migration_owner lacks bounded SET edge to app_security_owner")

    for role_name in _FINANCE_ROLES:
        _require_reduced_role(bind, role_name)
        for semantic in ("MEMBER", "USAGE", "SET"):
            if bind.execute(
                sa.text(
                    "SELECT pg_catalog.pg_has_role(:member,:target,:semantic)"
                ),
                {
                    "member": _MIGRATION_OWNER,
                    "target": role_name,
                    "semantic": semantic,
                },
            ).scalar_one():
                raise RuntimeError(
                    f"PAY-2B migration_owner leaked into Finance runtime role: "
                    f"{role_name}.{semantic}"
                )


def _require_clean_predecessor(bind) -> None:
    if bind.execute(
        sa.text(
            "SELECT pg_catalog.to_regprocedure(:signature) IS NOT NULL"
        ),
        {"signature": _FUNCTION},
    ).scalar_one():
        raise RuntimeError("PAY-2B capability guard already exists")

    for role_name in _FINANCE_ROLES:
        if bind.execute(
            sa.text(
                "SELECT pg_catalog.has_schema_privilege(:role,'app_secure','USAGE') "
                "OR pg_catalog.has_schema_privilege(:role,'app_secure','CREATE')"
            ),
            {"role": role_name},
        ).scalar_one():
            raise RuntimeError(
                f"PAY-2B refuses pre-existing app_secure schema authority: {role_name}"
            )
        if bind.execute(
            sa.text(
                "SELECT pg_catalog.has_schema_privilege(:role,'finance','USAGE') "
                "OR pg_catalog.has_schema_privilege(:role,'finance','CREATE')"
            ),
            {"role": role_name},
        ).scalar_one():
            raise RuntimeError(
                f"PAY-2B refuses pre-existing Finance schema authority: {role_name}"
            )
        if bind.execute(
            sa.text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.role_table_grants
                    WHERE grantee=:role
                      AND table_schema='finance'
                )
                """
            ),
            {"role": role_name},
        ).scalar_one():
            raise RuntimeError(
                f"PAY-2B refuses pre-existing direct Finance table authority: {role_name}"
            )
        if bind.execute(
            sa.text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.role_usage_grants
                    WHERE grantee=:role
                      AND object_schema='finance'
                )
                """
            ),
            {"role": role_name},
        ).scalar_one():
            raise RuntimeError(
                f"PAY-2B refuses pre-existing Finance sequence/domain authority: {role_name}"
            )
        if bind.execute(
            sa.text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.routine_privileges
                    WHERE grantee=:role
                      AND specific_schema='app_secure'
                )
                """
            ),
            {"role": role_name},
        ).scalar_one():
            raise RuntimeError(
                f"PAY-2B refuses pre-existing app_secure function authority: {role_name}"
            )


def _install_guard() -> None:
    op.execute("SET LOCAL ROLE app_security_owner")
    for role_name in _FINANCE_ROLES:
        op.execute(f"GRANT USAGE ON SCHEMA app_secure TO {role_name}")
    op.execute(
        r"""
        CREATE FUNCTION app_secure.require_finance_capability(
            p_capability text,
            p_require_tenant boolean DEFAULT true
        )
        RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path=pg_catalog,public,finance
        SET row_security=on
        AS $$
        DECLARE
            v_required_role text;
            v_current_org_id uuid;
            v_require_tenant boolean := coalesce(p_require_tenant, true);
        BEGIN
            CASE p_capability
                WHEN 'command' THEN
                    v_required_role := 'finance_runtime';
                WHEN 'read' THEN
                    v_required_role := 'finance_read_runtime';
                WHEN 'payment_worker' THEN
                    v_required_role := 'payment_worker_runtime';
                WHEN 'refund' THEN
                    v_required_role := 'refund_runtime';
                WHEN 'reconciliation' THEN
                    v_required_role := 'finance_reconciliation_runtime';
                WHEN 'maintenance' THEN
                    v_required_role := 'finance_maintenance_runtime';
                ELSE
                    RAISE EXCEPTION 'PAY2_FINANCE_CAPABILITY_UNKNOWN'
                        USING ERRCODE='22023';
            END CASE;

            IF NOT pg_catalog.pg_has_role(
                session_user,
                v_required_role,
                'MEMBER'
            ) THEN
                RAISE EXCEPTION 'PAY2_FINANCE_CAPABILITY_FORBIDDEN'
                    USING ERRCODE='42501';
            END IF;

            IF NOT v_require_tenant
               AND p_capability NOT IN ('reconciliation','maintenance') THEN
                RAISE EXCEPTION 'PAY2_FINANCE_TENANT_CONTEXT_REQUIRED'
                    USING ERRCODE='42501';
            END IF;

            IF NOT v_require_tenant THEN
                RETURN NULL;
            END IF;

            BEGIN
                v_current_org_id := NULLIF(
                    pg_catalog.current_setting('app.current_org_id', true),
                    ''
                )::uuid;
            EXCEPTION WHEN invalid_text_representation THEN
                RAISE EXCEPTION 'PAY2_FINANCE_TENANT_CONTEXT_INVALID'
                    USING ERRCODE='22023';
            END;

            IF v_current_org_id IS NULL THEN
                RAISE EXCEPTION 'PAY2_FINANCE_TENANT_CONTEXT_REQUIRED'
                    USING ERRCODE='42501';
            END IF;

            RETURN v_current_org_id;
        END;
        $$;
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "app_secure.require_finance_capability(text,boolean) FROM PUBLIC"
    )
    for role_name in _FINANCE_ROLES:
        op.execute(
            "GRANT EXECUTE ON FUNCTION "
            f"app_secure.require_finance_capability(text,boolean) TO {role_name}"
        )
    op.execute("RESET ROLE")


def _post_install_proof(bind) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT pg_catalog.pg_get_userbyid(p.proowner) AS owner,
                   p.prosecdef,
                   coalesce(p.proconfig::text,'') AS config,
                   EXISTS (
                       SELECT 1
                       FROM pg_catalog.aclexplode(
                           coalesce(
                               p.proacl,
                               pg_catalog.acldefault('f', p.proowner)
                           )
                       ) AS acl
                       WHERE acl.grantee=0
                         AND acl.privilege_type='EXECUTE'
                   ) AS public_execute
            FROM pg_catalog.pg_proc AS p
            WHERE p.oid=pg_catalog.to_regprocedure(:signature)
            """
        ),
        {"signature": _FUNCTION},
    ).mappings().one_or_none()
    if row is None:
        raise RuntimeError("PAY-2B capability guard missing after install")
    if row["owner"] != _SECURITY_OWNER or not row["prosecdef"]:
        raise RuntimeError("PAY-2B capability guard owner/security drift")
    if (
        "search_path=pg_catalog, public, finance" not in row["config"]
        and "search_path=pg_catalog,public,finance" not in row["config"]
    ):
        raise RuntimeError("PAY-2B capability guard search_path drift")
    if "row_security=on" not in row["config"]:
        raise RuntimeError("PAY-2B capability guard row_security drift")
    if row["public_execute"]:
        raise RuntimeError("PAY-2B capability guard leaked PUBLIC EXECUTE")

    for role_name in _FINANCE_ROLES:
        if not bind.execute(
            sa.text(
                "SELECT pg_catalog.has_schema_privilege(:role,'app_secure','USAGE')"
            ),
            {"role": role_name},
        ).scalar_one():
            raise RuntimeError(
                f"PAY-2B app_secure USAGE missing for {role_name}"
            )
        if not bind.execute(
            sa.text(
                "SELECT pg_catalog.has_function_privilege("
                ":role, pg_catalog.to_regprocedure(:signature), 'EXECUTE')"
            ),
            {"role": role_name, "signature": _FUNCTION},
        ).scalar_one():
            raise RuntimeError(
                f"PAY-2B guard EXECUTE missing for {role_name}"
            )
        if bind.execute(
            sa.text(
                "SELECT pg_catalog.has_schema_privilege(:role,'finance','USAGE') "
                "OR pg_catalog.has_schema_privilege(:role,'finance','CREATE')"
            ),
            {"role": role_name},
        ).scalar_one():
            raise RuntimeError(
                f"PAY-2B Finance schema authority leaked to {role_name}"
            )
        if bind.execute(
            sa.text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.role_table_grants
                    WHERE grantee=:role
                      AND table_schema='finance'
                )
                """
            ),
            {"role": role_name},
        ).scalar_one():
            raise RuntimeError(
                f"PAY-2B direct Finance table authority leaked to {role_name}"
            )

    if bind.execute(
        sa.text(
            "SELECT pg_catalog.has_function_privilege("
            ":role, pg_catalog.to_regprocedure(:signature), 'EXECUTE')"
        ),
        {"role": _MIGRATION_OWNER, "signature": _FUNCTION},
    ).scalar_one():
        raise RuntimeError("PAY-2B migration_owner can execute Finance runtime guard")


def upgrade() -> None:
    bind = op.get_bind()
    _require_identity_contract(bind)
    _require_clean_predecessor(bind)
    _install_guard()
    _post_install_proof(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_identity_contract(bind)
    if not bind.execute(
        sa.text(
            "SELECT pg_catalog.to_regprocedure(:signature) IS NOT NULL"
        ),
        {"signature": _FUNCTION},
    ).scalar_one():
        raise RuntimeError("PAY-2B downgrade requires installed capability guard")

    op.execute("SET LOCAL ROLE app_security_owner")
    for role_name in _FINANCE_ROLES:
        op.execute(
            "REVOKE EXECUTE ON FUNCTION "
            f"app_secure.require_finance_capability(text,boolean) FROM {role_name}"
        )
    op.execute(
        "DROP FUNCTION app_secure.require_finance_capability(text,boolean)"
    )
    for role_name in _FINANCE_ROLES:
        op.execute(f"REVOKE USAGE ON SCHEMA app_secure FROM {role_name}")
    op.execute("RESET ROLE")
