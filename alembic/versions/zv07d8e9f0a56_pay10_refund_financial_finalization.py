"""PAY-10-D atomic refund financial finalization.

Revision ID: zv07d8e9f0a56
Revises: zu07d8e9f0a55
Create Date: 2026-09-20

PAY-10-D binds already-issued credit-note accounting provenance and exposes one
bounded atomic financial-finalization capability. It never issues statutory
credit notes and never performs provider network I/O.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "zv07d8e9f0a56"
down_revision = "zu07d8e9f0a55"
branch_labels = None
depends_on = None


_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_REFUND_RUNTIME = "finance_refund_runtime"
_ACL_STATE = "app_private.pay10d_acl_delta"

_LINK = "app_secure.link_pay10_refund_credit_note(uuid,uuid)"
_FINALIZE = "app_secure.finalize_pay10_refund(uuid)"
_FUNCTIONS = (_LINK, _FINALIZE)

_RUNTIME_ROLES = (
    "app_runtime",
    "worker_runtime",
    "finance_payment_runtime",
    "finance_refund_runtime",
    "finance_reconciliation_runtime",
    "finance_maintenance_runtime",
)

_COLUMN_ACL = {
    ("credit_notes", "SELECT"): (
        "id",
        "organization_id",
        "invoice_id",
        "legal_entity_id",
        "division_id",
        "brand_id",
        "status",
        "total_amount",
        "credit_note_number",
        "issued_at",
    ),
    ("invoices", "SELECT"): (
        "id",
        "organization_id",
        "legal_entity_id",
        "division_id",
        "brand_id",
        "currency_code",
    ),
    ("payment_allocations", "SELECT"): (
        "payment_id",
        "invoice_id",
        "allocated_amount",
    ),
    ("ledger_accounts", "SELECT"): (
        "id",
        "legal_entity_id",
        "code",
    ),
    ("ledger_entries", "SELECT"): (
        "id",
        "legal_entity_id",
        "division_id",
        "brand_id",
        "entry_type",
        "source_type",
        "source_id",
        "status",
        "posted_at",
    ),
    ("ledger_entries", "INSERT"): (
        "legal_entity_id",
        "division_id",
        "brand_id",
        "entry_type",
        "source_type",
        "source_id",
        "status",
        "posted_at",
    ),
    ("ledger_entry_lines", "SELECT"): (
        "ledger_entry_id",
        "ledger_account_id",
        "debit_amount",
        "credit_amount",
    ),
    ("ledger_entry_lines", "INSERT"): (
        "ledger_entry_id",
        "ledger_account_id",
        "debit_amount",
        "credit_amount",
        "memo",
    ),
    ("outbox_events", "INSERT"): (
        "organization_id",
        "legal_entity_id",
        "division_id",
        "brand_id",
        "aggregate_type",
        "aggregate_id",
        "event_type",
        "idempotency_key",
        "payload_json",
        "payload_sha256",
        "status",
    ),
}


def _require_reduced_role(bind, role: str, *, login: bool = False) -> None:
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
        raise RuntimeError(f"PAY-10-D missing externally managed role: {role}")
    if bool(row["rolcanlogin"]) is not login:
        raise RuntimeError(f"PAY-10-D login posture drift: {role}")
    for key in (
        "rolsuper",
        "rolinherit",
        "rolcreatedb",
        "rolcreaterole",
        "rolreplication",
        "rolbypassrls",
    ):
        if bool(row[key]):
            raise RuntimeError(f"PAY-10-D reduced-role drift: {role}.{key}")


def _require_identity(bind) -> None:
    _require_reduced_role(bind, _MIGRATION_OWNER, login=True)
    _require_reduced_role(bind, _SECURITY_OWNER)
    _require_reduced_role(bind, _REFUND_RUNTIME)
    identity = bind.execute(
        sa.text("SELECT session_user::text,current_user::text")
    ).one()
    if tuple(identity) != (_MIGRATION_OWNER, _MIGRATION_OWNER):
        raise RuntimeError(
            "PAY-10-D migration requires "
            "session_user=current_user=migration_owner"
        )
    if not bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.pg_has_role("
                "'migration_owner','app_security_owner','SET')"
            )
        ).scalar_one()
    ):
        raise RuntimeError(
            "PAY-10-D requires bounded migration_owner SET edge "
            "to app_security_owner"
        )
    for role in _RUNTIME_ROLES:
        if bool(
            bind.execute(
                sa.text(
                    "SELECT pg_catalog.pg_has_role("
                    ":role,'app_security_owner','SET') "
                    "OR pg_catalog.pg_has_role("
                    ":role,'app_security_owner','MEMBER')"
                ),
                {"role": role},
            ).scalar_one()
        ):
            raise RuntimeError(
                f"PAY-10-D runtime may reach app_security_owner: {role}"
            )


def _require_predecessor(bind) -> None:
    for relation in (
        "finance.refund_execution_commands",
        "finance.refunds",
        "finance.payments",
        "finance.payment_allocations",
        "finance.refund_provider_evidence",
        "finance.refund_credit_note_links",
        "finance.credit_notes",
        "finance.invoices",
        "finance.ledger_accounts",
        "finance.ledger_entries",
        "finance.ledger_entry_lines",
        "finance.outbox_events",
    ):
        if bool(
            bind.execute(
                sa.text("SELECT pg_catalog.to_regclass(:r) IS NULL"),
                {"r": relation},
            ).scalar_one()
        ):
            raise RuntimeError(
                f"PAY-10-D missing predecessor relation: {relation}"
            )

    for relation, privilege in (
        ("finance.refund_execution_commands", "SELECT"),
        ("finance.refund_execution_commands", "UPDATE"),
        ("finance.refunds", "SELECT"),
        ("finance.refunds", "UPDATE"),
        ("finance.payments", "SELECT"),
        ("finance.payments", "UPDATE"),
        ("finance.refund_provider_evidence", "SELECT"),
        ("finance.refund_credit_note_links", "SELECT"),
        ("finance.refund_credit_note_links", "INSERT"),
    ):
        if not bool(
            bind.execute(
                sa.text(
                    "SELECT pg_catalog.has_table_privilege("
                    "'app_security_owner',:relation,:privilege)"
                ),
                {"relation": relation, "privilege": privilege},
            ).scalar_one()
        ):
            raise RuntimeError(
                "PAY-10-D inherited owner authority missing: "
                f"{privilege} {relation}"
            )

    duplicate = bind.execute(
        sa.text(
            """
            SELECT source_id
            FROM finance.ledger_entries
            WHERE source_type='refund'
              AND status='posted'
            GROUP BY source_id
            HAVING count(*)>1
            LIMIT 1
            """
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise RuntimeError(
            "PAY-10-D refuses duplicate predecessor posted refund ledgers"
        )

    if bool(
        bind.execute(
            sa.text(
                """
                SELECT EXISTS(
                    SELECT 1
                    FROM pg_catalog.pg_indexes
                    WHERE schemaname='finance'
                      AND indexname='uq_pay10_refund_ledger_source'
                )
                """
            )
        ).scalar_one()
    ):
        raise RuntimeError(
            "PAY-10-D predecessor already has refund ledger uniqueness"
        )

    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        for signature in _FUNCTIONS:
            if bool(
                bind.execute(
                    sa.text(
                        "SELECT pg_catalog.to_regprocedure(:s) IS NOT NULL"
                    ),
                    {"s": signature},
                ).scalar_one()
            ):
                raise RuntimeError(
                    f"PAY-10-D predecessor already has {signature}"
                )
    finally:
        op.execute("RESET ROLE")


def _install_acl_state(bind) -> None:
    if bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.to_regclass(:r) IS NOT NULL"
            ),
            {"r": _ACL_STATE},
        ).scalar_one()
    ):
        raise RuntimeError("PAY-10-D ACL delta table already exists")
    op.execute(
        """
        CREATE TABLE app_private.pay10d_acl_delta(
            table_name text NOT NULL,
            column_name text NOT NULL,
            privilege_name text NOT NULL,
            PRIMARY KEY(table_name,column_name,privilege_name),
            CHECK (privilege_name IN ('SELECT','INSERT'))
        )
        """
    )
    op.execute(
        "REVOKE ALL ON TABLE app_private.pay10d_acl_delta FROM PUBLIC"
    )

    for (table_name, privilege), columns in _COLUMN_ACL.items():
        for column_name in columns:
            present = bool(
                bind.execute(
                    sa.text(
                        """
                        SELECT pg_catalog.has_column_privilege(
                            'app_security_owner',
                            :relation,
                            :column_name,
                            :privilege_name
                        )
                        """
                    ),
                    {
                        "relation": f"finance.{table_name}",
                        "column_name": column_name,
                        "privilege_name": privilege,
                    },
                ).scalar_one()
            )
            if present:
                continue
            bind.execute(
                sa.text(
                    """
                    INSERT INTO app_private.pay10d_acl_delta(
                        table_name,column_name,privilege_name
                    ) VALUES(:table_name,:column_name,:privilege_name)
                    """
                ),
                {
                    "table_name": table_name,
                    "column_name": column_name,
                    "privilege_name": privilege,
                },
            )
            op.execute(
                f"GRANT {privilege} ({column_name}) "
                f"ON TABLE finance.{table_name} "
                "TO app_security_owner"
            )


def _install_functions() -> None:
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        op.execute(
            r"""
            CREATE FUNCTION app_secure.link_pay10_refund_credit_note(
                p_refund_id uuid,
                p_credit_note_id uuid
            )
            RETURNS TABLE(
                link_id uuid,
                refund_id uuid,
                credit_note_id uuid,
                linked_amount numeric,
                total_backing numeric,
                replayed boolean
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_payment_id uuid;
                v_payment finance.payments%ROWTYPE;
                v_refund finance.refunds%ROWTYPE;
                v_command finance.refund_execution_commands%ROWTYPE;
                v_existing record;
                v_cn_org uuid;
                v_cn_invoice uuid;
                v_cn_entity uuid;
                v_cn_division uuid;
                v_cn_brand uuid;
                v_cn_status text;
                v_cn_amount numeric(14,2);
                v_cn_number text;
                v_cn_issued_at timestamptz;
                v_invoice_org uuid;
                v_invoice_entity uuid;
                v_invoice_division uuid;
                v_invoice_brand uuid;
                v_invoice_currency text;
                v_credit_ledger_id uuid;
                v_debits numeric(14,2);
                v_credits numeric(14,2);
                v_ar_credit_count integer;
                v_backing numeric(14,2);
                v_link_amount numeric(14,2);
                v_link_id uuid;
            BEGIN
                IF NOT pg_catalog.pg_has_role(
                    session_user,'finance_refund_runtime','MEMBER'
                ) THEN
                    RAISE EXCEPTION
                        'PAY-10-D credit-note binding requires finance_refund_runtime'
                        USING ERRCODE='42501';
                END IF;
                IF p_refund_id IS NULL OR p_credit_note_id IS NULL THEN
                    RAISE EXCEPTION
                        'PAY-10-D credit-note binding requires identities'
                        USING ERRCODE='22023';
                END IF;

                SELECT r.payment_id
                INTO v_payment_id
                FROM finance.refunds r
                WHERE r.id=p_refund_id;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'PAY-10-D refund not found'
                        USING ERRCODE='P0002';
                END IF;

                SELECT p.*
                INTO v_payment
                FROM finance.payments p
                WHERE p.id=v_payment_id
                FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'PAY-10-D payment not found'
                        USING ERRCODE='P0002';
                END IF;

                SELECT r.*
                INTO v_refund
                FROM finance.refunds r
                WHERE r.id=p_refund_id
                FOR UPDATE;
                IF NOT FOUND OR v_refund.payment_id<>v_payment.id THEN
                    RAISE EXCEPTION
                        'PAY-10-D refund/payment authority drift'
                        USING ERRCODE='23514';
                END IF;

                SELECT c.*
                INTO v_command
                FROM finance.refund_execution_commands c
                WHERE c.refund_id=v_refund.id
                FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'PAY-10-D refund command not found'
                        USING ERRCODE='P0002';
                END IF;

                IF v_command.status IN (
                    'succeeded','rejected','dead_lettered','cancelled'
                )
                   OR v_refund.status IN (
                       'succeeded','rejected','failed','cancelled'
                   )
                THEN
                    RAISE EXCEPTION
                        'PAY-10-D terminal refund cannot change credit-note backing'
                        USING ERRCODE='23514';
                END IF;

                SELECT l.id,l.refund_id,l.amount
                INTO v_existing
                FROM finance.refund_credit_note_links l
                WHERE l.credit_note_id=p_credit_note_id;
                IF FOUND THEN
                    IF v_existing.refund_id<>v_refund.id THEN
                        RAISE EXCEPTION
                            'PAY-10-D credit note already backs another refund'
                            USING ERRCODE='23505';
                    END IF;
                    SELECT coalesce(sum(l.amount),0)
                    INTO v_backing
                    FROM finance.refund_credit_note_links l
                    WHERE l.refund_id=v_refund.id;
                    RETURN QUERY SELECT
                        v_existing.id,
                        v_refund.id,
                        p_credit_note_id,
                        v_existing.amount,
                        v_backing,
                        true;
                    RETURN;
                END IF;

                SELECT
                    cn.organization_id,
                    cn.invoice_id,
                    cn.legal_entity_id,
                    cn.division_id,
                    cn.brand_id,
                    cn.status,
                    cn.total_amount,
                    cn.credit_note_number,
                    cn.issued_at
                INTO
                    v_cn_org,
                    v_cn_invoice,
                    v_cn_entity,
                    v_cn_division,
                    v_cn_brand,
                    v_cn_status,
                    v_cn_amount,
                    v_cn_number,
                    v_cn_issued_at
                FROM finance.credit_notes cn
                WHERE cn.id=p_credit_note_id;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'PAY-10-D credit note not found'
                        USING ERRCODE='P0002';
                END IF;

                IF v_cn_status<>'issued'
                   OR v_cn_number IS NULL
                   OR v_cn_issued_at IS NULL
                   OR v_cn_amount<=0
                   OR v_cn_org IS DISTINCT FROM v_refund.organization_id
                   OR v_cn_entity IS DISTINCT FROM v_refund.legal_entity_id
                   OR v_cn_division IS DISTINCT FROM v_refund.division_id
                   OR v_cn_brand IS DISTINCT FROM v_refund.brand_id
                THEN
                    RAISE EXCEPTION
                        'PAY-10-D credit note is not valid issued backing'
                        USING ERRCODE='23514';
                END IF;

                SELECT
                    i.organization_id,
                    i.legal_entity_id,
                    i.division_id,
                    i.brand_id,
                    i.currency_code
                INTO
                    v_invoice_org,
                    v_invoice_entity,
                    v_invoice_division,
                    v_invoice_brand,
                    v_invoice_currency
                FROM finance.invoices i
                WHERE i.id=v_cn_invoice;
                IF NOT FOUND
                   OR v_invoice_org IS DISTINCT FROM v_refund.organization_id
                   OR v_invoice_entity IS DISTINCT FROM v_refund.legal_entity_id
                   OR v_invoice_division IS DISTINCT FROM v_refund.division_id
                   OR v_invoice_brand IS DISTINCT FROM v_refund.brand_id
                   OR v_invoice_currency IS DISTINCT FROM v_refund.currency_code
                THEN
                    RAISE EXCEPTION
                        'PAY-10-D credit-note invoice authority mismatch'
                        USING ERRCODE='23514';
                END IF;

                PERFORM 1
                FROM finance.payment_allocations pa
                WHERE pa.payment_id=v_payment.id
                  AND pa.invoice_id=v_cn_invoice
                  AND pa.allocated_amount>0;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'PAY-10-D credit note invoice was not paid by refund payment'
                        USING ERRCODE='23514';
                END IF;

                SELECT le.id
                INTO v_credit_ledger_id
                FROM finance.ledger_entries le
                WHERE le.source_type='credit_note'
                  AND le.source_id=p_credit_note_id
                  AND le.status='posted';
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'PAY-10-D credit note lacks posted accounting reversal'
                        USING ERRCODE='23514';
                END IF;

                SELECT
                    coalesce(sum(ll.debit_amount),0),
                    coalesce(sum(ll.credit_amount),0),
                    count(*) FILTER (
                        WHERE la.code='AR'
                          AND ll.debit_amount=0
                          AND ll.credit_amount=v_cn_amount
                    )
                INTO v_debits,v_credits,v_ar_credit_count
                FROM finance.ledger_entry_lines ll
                JOIN finance.ledger_accounts la
                  ON la.id=ll.ledger_account_id
                WHERE ll.ledger_entry_id=v_credit_ledger_id;
                IF v_debits<>v_cn_amount
                   OR v_credits<>v_cn_amount
                   OR v_ar_credit_count<>1
                THEN
                    RAISE EXCEPTION
                        'PAY-10-D credit-note accounting reversal is incomplete'
                        USING ERRCODE='23514';
                END IF;

                SELECT coalesce(sum(l.amount),0)
                INTO v_backing
                FROM finance.refund_credit_note_links l
                WHERE l.refund_id=v_refund.id;

                IF v_backing>=v_refund.amount THEN
                    RAISE EXCEPTION
                        'PAY-10-D refund already has full credit-note backing'
                        USING ERRCODE='23514';
                END IF;

                v_link_amount:=least(
                    v_cn_amount,
                    v_refund.amount-v_backing
                );
                IF v_link_amount<=0 THEN
                    RAISE EXCEPTION
                        'PAY-10-D computed credit-note backing is invalid'
                        USING ERRCODE='23514';
                END IF;

                INSERT INTO finance.refund_credit_note_links(
                    organization_id,
                    refund_id,
                    credit_note_id,
                    amount
                )
                VALUES(
                    v_refund.organization_id,
                    v_refund.id,
                    p_credit_note_id,
                    v_link_amount
                )
                RETURNING id INTO v_link_id;

                v_backing:=v_backing+v_link_amount;
                RETURN QUERY SELECT
                    v_link_id,
                    v_refund.id,
                    p_credit_note_id,
                    v_link_amount,
                    v_backing,
                    false;
            END
            $function$
            """
        )

        op.execute(
            r"""
            CREATE FUNCTION app_secure.finalize_pay10_refund(
                p_command_id uuid
            )
            RETURNS TABLE(
                command_id uuid,
                refund_id uuid,
                payment_id uuid,
                ledger_entry_id uuid,
                refund_status text,
                payment_status text,
                command_status text,
                replayed boolean
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_payment_id uuid;
                v_payment finance.payments%ROWTYPE;
                v_refund finance.refunds%ROWTYPE;
                v_command finance.refund_execution_commands%ROWTYPE;
                v_ledger_id uuid;
                v_ledger_count integer;
                v_line_count integer;
                v_debits numeric(14,2);
                v_credits numeric(14,2);
                v_ar_debit_count integer;
                v_clearing_credit_count integer;
                v_processed_count integer;
                v_backing numeric(14,2);
                v_invalid_backing integer;
                v_allocated numeric(14,2);
                v_successful numeric(14,2);
                v_new_payment_status text;
                v_ar_account uuid;
                v_clearing_account uuid;
                v_payload jsonb;
                v_hash text;
                v_rows integer;
            BEGIN
                IF NOT pg_catalog.pg_has_role(
                    session_user,'finance_refund_runtime','MEMBER'
                ) THEN
                    RAISE EXCEPTION
                        'PAY-10-D finalization requires finance_refund_runtime'
                        USING ERRCODE='42501';
                END IF;
                IF p_command_id IS NULL THEN
                    RAISE EXCEPTION
                        'PAY-10-D finalization requires command identity'
                        USING ERRCODE='22023';
                END IF;

                SELECT c.payment_id
                INTO v_payment_id
                FROM finance.refund_execution_commands c
                WHERE c.command_id=p_command_id;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'PAY-10-D command not found'
                        USING ERRCODE='P0002';
                END IF;

                SELECT p.*
                INTO v_payment
                FROM finance.payments p
                WHERE p.id=v_payment_id
                FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'PAY-10-D payment not found'
                        USING ERRCODE='P0002';
                END IF;

                SELECT r.*
                INTO v_refund
                FROM finance.refunds r
                JOIN finance.refund_execution_commands c
                  ON c.refund_id=r.id
                WHERE c.command_id=p_command_id
                FOR UPDATE OF r;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'PAY-10-D refund not found'
                        USING ERRCODE='P0002';
                END IF;

                SELECT c.*
                INTO v_command
                FROM finance.refund_execution_commands c
                WHERE c.command_id=p_command_id
                FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'PAY-10-D command not found'
                        USING ERRCODE='P0002';
                END IF;

                IF v_command.payment_id<>v_payment.id
                   OR v_command.refund_id<>v_refund.id
                   OR v_refund.payment_id<>v_payment.id
                   OR v_command.organization_id
                        IS DISTINCT FROM v_payment.organization_id
                   OR v_refund.organization_id
                        IS DISTINCT FROM v_payment.organization_id
                   OR v_command.legal_entity_id
                        IS DISTINCT FROM v_payment.legal_entity_id
                   OR v_refund.legal_entity_id
                        IS DISTINCT FROM v_payment.legal_entity_id
                   OR v_command.division_id
                        IS DISTINCT FROM v_payment.division_id
                   OR v_refund.division_id
                        IS DISTINCT FROM v_payment.division_id
                   OR v_command.brand_id
                        IS DISTINCT FROM v_payment.brand_id
                   OR v_refund.brand_id
                        IS DISTINCT FROM v_payment.brand_id
                   OR v_command.amount IS DISTINCT FROM v_refund.amount
                   OR v_command.currency_code
                        IS DISTINCT FROM v_payment.currency_code
                   OR v_refund.currency_code
                        IS DISTINCT FROM v_payment.currency_code
                THEN
                    RAISE EXCEPTION
                        'PAY-10-D financial authority drift'
                        USING ERRCODE='23514';
                END IF;

                SELECT count(*),min(le.id)
                INTO v_ledger_count,v_ledger_id
                FROM finance.ledger_entries le
                WHERE le.source_type='refund'
                  AND le.source_id=v_refund.id
                  AND le.status='posted';

                IF v_command.status='succeeded' THEN
                    IF v_refund.status<>'succeeded'
                       OR v_command.completed_at IS NULL
                       OR v_ledger_count<>1
                    THEN
                        RAISE EXCEPTION
                            'PAY-10-D terminal replay state is incomplete'
                            USING ERRCODE='23514';
                    END IF;

                    SELECT
                        count(*),
                        coalesce(sum(ll.debit_amount),0),
                        coalesce(sum(ll.credit_amount),0),
                        count(*) FILTER (
                            WHERE la.code='AR'
                              AND ll.debit_amount=v_refund.amount
                              AND ll.credit_amount=0
                        ),
                        count(*) FILTER (
                            WHERE la.code='PAYMENT_CLEARING'
                              AND ll.debit_amount=0
                              AND ll.credit_amount=v_refund.amount
                        )
                    INTO
                        v_line_count,
                        v_debits,
                        v_credits,
                        v_ar_debit_count,
                        v_clearing_credit_count
                    FROM finance.ledger_entry_lines ll
                    JOIN finance.ledger_accounts la
                      ON la.id=ll.ledger_account_id
                    WHERE ll.ledger_entry_id=v_ledger_id;
                    IF v_line_count<>2
                       OR v_debits<>v_refund.amount
                       OR v_credits<>v_refund.amount
                       OR v_ar_debit_count<>1
                       OR v_clearing_credit_count<>1
                    THEN
                        RAISE EXCEPTION
                            'PAY-10-D terminal refund ledger drift'
                            USING ERRCODE='23514';
                    END IF;

                    RETURN QUERY SELECT
                        v_command.command_id,
                        v_refund.id,
                        v_payment.id,
                        v_ledger_id,
                        v_refund.status::text,
                        v_payment.status::text,
                        v_command.status::text,
                        true;
                    RETURN;
                END IF;

                IF v_command.status<>'reconciliation_pending'
                   OR v_refund.status<>'processing'
                   OR v_command.leased_by IS NOT NULL
                   OR v_command.leased_until IS NOT NULL
                   OR v_command.provider_refund_ref IS NULL
                   OR v_command.provider_evidence_sha256 IS NULL
                   OR v_command.provider_accepted_at IS NULL
                   OR v_command.request_sha256 IS NULL
                   OR v_payment.status NOT IN (
                       'captured','settled','partially_refunded'
                   )
                THEN
                    RAISE EXCEPTION
                        'PAY-10-D refund is not financially finalizable'
                        USING ERRCODE='23514';
                END IF;

                IF v_ledger_count<>0 THEN
                    RAISE EXCEPTION
                        'PAY-10-D fresh finalization found preexisting refund ledger'
                        USING ERRCODE='23505';
                END IF;

                SELECT count(*)
                INTO v_processed_count
                FROM finance.refund_provider_evidence e
                WHERE e.command_id=v_command.command_id
                  AND e.refund_id=v_refund.id
                  AND e.payment_id=v_payment.id
                  AND e.organization_id=v_payment.organization_id
                  AND e.provider_code=v_command.provider_code
                  AND e.provider_refund_ref=v_command.provider_refund_ref
                  AND e.request_sha256=v_command.request_sha256
                  AND e.evidence_sha256=v_command.provider_evidence_sha256
                  AND e.normalized_status='processed';
                IF v_processed_count<>1 THEN
                    RAISE EXCEPTION
                        'PAY-10-D authoritative processed provider evidence missing'
                        USING ERRCODE='23514';
                END IF;

                SELECT
                    coalesce(sum(l.amount),0),
                    count(*) FILTER (
                        WHERE
                            cn.status<>'issued'
                            OR cn.credit_note_number IS NULL
                            OR cn.issued_at IS NULL
                            OR l.amount>cn.total_amount
                            OR cn.organization_id
                                IS DISTINCT FROM v_refund.organization_id
                            OR cn.legal_entity_id
                                IS DISTINCT FROM v_refund.legal_entity_id
                            OR cn.division_id
                                IS DISTINCT FROM v_refund.division_id
                            OR cn.brand_id
                                IS DISTINCT FROM v_refund.brand_id
                            OR i.organization_id
                                IS DISTINCT FROM v_refund.organization_id
                            OR i.legal_entity_id
                                IS DISTINCT FROM v_refund.legal_entity_id
                            OR i.division_id
                                IS DISTINCT FROM v_refund.division_id
                            OR i.brand_id
                                IS DISTINCT FROM v_refund.brand_id
                            OR i.currency_code
                                IS DISTINCT FROM v_refund.currency_code
                            OR NOT EXISTS(
                                SELECT 1
                                FROM finance.payment_allocations pa
                                WHERE pa.payment_id=v_payment.id
                                  AND pa.invoice_id=cn.invoice_id
                                  AND pa.allocated_amount>0
                            )
                            OR NOT EXISTS(
                                SELECT 1
                                FROM finance.ledger_entries cle
                                WHERE cle.source_type='credit_note'
                                  AND cle.source_id=cn.id
                                  AND cle.status='posted'
                            )
                    )
                INTO v_backing,v_invalid_backing
                FROM finance.refund_credit_note_links l
                JOIN finance.credit_notes cn
                  ON cn.id=l.credit_note_id
                 AND cn.organization_id=l.organization_id
                JOIN finance.invoices i
                  ON i.id=cn.invoice_id
                WHERE l.refund_id=v_refund.id
                  AND l.organization_id=v_refund.organization_id;

                IF v_backing<>v_refund.amount
                   OR v_invalid_backing<>0
                THEN
                    RAISE EXCEPTION
                        'PAY-10-D issued credit-note backing is incomplete'
                        USING ERRCODE='23514';
                END IF;

                SELECT coalesce(sum(pa.allocated_amount),0)
                INTO v_allocated
                FROM finance.payment_allocations pa
                WHERE pa.payment_id=v_payment.id;

                SELECT coalesce(sum(r.amount),0)
                INTO v_successful
                FROM finance.refunds r
                WHERE r.payment_id=v_payment.id
                  AND r.status='succeeded'
                  AND r.id<>v_refund.id;

                v_successful:=v_successful+v_refund.amount;
                IF v_allocated<=0
                   OR v_successful>v_allocated
                   OR v_successful>v_payment.amount
                THEN
                    RAISE EXCEPTION
                        'PAY-10-D successful refund exceeds refundable payment value'
                        USING ERRCODE='23514';
                END IF;

                v_new_payment_status:=CASE
                    WHEN v_successful=v_payment.amount THEN 'refunded'
                    ELSE 'partially_refunded'
                END;

                SELECT la.id
                INTO v_ar_account
                FROM finance.ledger_accounts la
                WHERE la.legal_entity_id=v_payment.legal_entity_id
                  AND la.code='AR';
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'PAY-10-D AR account unavailable'
                        USING ERRCODE='23514';
                END IF;

                SELECT la.id
                INTO v_clearing_account
                FROM finance.ledger_accounts la
                WHERE la.legal_entity_id=v_payment.legal_entity_id
                  AND la.code='PAYMENT_CLEARING';
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'PAY-10-D payment clearing account unavailable'
                        USING ERRCODE='23514';
                END IF;

                INSERT INTO finance.ledger_entries(
                    legal_entity_id,
                    division_id,
                    brand_id,
                    entry_type,
                    source_type,
                    source_id,
                    status,
                    posted_at
                )
                VALUES(
                    v_payment.legal_entity_id,
                    v_payment.division_id,
                    v_payment.brand_id,
                    'refund',
                    'refund',
                    v_refund.id,
                    'posted',
                    pg_catalog.clock_timestamp()
                )
                RETURNING id INTO v_ledger_id;

                INSERT INTO finance.ledger_entry_lines(
                    ledger_entry_id,
                    ledger_account_id,
                    debit_amount,
                    credit_amount,
                    memo
                )
                VALUES
                (
                    v_ledger_id,
                    v_ar_account,
                    v_refund.amount,
                    0,
                    'Provider cash refund receivable restoration'
                ),
                (
                    v_ledger_id,
                    v_clearing_account,
                    0,
                    v_refund.amount,
                    'Provider cash refund clearing'
                );

                v_payload:=pg_catalog.jsonb_build_object(
                    'refund_id',v_refund.id::text,
                    'payment_id',v_payment.id::text,
                    'command_id',v_command.command_id::text,
                    'provider_refund_ref',v_command.provider_refund_ref,
                    'amount',v_refund.amount,
                    'currency_code',v_refund.currency_code,
                    'status','succeeded'
                );
                v_hash:=pg_catalog.encode(
                    pg_catalog.sha256(
                        pg_catalog.convert_to(v_payload::text,'UTF8')
                    ),
                    'hex'
                );
                INSERT INTO finance.outbox_events(
                    organization_id,legal_entity_id,division_id,brand_id,
                    aggregate_type,aggregate_id,event_type,idempotency_key,
                    payload_json,payload_sha256,status
                )
                VALUES(
                    v_payment.organization_id,v_payment.legal_entity_id,
                    v_payment.division_id,v_payment.brand_id,
                    'refund',v_refund.id,'finance.refund.completed',
                    'pay10:'||v_command.command_id::text||':refund',
                    v_payload,v_hash,'pending'
                )
                ON CONFLICT ON CONSTRAINT uq_finance_outbox_events_idempotency
                DO NOTHING;
                GET DIAGNOSTICS v_rows=ROW_COUNT;
                IF v_rows<>1 THEN
                    RAISE EXCEPTION
                        'PAY-10-D refund outbox identity already exists'
                        USING ERRCODE='23505';
                END IF;

                v_payload:=pg_catalog.jsonb_build_object(
                    'payment_id',v_payment.id::text,
                    'refund_id',v_refund.id::text,
                    'successful_refund_total',v_successful,
                    'currency_code',v_payment.currency_code,
                    'status',v_new_payment_status
                );
                v_hash:=pg_catalog.encode(
                    pg_catalog.sha256(
                        pg_catalog.convert_to(v_payload::text,'UTF8')
                    ),
                    'hex'
                );
                INSERT INTO finance.outbox_events(
                    organization_id,legal_entity_id,division_id,brand_id,
                    aggregate_type,aggregate_id,event_type,idempotency_key,
                    payload_json,payload_sha256,status
                )
                VALUES(
                    v_payment.organization_id,v_payment.legal_entity_id,
                    v_payment.division_id,v_payment.brand_id,
                    'payment',v_payment.id,
                    'finance.payment.refund_state.changed',
                    'pay10:'||v_command.command_id::text||':payment',
                    v_payload,v_hash,'pending'
                )
                ON CONFLICT ON CONSTRAINT uq_finance_outbox_events_idempotency
                DO NOTHING;
                GET DIAGNOSTICS v_rows=ROW_COUNT;
                IF v_rows<>1 THEN
                    RAISE EXCEPTION
                        'PAY-10-D payment outbox identity already exists'
                        USING ERRCODE='23505';
                END IF;

                v_payload:=pg_catalog.jsonb_build_object(
                    'ledger_entry_id',v_ledger_id::text,
                    'source_type','refund',
                    'source_id',v_refund.id::text
                );
                v_hash:=pg_catalog.encode(
                    pg_catalog.sha256(
                        pg_catalog.convert_to(v_payload::text,'UTF8')
                    ),
                    'hex'
                );
                INSERT INTO finance.outbox_events(
                    organization_id,legal_entity_id,division_id,brand_id,
                    aggregate_type,aggregate_id,event_type,idempotency_key,
                    payload_json,payload_sha256,status
                )
                VALUES(
                    v_payment.organization_id,v_payment.legal_entity_id,
                    v_payment.division_id,v_payment.brand_id,
                    'ledger_entry',v_ledger_id,
                    'finance.ledger.entry.posted',
                    'pay10:'||v_command.command_id::text||':ledger',
                    v_payload,v_hash,'pending'
                )
                ON CONFLICT ON CONSTRAINT uq_finance_outbox_events_idempotency
                DO NOTHING;
                GET DIAGNOSTICS v_rows=ROW_COUNT;
                IF v_rows<>1 THEN
                    RAISE EXCEPTION
                        'PAY-10-D ledger outbox identity already exists'
                        USING ERRCODE='23505';
                END IF;

                UPDATE finance.refunds r
                SET
                    status='succeeded',
                    updated_at=pg_catalog.clock_timestamp()
                WHERE r.id=v_refund.id;

                UPDATE finance.payments p
                SET
                    status=v_new_payment_status,
                    updated_at=pg_catalog.clock_timestamp()
                WHERE p.id=v_payment.id;

                UPDATE finance.refund_execution_commands c
                SET
                    status='succeeded',
                    completed_at=pg_catalog.clock_timestamp(),
                    leased_by=NULL,
                    leased_until=NULL,
                    last_error_code=NULL,
                    updated_at=pg_catalog.clock_timestamp()
                WHERE c.command_id=v_command.command_id;

                RETURN QUERY SELECT
                    v_command.command_id,
                    v_refund.id,
                    v_payment.id,
                    v_ledger_id,
                    'succeeded'::text,
                    v_new_payment_status,
                    'succeeded'::text,
                    false;
            END
            $function$
            """
        )

        for signature in _FUNCTIONS:
            op.execute(
                f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC"
            )
            op.execute(
                f"GRANT EXECUTE ON FUNCTION {signature} "
                "TO finance_refund_runtime"
            )
    finally:
        op.execute("RESET ROLE")


def _post_install_proof(bind) -> None:
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        for signature in _FUNCTIONS:
            row = bind.execute(
                sa.text(
                    """
                    SELECT
                        pg_catalog.pg_get_userbyid(p.proowner) AS owner,
                        p.prosecdef,
                        coalesce(array_to_string(p.proconfig,','),'') AS config,
                        EXISTS(
                            SELECT 1
                            FROM pg_catalog.aclexplode(
                                coalesce(
                                    p.proacl,
                                    pg_catalog.acldefault('f',p.proowner)
                                )
                            ) acl
                            WHERE acl.grantee=0
                              AND acl.privilege_type='EXECUTE'
                        ) AS public_execute
                    FROM pg_catalog.pg_proc p
                    WHERE p.oid=pg_catalog.to_regprocedure(:signature)
                    """
                ),
                {"signature": signature},
            ).mappings().one_or_none()
            if (
                row is None
                or row["owner"] != _SECURITY_OWNER
                or not bool(row["prosecdef"])
                or bool(row["public_execute"])
                or "row_security=on" not in row["config"]
            ):
                raise RuntimeError(
                    f"PAY-10-D function security drift: {signature}"
                )
    finally:
        op.execute("RESET ROLE")

    for role in _RUNTIME_ROLES:
        for signature in _FUNCTIONS:
            actual = bool(
                bind.execute(
                    sa.text(
                        "SELECT pg_catalog.has_function_privilege("
                        ":role,:signature,'EXECUTE')"
                    ),
                    {"role": role, "signature": signature},
                ).scalar_one()
            )
            if actual != (role == _REFUND_RUNTIME):
                raise RuntimeError(
                    "PAY-10-D execute ACL drift: "
                    f"{role} -> {signature}"
                )

    for role in (
        _REFUND_RUNTIME,
        "finance_reconciliation_runtime",
        "app_runtime",
        "worker_runtime",
    ):
        for relation in (
            "finance.refund_credit_note_links",
            "finance.credit_notes",
            "finance.ledger_entries",
            "finance.ledger_entry_lines",
            "finance.outbox_events",
        ):
            if bool(
                bind.execute(
                    sa.text(
                        """
                        SELECT
                            pg_catalog.has_table_privilege(
                                :role,:relation,'SELECT'
                            )
                            OR pg_catalog.has_table_privilege(
                                :role,:relation,'INSERT'
                            )
                            OR pg_catalog.has_table_privilege(
                                :role,:relation,'UPDATE'
                            )
                            OR pg_catalog.has_table_privilege(
                                :role,:relation,'DELETE'
                            )
                        """
                    ),
                    {"role": role, "relation": relation},
                ).scalar_one()
            ):
                raise RuntimeError(
                    "PAY-10-D direct Finance DML leaked: "
                    f"{role} -> {relation}"
                )


def _has_finalization_evidence(bind) -> bool:
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        return bool(
            bind.execute(
                sa.text(
                    """
                    SELECT EXISTS(
                        SELECT 1
                        FROM finance.refund_execution_commands
                        WHERE status='succeeded'
                          AND completed_at IS NOT NULL
                    )
                    """
                )
            ).scalar_one()
        )
    finally:
        op.execute("RESET ROLE")


def _drop_functions() -> None:
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        for signature in _FUNCTIONS:
            op.execute(
                f"REVOKE EXECUTE ON FUNCTION {signature} "
                "FROM finance_refund_runtime"
            )
        for signature in reversed(_FUNCTIONS):
            op.execute(f"DROP FUNCTION {signature}")
    finally:
        op.execute("RESET ROLE")


def _restore_acl(bind) -> None:
    rows = bind.execute(
        sa.text(
            """
            SELECT table_name,column_name,privilege_name
            FROM app_private.pay10d_acl_delta
            ORDER BY table_name,column_name,privilege_name
            """
        )
    ).mappings().all()
    for row in rows:
        table_name = row["table_name"]
        column_name = row["column_name"]
        privilege = row["privilege_name"]
        if (
            not table_name.replace("_", "").isalnum()
            or not column_name.replace("_", "").isalnum()
            or privilege not in {"SELECT", "INSERT"}
        ):
            raise RuntimeError("PAY-10-D unsafe ACL delta identity")
        op.execute(
            f"REVOKE {privilege} ({column_name}) "
            f"ON TABLE finance.{table_name} "
            "FROM app_security_owner"
        )
    op.execute("DROP TABLE app_private.pay10d_acl_delta")


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _require_identity(bind)
    _require_predecessor(bind)
    _install_acl_state(bind)

    op.execute(
        """
        CREATE UNIQUE INDEX uq_pay10_refund_ledger_source
        ON finance.ledger_entries(source_id)
        WHERE source_type='refund' AND status='posted'
        """
    )
    _install_functions()
    _post_install_proof(bind)


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _require_identity(bind)

    if _has_finalization_evidence(bind):
        raise RuntimeError(
            "PAY-10-D downgrade blocked: finalized refund effects exist"
        )

    _drop_functions()
    op.execute(
        "DROP INDEX finance.uq_pay10_refund_ledger_source"
    )
    _restore_acl(bind)
