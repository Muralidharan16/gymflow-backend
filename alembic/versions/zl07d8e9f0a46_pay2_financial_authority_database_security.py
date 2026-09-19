"""PAY-2 financial authority and immutable evidence boundary.

Revision ID: zl07d8e9f0a46
Revises: zk07d8e9f0a45
Create Date: 2026-09-19

The cluster-managed Finance capability roles are provisioned outside Alembic
through security/cluster_role_bootstrap. This migration refuses to create,
alter, repair or modify PostgreSQL role-membership topology.

PAY-2 keeps all dedicated Finance runtime capabilities table-blind. It adds
database-level immutable-history guards to evidence that must never be rewritten
by an application/runtime identity while preserving migration_owner's explicit
migration/fixture authority. Live provider money movement remains disabled.
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
    "finance_payment_runtime",
    "finance_refund_runtime",
    "finance_reconciliation_runtime",
    "finance_read_runtime",
    "finance_maintenance_runtime",
)
_BLOCKED_DIRECT_ROLES = (
    "app_runtime",
    "app_user",
    "auth_runtime",
    "audit_writer",
    "branch_admin",
    "branch_viewer",
    "ops_support",
    "readonly_analytics",
    "worker_runtime",
    "lifecycle_maintenance_runtime",
    "finance_config_runtime",
) + _FINANCE_ROLES
_IMMUTABLE_TABLES = (
    "audit_events",
    "payment_events",
    "ledger_entry_lines",
    "tax_records",
    "credit_note_lines",
)
_IMMUTABLE_FUNCTION = "app_secure.pay2_reject_finance_immutable_history_mutation()"
_LEDGER_FUNCTION = "app_secure.pay2_guard_posted_finance_ledger_entry_mutation()"
_IMMUTABLE_TRIGGER = "pay2_immutable_finance_history_guard"
_LEDGER_TRIGGER = "pay2_posted_finance_ledger_guard"


def _require_reduced_role(bind, role_name: str, *, login: bool = False) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT rolcanlogin,rolsuper,rolinherit,rolcreatedb,rolcreaterole,
                   rolreplication,rolbypassrls
            FROM pg_catalog.pg_roles
            WHERE rolname=:role
            """
        ),
        {"role": role_name},
    ).mappings().one_or_none()
    if row is None:
        raise RuntimeError(f"PAY-2 missing externally managed role: {role_name}")
    if bool(row["rolcanlogin"]) is not login:
        raise RuntimeError(f"PAY-2 login posture drift: {role_name}")
    for key in ("rolsuper","rolinherit","rolcreatedb","rolcreaterole","rolreplication","rolbypassrls"):
        if bool(row[key]):
            raise RuntimeError(f"PAY-2 reduced-role contract drift: {role_name}.{key}")


def _has_direct_finance_relation_authority(bind, role_name: str) -> bool:
    return bool(
        bind.execute(
            sa.text(
                """
                SELECT
                    EXISTS (
                        SELECT 1
                        FROM pg_catalog.pg_class c
                        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                        WHERE n.nspname='finance'
                          AND c.relkind IN ('r','p','v','m')
                          AND (
                            pg_catalog.has_table_privilege(:role,c.oid,'SELECT')
                            OR pg_catalog.has_table_privilege(:role,c.oid,'INSERT')
                            OR pg_catalog.has_table_privilege(:role,c.oid,'UPDATE')
                            OR pg_catalog.has_table_privilege(:role,c.oid,'DELETE')
                            OR pg_catalog.has_table_privilege(:role,c.oid,'TRUNCATE')
                            OR pg_catalog.has_any_column_privilege(:role,c.oid,'SELECT')
                            OR pg_catalog.has_any_column_privilege(:role,c.oid,'INSERT')
                            OR pg_catalog.has_any_column_privilege(:role,c.oid,'UPDATE')
                        )
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM pg_catalog.pg_class c
                        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                        WHERE n.nspname='finance'
                          AND c.relkind='S'
                          AND (
                            pg_catalog.has_sequence_privilege(:role,c.oid,'USAGE')
                            OR pg_catalog.has_sequence_privilege(:role,c.oid,'SELECT')
                            OR pg_catalog.has_sequence_privilege(:role,c.oid,'UPDATE')
                          )
                    )
                """
            ),
            {"role": role_name},
        ).scalar_one()
    )


def _function_row(bind, signature: str):
    schema_name, remainder = signature.split(".", 1)
    function_name, argument_text = remainder.split("(", 1)
    normalized_args = argument_text.rstrip(")").replace(" ", "")
    return bind.execute(
        sa.text(
            """
            SELECT p.oid,owner.rolname AS owner,p.prosecdef,p.proconfig
            FROM pg_catalog.pg_proc p
            JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
            JOIN pg_catalog.pg_roles owner ON owner.oid=p.proowner
            WHERE n.nspname=:schema_name
              AND p.proname=:function_name
              AND replace(pg_catalog.oidvectortypes(p.proargtypes),' ','')=:normalized_args
            """
        ),
        {
            "schema_name": schema_name,
            "function_name": function_name,
            "normalized_args": normalized_args,
        },
    ).mappings().one_or_none()


def _has_finance_object_ownership(bind, role_name: str) -> bool:
    return bool(
        bind.execute(
            sa.text(
                """
                SELECT
                    EXISTS (
                        SELECT 1 FROM pg_catalog.pg_namespace n
                        JOIN pg_catalog.pg_roles r ON r.oid=n.nspowner
                        WHERE n.nspname IN ('finance','app_secure') AND r.rolname=:role
                    )
                    OR EXISTS (
                        SELECT 1 FROM pg_catalog.pg_class c
                        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                        JOIN pg_catalog.pg_roles r ON r.oid=c.relowner
                        WHERE n.nspname='finance' AND r.rolname=:role
                    )
                    OR EXISTS (
                        SELECT 1 FROM pg_catalog.pg_proc p
                        JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
                        JOIN pg_catalog.pg_roles r ON r.oid=p.proowner
                        WHERE n.nspname IN ('finance','app_secure') AND r.rolname=:role
                    )
                """
            ),
            {"role": role_name},
        ).scalar_one()
    )


def _require_predecessor(bind) -> None:
    _require_reduced_role(bind, _MIGRATION_OWNER, login=True)
    identity = bind.execute(sa.text("SELECT session_user::text,current_user::text")).one()
    if tuple(identity) != (_MIGRATION_OWNER, _MIGRATION_OWNER):
        raise RuntimeError("PAY-2 migration requires session_user=current_user=migration_owner")

    _require_reduced_role(bind, _SECURITY_OWNER)
    if not bool(bind.execute(
        sa.text("SELECT pg_catalog.pg_has_role(:member,:target,'SET')"),
        {"member": _MIGRATION_OWNER, "target": _SECURITY_OWNER},
    ).scalar_one()):
        raise RuntimeError("PAY-2 requires bounded migration_owner SET edge to app_security_owner")

    for role_name in _FINANCE_ROLES:
        _require_reduced_role(bind, role_name)
        if bool(bind.execute(
            sa.text(
                "SELECT pg_catalog.pg_has_role(:member,:owner,'MEMBER') "
                "OR pg_catalog.pg_has_role(:member,:owner,'SET') "
                "OR pg_catalog.pg_has_role(:owner,:member,'MEMBER') "
                "OR pg_catalog.pg_has_role(:owner,:member,'SET')"
            ),
            {"member": role_name, "owner": _MIGRATION_OWNER},
        ).scalar_one()):
            raise RuntimeError(f"PAY-2 Finance role has forbidden migration-owner edge: {role_name}")
        if bool(bind.execute(
            sa.text(
                "SELECT pg_catalog.pg_has_role(:member,:owner,'MEMBER') "
                "OR pg_catalog.pg_has_role(:member,:owner,'SET')"
            ),
            {"member": role_name, "owner": _SECURITY_OWNER},
        ).scalar_one()):
            raise RuntimeError(f"PAY-2 Finance role can reach app_security_owner: {role_name}")

    schema_owner = bind.execute(sa.text(
        "SELECT pg_catalog.pg_get_userbyid(nspowner) "
        "FROM pg_catalog.pg_namespace WHERE nspname='finance'"
    )).scalar_one_or_none()
    if schema_owner != _MIGRATION_OWNER:
        raise RuntimeError(f"PAY-2 finance schema owner drift: {schema_owner!r}")

    for table_name in (*_IMMUTABLE_TABLES, "ledger_entries"):
        if bind.execute(
            sa.text("SELECT pg_catalog.to_regclass(:relation) IS NULL"),
            {"relation": f"finance.{table_name}"},
        ).scalar_one():
            raise RuntimeError(f"PAY-2 missing Finance predecessor relation: {table_name}")

    for role_name in _BLOCKED_DIRECT_ROLES:
        _require_reduced_role(bind, role_name)
        if _has_direct_finance_relation_authority(bind, role_name):
            raise RuntimeError(f"PAY-2 refuses pre-existing direct Finance relation authority: {role_name}")
        if bool(bind.execute(
            sa.text("SELECT pg_catalog.has_schema_privilege(:role,'finance','USAGE') "
                    "OR pg_catalog.has_schema_privilege(:role,'finance','CREATE')"),
            {"role": role_name},
        ).scalar_one()):
            raise RuntimeError(f"PAY-2 refuses pre-existing Finance schema authority: {role_name}")
        if _has_finance_object_ownership(bind, role_name):
            raise RuntimeError(f"PAY-2 refuses Finance/app_secure object ownership by runtime role: {role_name}")

    for signature in (_IMMUTABLE_FUNCTION, _LEDGER_FUNCTION):
        if _function_row(bind, signature) is not None:
            raise RuntimeError(f"PAY-2 predecessor unexpectedly contains {signature}")


def _install(bind) -> None:
    guarded_tables = (*_IMMUTABLE_TABLES, "ledger_entries")
    for table_name in guarded_tables:
        if bind.execute(
            sa.text(
                "SELECT pg_catalog.has_table_privilege(:role,:relation,'TRIGGER')"
            ),
            {
                "role": _SECURITY_OWNER,
                "relation": f"finance.{table_name}",
            },
        ).scalar_one():
            raise RuntimeError(
                f"PAY-2 refuses pre-existing app_security_owner TRIGGER authority: {table_name}"
            )
        op.execute(
            f"GRANT TRIGGER ON TABLE finance.{table_name} TO app_security_owner"
        )

    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        op.execute(
            r"""
            CREATE FUNCTION app_secure.pay2_reject_finance_immutable_history_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,finance
            SET row_security=on
            AS $function$
            BEGIN
                IF session_user = 'migration_owner' THEN
                    IF TG_OP = 'DELETE' THEN
                        RETURN OLD;
                    END IF;
                    RETURN NEW;
                END IF;
                RAISE EXCEPTION
                    'PAY-2 immutable finance history rejects % on %.%',
                    TG_OP,TG_TABLE_SCHEMA,TG_TABLE_NAME
                    USING ERRCODE='42501';
            END
            $function$
            """
        )
        op.execute(
            r"""
            CREATE FUNCTION app_secure.pay2_guard_posted_finance_ledger_entry_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,finance
            SET row_security=on
            AS $function$
            BEGIN
                IF session_user = 'migration_owner' THEN
                    IF TG_OP = 'DELETE' THEN
                        RETURN OLD;
                    END IF;
                    RETURN NEW;
                END IF;
                IF OLD.status = 'posted' THEN
                    RAISE EXCEPTION
                        'PAY-2 posted ledger entry is immutable'
                        USING ERRCODE='42501';
                END IF;
                IF TG_OP = 'DELETE' THEN
                    RETURN OLD;
                END IF;
                RETURN NEW;
            END
            $function$
            """
        )
        op.execute(f"REVOKE ALL ON FUNCTION {_IMMUTABLE_FUNCTION} FROM PUBLIC")
        op.execute(f"REVOKE ALL ON FUNCTION {_LEDGER_FUNCTION} FROM PUBLIC")

        for table_name in _IMMUTABLE_TABLES:
            op.execute(
                f"CREATE TRIGGER {_IMMUTABLE_TRIGGER} "
                f"BEFORE UPDATE OR DELETE ON finance.{table_name} "
                "FOR EACH ROW EXECUTE FUNCTION "
                "app_secure.pay2_reject_finance_immutable_history_mutation()"
            )
        op.execute(
            f"CREATE TRIGGER {_LEDGER_TRIGGER} "
            "BEFORE UPDATE OR DELETE ON finance.ledger_entries "
            "FOR EACH ROW EXECUTE FUNCTION "
            "app_secure.pay2_guard_posted_finance_ledger_entry_mutation()"
        )
    finally:
        op.execute("RESET ROLE")

    for table_name in guarded_tables:
        op.execute(
            f"REVOKE TRIGGER ON TABLE finance.{table_name} FROM app_security_owner"
        )


def _post_install_proof(bind) -> None:
    for signature in (_IMMUTABLE_FUNCTION, _LEDGER_FUNCTION):
        row = _function_row(bind, signature)
        if row is None or row["owner"] != _SECURITY_OWNER or not bool(row["prosecdef"]):
            raise RuntimeError(f"PAY-2 hardened function metadata drift: {signature}")
        config = set(row["proconfig"] or ())
        if "row_security=on" not in config or not any(v.startswith("search_path=") for v in config):
            raise RuntimeError(f"PAY-2 hardened function configuration drift: {signature}")
        public_execute = bind.execute(
            sa.text(
                """
                SELECT EXISTS(
                    SELECT 1 FROM pg_catalog.aclexplode(
                        COALESCE((SELECT proacl FROM pg_catalog.pg_proc WHERE oid=:oid),
                                 pg_catalog.acldefault('f',(SELECT proowner FROM pg_catalog.pg_proc WHERE oid=:oid)))
                    ) acl
                    WHERE acl.grantee=0 AND acl.privilege_type='EXECUTE'
                )
                """
            ),
            {"oid": row["oid"]},
        ).scalar_one()
        if public_execute:
            raise RuntimeError(f"PAY-2 PUBLIC EXECUTE leaked: {signature}")

    expected = {(table_name, _IMMUTABLE_TRIGGER) for table_name in _IMMUTABLE_TABLES}
    expected.add(("ledger_entries", _LEDGER_TRIGGER))
    rows = bind.execute(sa.text(
        """
        SELECT c.relname,t.tgname,t.tgenabled
        FROM pg_catalog.pg_trigger t
        JOIN pg_catalog.pg_class c ON c.oid=t.tgrelid
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='finance'
          AND t.tgname IN (:immutable_trigger,:ledger_trigger)
          AND NOT t.tgisinternal
        """
    ), {
        "immutable_trigger": _IMMUTABLE_TRIGGER,
        "ledger_trigger": _LEDGER_TRIGGER,
    }).all()
    actual={(str(r[0]),str(r[1])) for r in rows if str(r[2]) == "O"}
    if actual != expected:
        raise RuntimeError(f"PAY-2 immutable trigger inventory drift: {sorted(actual)!r}")

    for table_name in (*_IMMUTABLE_TABLES, "ledger_entries"):
        if bind.execute(
            sa.text(
                "SELECT pg_catalog.has_table_privilege(:role,:relation,'TRIGGER')"
            ),
            {
                "role": _SECURITY_OWNER,
                "relation": f"finance.{table_name}",
            },
        ).scalar_one():
            raise RuntimeError(
                f"PAY-2 installation-only TRIGGER privilege leaked: {table_name}"
            )

    for role_name in _BLOCKED_DIRECT_ROLES:
        if _has_direct_finance_relation_authority(bind, role_name):
            raise RuntimeError(f"PAY-2 direct Finance authority leaked: {role_name}")
        if _has_finance_object_ownership(bind, role_name):
            raise RuntimeError(f"PAY-2 Finance object ownership leaked: {role_name}")


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _require_predecessor(bind)
    _install(bind)
    _post_install_proof(bind)


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _require_reduced_role(bind, _MIGRATION_OWNER, login=True)
    identity = bind.execute(sa.text("SELECT session_user::text,current_user::text")).one()
    if tuple(identity) != (_MIGRATION_OWNER, _MIGRATION_OWNER):
        raise RuntimeError("PAY-2 downgrade requires migration_owner")

    op.execute(f"DROP TRIGGER IF EXISTS {_LEDGER_TRIGGER} ON finance.ledger_entries")
    for table_name in reversed(_IMMUTABLE_TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS {_IMMUTABLE_TRIGGER} ON finance.{table_name}")

    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute(f"DROP FUNCTION IF EXISTS {_LEDGER_FUNCTION}")
    op.execute(f"DROP FUNCTION IF EXISTS {_IMMUTABLE_FUNCTION}")
    op.execute("RESET ROLE")
