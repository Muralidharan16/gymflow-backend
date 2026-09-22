"""PAY-18 financial observability aggregate capability.

Revision ID: zz37d8e9f0a63
Revises: zz27d8e9f0a62
Create Date: 2026-09-22

PAY-18 adds observation only. It exposes bounded aggregate financial counts
through one SECURITY DEFINER function to the isolated lifecycle maintenance
runtime. No identifier, provider reference, customer data, payment data, or
business mutation is returned.

The security owner receives only SELECT authority required by the aggregate
function. Runtime identities cannot assume the security owner and receive no
new direct table privileges.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "zz37d8e9f0a63"
down_revision = "zz27d8e9f0a62"
branch_labels = None
depends_on = None


_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_MAINTENANCE = "lifecycle_maintenance_runtime"
_APP = "app_runtime"
_WORKER = "worker_runtime"
_SNAPSHOT_NAME = "pay18_financial_observability_snapshot"
_SNAPSHOT = "app_secure.pay18_financial_observability_snapshot()"

_POLICY_TABLES = (
    ("finance", "payment_events"),
    ("finance", "payment_application_records"),
    ("finance", "outbox_events"),
    ("public", "platform_payment_attempts"),
    ("public", "platform_refunds"),
    ("public", "platform_mandates"),
    ("public", "platform_dunning_cases"),
    ("public", "platform_disputes"),
    ("public", "platform_accounting_reconciliation_items"),
)

_COLUMN_GRANTS = {
    ("finance", "payment_events"): ("id", "event_type"),
    ("finance", "payment_application_records"): ("payment_event_id",),
    ("finance", "outbox_events"): ("status",),
    ("public", "platform_payment_attempts"): ("status",),
    ("public", "platform_refunds"): ("status",),
    ("public", "platform_mandates"): ("status", "replacement_mandate_id"),
    ("public", "platform_dunning_cases"): ("status", "stage"),
    ("public", "platform_disputes"): ("dispute_type", "status"),
    ("public", "platform_accounting_reconciliation_items"): (
        "object_type",
        "settlement_evidence_id",
        "mismatch_category",
        "resolution_status",
    ),
}


def _require_role(bind, role: str, *, login: bool = False) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT rolcanlogin,rolsuper,rolinherit,rolcreatedb,rolcreaterole,
                   rolreplication,rolbypassrls
            FROM pg_catalog.pg_roles
            WHERE rolname=:role
            """
        ),
        {"role": role},
    ).mappings().one_or_none()
    if row is None:
        raise RuntimeError(f"PAY-18 missing externally managed role: {role}")
    if bool(row["rolcanlogin"]) is not login:
        raise RuntimeError(f"PAY-18 role login posture drift: {role}")
    for key in (
        "rolsuper",
        "rolinherit",
        "rolcreatedb",
        "rolcreaterole",
        "rolreplication",
        "rolbypassrls",
    ):
        if bool(row[key]):
            raise RuntimeError(f"PAY-18 reduced-role drift: {role}.{key}")


def _require_identity(bind) -> None:
    _require_role(bind, _MIGRATION_OWNER, login=True)
    _require_role(bind, _SECURITY_OWNER)
    _require_role(bind, _MAINTENANCE)
    _require_role(bind, _APP)
    _require_role(bind, _WORKER)

    identity = bind.execute(
        sa.text("SELECT session_user::text,current_user::text")
    ).one()
    if tuple(identity) != (_MIGRATION_OWNER, _MIGRATION_OWNER):
        raise RuntimeError("PAY-18 migration requires migration_owner")

    if not bind.execute(
        sa.text("SELECT pg_catalog.pg_has_role(:member,:target,'SET')"),
        {"member": _MIGRATION_OWNER, "target": _SECURITY_OWNER},
    ).scalar_one():
        raise RuntimeError(
            "PAY-18 requires migration_owner SET edge to app_security_owner"
        )

    for runtime in (_MAINTENANCE, _APP, _WORKER):
        if bind.execute(
            sa.text(
                "SELECT pg_catalog.pg_has_role(:member,:target,'MEMBER') "
                "OR pg_catalog.pg_has_role(:member,:target,'SET')"
            ),
            {"member": runtime, "target": _SECURITY_OWNER},
        ).scalar_one():
            raise RuntimeError(
                f"PAY-18 runtime may reach app_security_owner: {runtime}"
            )


def _require_predecessor(bind) -> None:
    relations = (
        "finance.provider_webhook_inbox",
        "finance.payment_events",
        "finance.payment_application_records",
        "finance.outbox_events",
        "public.platform_payment_attempts",
        "public.platform_refunds",
        "public.platform_mandates",
        "public.platform_dunning_cases",
        "public.platform_disputes",
        "public.platform_accounting_reconciliation_items",
    )
    for relation in relations:
        if bind.execute(
            sa.text("SELECT pg_catalog.to_regclass(:relation) IS NULL"),
            {"relation": relation},
        ).scalar_one():
            raise RuntimeError(
                f"PAY-18 predecessor relation missing: {relation}"
            )

    if bind.execute(
        sa.text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_proc AS p
                JOIN pg_catalog.pg_namespace AS n
                  ON n.oid=p.pronamespace
                WHERE n.nspname='app_secure'
                  AND p.proname=:name
                  AND p.pronargs=0
            )
            """
        ),
        {"name": _SNAPSHOT_NAME},
    ).scalar_one():
        raise RuntimeError("PAY-18 observability snapshot already exists")


def _policy_name(schema_name: str, table_name: str) -> str:
    return f"pay18_observe_{schema_name}_{table_name}"


def _install_read_boundary(bind) -> None:
    for (schema_name, table_name), columns in _COLUMN_GRANTS.items():
        rendered = ",".join(columns)
        op.execute(
            f"GRANT SELECT({rendered}) ON TABLE "
            f"{schema_name}.{table_name} TO {_SECURITY_OWNER}"
        )

    # PAY-18 deliberately gives only the non-login security owner a global
    # SELECT policy. Runtime identities retain their existing tenant policies
    # and cannot SET/MEMBER into the security owner.
    for schema_name, table_name in _POLICY_TABLES:
        policy = _policy_name(schema_name, table_name)
        op.execute(
            f"""
            CREATE POLICY {policy}
            ON {schema_name}.{table_name}
            FOR SELECT
            TO {_SECURITY_OWNER}
            USING (true)
            """
        )

    had_create = bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.has_schema_privilege("
                "'app_security_owner','app_secure','CREATE')"
            )
        ).scalar_one()
    )
    if not had_create:
        op.execute("GRANT CREATE ON SCHEMA app_secure TO app_security_owner")

    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        op.execute(
            """
            CREATE FUNCTION app_secure.pay18_financial_observability_snapshot()
            RETURNS TABLE(
                payment_attempt_total bigint,
                payment_failure_total bigint,
                payment_unknown_total bigint,
                webhook_backlog bigint,
                payment_application_backlog bigint,
                finance_outbox_backlog bigint,
                platform_refund_backlog bigint,
                platform_refund_unknown_total bigint,
                settlement_mismatch_total bigint,
                reconciliation_open_total bigint,
                mandate_failed_total bigint,
                mandate_expired_total bigint,
                mandate_revoked_total bigint,
                dunning_full_grace_total bigint,
                dunning_limited_write_total bigint,
                dunning_read_only_total bigint,
                dunning_billing_only_total bigint,
                dunning_recovered_total bigint,
                chargeback_open_total bigint,
                duplicate_payment_allegation_open_total bigint
            )
            LANGUAGE sql
            STABLE
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
                WITH payment_attempts AS (
                    SELECT
                        count(*)::bigint AS total,
                        count(*) FILTER (WHERE status='failed')::bigint
                            AS failed,
                        count(*) FILTER (WHERE status='unknown')::bigint
                            AS unknown_count
                    FROM public.platform_payment_attempts
                ),
                webhook AS (
                    SELECT count(*)::bigint AS backlog
                    FROM finance.provider_webhook_inbox
                    WHERE status IN (
                        'received','processing','retry','dead_letter'
                    )
                ),
                application AS (
                    SELECT count(*)::bigint AS backlog
                    FROM finance.payment_events AS event_data
                    WHERE event_data.event_type IN (
                        'payment.captured','order.paid'
                    )
                      AND NOT EXISTS (
                        SELECT 1
                        FROM finance.payment_application_records AS applied
                        WHERE applied.payment_event_id=event_data.id
                    )
                ),
                outbox AS (
                    SELECT count(*)::bigint AS backlog
                    FROM finance.outbox_events
                    WHERE status IN ('pending','processing','failed')
                ),
                platform_refunds AS (
                    SELECT
                        count(*) FILTER (
                            WHERE status IN (
                                'requested','approved',
                                'provider_pending','unknown'
                            )
                        )::bigint AS backlog,
                        count(*) FILTER (
                            WHERE status='unknown'
                        )::bigint AS unknown_count
                    FROM public.platform_refunds
                ),
                reconciliation AS (
                    SELECT
                        count(*) FILTER (
                            WHERE mismatch_category IS NOT NULL
                              AND resolution_status<>'resolved'
                              AND (
                                  object_type IN (
                                      'settlement','gateway_fee','refund_fee'
                                  )
                                  OR settlement_evidence_id IS NOT NULL
                                  OR mismatch_category='settlement_missing'
                              )
                        )::bigint AS settlement_mismatch,
                        count(*) FILTER (
                            WHERE resolution_status<>'resolved'
                        )::bigint AS open_count
                    FROM public.platform_accounting_reconciliation_items
                ),
                mandates AS (
                    SELECT
                        count(*) FILTER (
                            WHERE status='failed'
                              AND replacement_mandate_id IS NULL
                        )::bigint AS failed,
                        count(*) FILTER (
                            WHERE status='expired'
                              AND replacement_mandate_id IS NULL
                        )::bigint AS expired,
                        count(*) FILTER (
                            WHERE status='revoked'
                              AND replacement_mandate_id IS NULL
                        )::bigint AS revoked
                    FROM public.platform_mandates
                ),
                dunning AS (
                    SELECT
                        count(*) FILTER (
                            WHERE stage='full_grace'
                              AND status IN ('open','suspended')
                        )::bigint AS full_grace,
                        count(*) FILTER (
                            WHERE stage='limited_write'
                              AND status IN ('open','suspended')
                        )::bigint AS limited_write,
                        count(*) FILTER (
                            WHERE stage='read_only'
                              AND status IN ('open','suspended')
                        )::bigint AS read_only_count,
                        count(*) FILTER (
                            WHERE stage='billing_only'
                              AND status IN ('open','suspended')
                        )::bigint AS billing_only,
                        count(*) FILTER (
                            WHERE stage='recovered'
                        )::bigint AS recovered
                    FROM public.platform_dunning_cases
                ),
                disputes AS (
                    SELECT
                        count(*) FILTER (
                            WHERE dispute_type='chargeback'
                              AND status IN (
                                  'opened','evidence_required',
                                  'submitted','under_review'
                              )
                        )::bigint AS chargeback_open,
                        count(*) FILTER (
                            WHERE dispute_type='duplicate_charge_allegation'
                              AND status IN (
                                  'opened','evidence_required',
                                  'submitted','under_review'
                              )
                        )::bigint AS duplicate_open
                    FROM public.platform_disputes
                )
                SELECT
                    p.total,
                    p.failed,
                    p.unknown_count,
                    w.backlog,
                    a.backlog,
                    o.backlog,
                    r.backlog,
                    r.unknown_count,
                    rec.settlement_mismatch,
                    rec.open_count,
                    m.failed,
                    m.expired,
                    m.revoked,
                    d.full_grace,
                    d.limited_write,
                    d.read_only_count,
                    d.billing_only,
                    d.recovered,
                    dp.chargeback_open,
                    dp.duplicate_open
                FROM payment_attempts AS p
                CROSS JOIN webhook AS w
                CROSS JOIN application AS a
                CROSS JOIN outbox AS o
                CROSS JOIN platform_refunds AS r
                CROSS JOIN reconciliation AS rec
                CROSS JOIN mandates AS m
                CROSS JOIN dunning AS d
                CROSS JOIN disputes AS dp
            $function$
            """
        )
        op.execute(f"REVOKE ALL ON FUNCTION {_SNAPSHOT} FROM PUBLIC")
        op.execute(
            f"GRANT EXECUTE ON FUNCTION {_SNAPSHOT} TO {_MAINTENANCE}"
        )
    finally:
        op.execute("RESET ROLE")

    if not had_create:
        op.execute("REVOKE CREATE ON SCHEMA app_secure FROM app_security_owner")


def _postflight(bind) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT p.oid, owner.rolname, p.prosecdef,
                   p.provolatile::text, p.proconfig
            FROM pg_catalog.pg_proc AS p
            JOIN pg_catalog.pg_namespace AS n
              ON n.oid=p.pronamespace
            JOIN pg_catalog.pg_roles AS owner
              ON owner.oid=p.proowner
            WHERE n.nspname='app_secure'
              AND p.proname=:name
              AND p.pronargs=0
            """
        ),
        {"name": _SNAPSHOT_NAME},
    ).one_or_none()
    if row is None:
        raise RuntimeError("PAY-18 observability snapshot missing")
    function_oid, owner, security_definer, volatility, config = row
    if owner != _SECURITY_OWNER or not security_definer:
        raise RuntimeError("PAY-18 snapshot owner/security drift")
    if volatility != "s":
        raise RuntimeError("PAY-18 snapshot must remain STABLE")
    config_values = set(config or ())
    if "row_security=on" not in config_values:
        raise RuntimeError("PAY-18 snapshot lost row_security=on")
    if not any(
        value.startswith("search_path=") for value in config_values
    ):
        raise RuntimeError("PAY-18 snapshot lost explicit search_path")

    if not bind.execute(
        sa.text(
            """
            SELECT pg_catalog.has_function_privilege(
                :role,CAST(:oid AS oid),'EXECUTE'
            )
            """
        ),
        {"role": _MAINTENANCE, "oid": function_oid},
    ).scalar_one():
        raise RuntimeError(
            "PAY-18 maintenance runtime missing snapshot EXECUTE"
        )

    for runtime in (_APP, _WORKER):
        if bind.execute(
            sa.text(
                """
                SELECT pg_catalog.has_function_privilege(
                    :role,CAST(:oid AS oid),'EXECUTE'
                )
                """
            ),
            {"role": runtime, "oid": function_oid},
        ).scalar_one():
            raise RuntimeError(
                f"PAY-18 unexpected snapshot EXECUTE for {runtime}"
            )

    for schema_name, table_name in _POLICY_TABLES:
        if bind.execute(
            sa.text(
                """
                SELECT pg_catalog.has_table_privilege(
                    :role,
                    pg_catalog.to_regclass(:relation),
                    'SELECT'
                )
                """
            ),
            {
                "role": _MAINTENANCE,
                "relation": f"{schema_name}.{table_name}",
            },
        ).scalar_one():
            raise RuntimeError(
                "PAY-18 maintenance runtime leaked direct SELECT: "
                f"{schema_name}.{table_name}"
            )


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _require_identity(bind)
    _require_predecessor(bind)
    _install_read_boundary(bind)
    _postflight(bind)


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _require_identity(bind)

    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        op.execute(f"REVOKE EXECUTE ON FUNCTION {_SNAPSHOT} FROM {_MAINTENANCE}")
        op.execute(f"DROP FUNCTION IF EXISTS {_SNAPSHOT}")
    finally:
        op.execute("RESET ROLE")

    for schema_name, table_name in reversed(_POLICY_TABLES):
        policy = _policy_name(schema_name, table_name)
        op.execute(
            f"DROP POLICY IF EXISTS {policy} "
            f"ON {schema_name}.{table_name}"
        )

    for (schema_name, table_name), columns in _COLUMN_GRANTS.items():
        rendered = ",".join(columns)
        op.execute(
            f"REVOKE SELECT({rendered}) ON TABLE "
            f"{schema_name}.{table_name} FROM {_SECURITY_OWNER}"
        )
