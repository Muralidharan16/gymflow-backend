"""PAY-9 payment application, invoice settlement and entitlement gate.

Revision ID: zs07d8e9f0a53
Revises: zr07d8e9f0a52
Create Date: 2026-09-20

PAY-9 connects verified provider money facts to the already-certified Finance
allocation/ledger/outbox path without making provider capture, settlement, the
browser, Redis or Celery entitlement authority.

A captured/settled provider payment may only be applied through its canonical
member-subscription checkout binding and immutable PAY-4 Finance binding.  The
function applies at most min(payment available, invoice outstanding), so
underpayments remain partial while overpayments settle only the invoice and
retain the excess as unapplied payment balance.

No live provider mode, live money movement, refund-provider execution, merge,
release, deployment or production activation is introduced.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "zs07d8e9f0a53"
down_revision = "zr07d8e9f0a52"
branch_labels = None
depends_on = None


_APP = "app_runtime"
_PAYMENT_RUNTIME = "finance_payment_runtime"
_SECURITY_OWNER = "app_security_owner"
_MIGRATION_OWNER = "migration_owner"
_FUNCTION = "app_secure.apply_verified_provider_payment(uuid,uuid)"


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
        raise RuntimeError(f"PAY-9 missing externally managed role: {role}")
    if bool(row["rolcanlogin"]) is not login:
        raise RuntimeError(f"PAY-9 role login posture drift: {role}")
    for key in (
        "rolsuper","rolinherit","rolcreatedb","rolcreaterole",
        "rolreplication","rolbypassrls",
    ):
        if bool(row[key]):
            raise RuntimeError(f"PAY-9 reduced-role drift: {role}.{key}")


def _require_identity(bind) -> None:
    _require_role(bind, _MIGRATION_OWNER, login=True)
    _require_role(bind, _SECURITY_OWNER)
    _require_role(bind, _APP)
    _require_role(bind, _PAYMENT_RUNTIME)

    identity = bind.execute(
        sa.text("SELECT session_user::text,current_user::text")
    ).one()
    if tuple(identity) != (_MIGRATION_OWNER, _MIGRATION_OWNER):
        raise RuntimeError("PAY-9 migration requires migration_owner")

    if not bind.execute(
        sa.text(
            "SELECT pg_catalog.pg_has_role(:member,:target,'SET')"
        ),
        {"member": _MIGRATION_OWNER, "target": _SECURITY_OWNER},
    ).scalar_one():
        raise RuntimeError(
            "PAY-9 requires migration_owner SET edge to app_security_owner"
        )

    for runtime in (_APP, _PAYMENT_RUNTIME):
        if bind.execute(
            sa.text(
                "SELECT pg_catalog.pg_has_role(:member,:target,'MEMBER') "
                "OR pg_catalog.pg_has_role(:member,:target,'SET')"
            ),
            {"member": runtime, "target": _SECURITY_OWNER},
        ).scalar_one():
            raise RuntimeError(
                f"PAY-9 runtime may reach app_security_owner: {runtime}"
            )


def _require_predecessor(bind) -> None:
    for relation in (
        "finance.payments",
        "finance.payment_events",
        "finance.payment_allocations",
        "finance.invoices",
        "finance.ledger_accounts",
        "finance.ledger_entries",
        "finance.ledger_entry_lines",
        "finance.outbox_events",
        "finance.member_subscription_checkout_bindings",
        "finance.member_subscription_finance_bindings",
        "finance.provider_webhook_inbox",
    ):
        if bind.execute(
            sa.text("SELECT pg_catalog.to_regclass(:relation) IS NULL"),
            {"relation": relation},
        ).scalar_one():
            raise RuntimeError(f"PAY-9 predecessor relation missing: {relation}")

    if not bind.execute(
        sa.text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_proc p
                JOIN pg_catalog.pg_namespace n
                  ON n.oid=p.pronamespace
                WHERE n.nspname='app_secure'
                  AND p.proname='confirm_finance_provider_evidence'
            )
            """
        )
    ).scalar_one():
        raise RuntimeError("PAY-9 PAY-8 provider-evidence authority missing")

    if bind.execute(
        sa.text(
            """
            SELECT pg_catalog.to_regclass(
                'finance.payment_application_records'
            ) IS NOT NULL
            """
        )
    ).scalar_one():
        raise RuntimeError("PAY-9 relation already exists")

    if bind.execute(
        sa.text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_proc p
                JOIN pg_catalog.pg_namespace n
                  ON n.oid=p.pronamespace
                WHERE n.nspname='app_secure'
                  AND p.proname='apply_verified_provider_payment'
            )
            """
        )
    ).scalar_one():
        raise RuntimeError("PAY-9 capability already exists")


def _install_table() -> None:
    op.execute(
        """
        CREATE TABLE finance.payment_application_records(
            id UUID PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
            organization_id UUID NOT NULL
                REFERENCES public.organizations(id) ON DELETE RESTRICT,
            payment_event_id UUID NOT NULL
                REFERENCES finance.payment_events(id) ON DELETE RESTRICT,
            payment_id UUID NOT NULL,
            invoice_id UUID NULL,
            allocation_id UUID NULL
                REFERENCES finance.payment_allocations(id) ON DELETE RESTRICT,
            decision_code VARCHAR(64) NOT NULL,
            allocated_amount NUMERIC(14,2) NOT NULL DEFAULT 0,
            unapplied_amount NUMERIC(14,2) NOT NULL DEFAULT 0,
            invoice_outstanding_amount NUMERIC(14,2) NULL,
            invoice_status VARCHAR(32) NULL,
            created_at TIMESTAMPTZ NOT NULL
                DEFAULT pg_catalog.clock_timestamp(),
            CONSTRAINT fk_pay9_application_payment_org
                FOREIGN KEY(payment_id,organization_id)
                REFERENCES finance.payments(id,organization_id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_pay9_application_invoice_org
                FOREIGN KEY(invoice_id,organization_id)
                REFERENCES finance.invoices(id,organization_id)
                ON DELETE RESTRICT,
            CONSTRAINT uq_pay9_application_event
                UNIQUE(payment_event_id),
            CONSTRAINT chk_pay9_application_decision
                CHECK(decision_code IN (
                    'applied_paid',
                    'applied_partial',
                    'replayed_existing_allocation',
                    'unapplied_no_checkout_binding',
                    'unapplied_subscription_binding_missing',
                    'unapplied_invoice_not_payable',
                    'unapplied_invoice_already_paid',
                    'unapplied_currency_mismatch',
                    'unapplied_relationship_mismatch',
                    'unapplied_payment_exhausted'
                )),
            CONSTRAINT chk_pay9_application_amounts
                CHECK(
                    allocated_amount >= 0
                    AND unapplied_amount >= 0
                    AND (
                        invoice_outstanding_amount IS NULL
                        OR invoice_outstanding_amount >= 0
                    )
                ),
            CONSTRAINT chk_pay9_application_shape
                CHECK(
                    (decision_code IN ('applied_paid','applied_partial')
                     AND invoice_id IS NOT NULL
                     AND allocation_id IS NOT NULL
                     AND allocated_amount > 0)
                    OR
                    (decision_code='replayed_existing_allocation'
                     AND invoice_id IS NOT NULL
                     AND allocation_id IS NOT NULL)
                    OR
                    (decision_code NOT IN (
                        'applied_paid','applied_partial',
                        'replayed_existing_allocation'
                     )
                     AND allocation_id IS NULL
                     AND allocated_amount = 0)
                )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_pay9_application_payment
        ON finance.payment_application_records(
            organization_id,payment_id,created_at,id
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_pay9_application_invoice
        ON finance.payment_application_records(
            organization_id,invoice_id,created_at,id
        )
        WHERE invoice_id IS NOT NULL
        """
    )
    op.execute(
        "ALTER TABLE finance.payment_application_records "
        "ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        "ALTER TABLE finance.payment_application_records "
        "FORCE ROW LEVEL SECURITY"
    )
    op.execute(
        "REVOKE ALL ON TABLE finance.payment_application_records FROM PUBLIC"
    )
    op.execute(
        "GRANT SELECT,INSERT ON TABLE finance.payment_application_records "
        "TO app_security_owner"
    )
    op.execute(
        """
        CREATE POLICY pay9_application_security_owner
        ON finance.payment_application_records
        FOR ALL TO app_security_owner
        USING (
            organization_id = NULLIF(
                pg_catalog.current_setting('app.current_org_id',true),''
            )::uuid
        )
        WITH CHECK (
            organization_id = NULLIF(
                pg_catalog.current_setting('app.current_org_id',true),''
            )::uuid
        )
        """
    )


def _install_function(bind) -> None:
    had_create = bool(
        bind.execute(
            sa.text(
                """
                SELECT pg_catalog.has_schema_privilege(
                    'app_security_owner','app_secure','CREATE'
                )
                """
            )
        ).scalar_one()
    )
    if not had_create:
        op.execute(
            "GRANT CREATE ON SCHEMA app_secure TO app_security_owner"
        )

    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        op.execute(
            r"""
            CREATE FUNCTION app_secure.apply_verified_provider_payment(
                p_payment_id uuid,
                p_payment_event_id uuid
            )
            RETURNS TABLE(
                application_record_id uuid,
                payment_id uuid,
                invoice_id uuid,
                allocation_id uuid,
                allocated_amount numeric,
                unapplied_amount numeric,
                invoice_outstanding_amount numeric,
                invoice_status text,
                decision_code text,
                replayed boolean
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_existing_org uuid;
                v_payment finance.payments%ROWTYPE;
                v_event record;
                v_checkout finance.member_subscription_checkout_bindings%ROWTYPE;
                v_binding finance.member_subscription_finance_bindings%ROWTYPE;
                v_invoice finance.invoices%ROWTYPE;
                v_existing finance.payment_application_records%ROWTYPE;
                v_record finance.payment_application_records%ROWTYPE;
                v_existing_allocation finance.payment_allocations%ROWTYPE;
                v_allocation finance.payment_allocations%ROWTYPE;

                v_payment_allocated numeric(14,2):=0;
                v_invoice_allocated numeric(14,2):=0;
                v_payment_available numeric(14,2):=0;
                v_invoice_outstanding numeric(14,2):=NULL;
                v_apply_amount numeric(14,2):=0;
                v_after_unapplied numeric(14,2):=0;
                v_after_outstanding numeric(14,2):=NULL;

                v_clearing_account uuid;
                v_ar_account uuid;
                v_ledger_entry uuid;

                v_decision text:='unapplied_no_checkout_binding';
                v_invoice_id uuid:=NULL;
                v_allocation_id uuid:=NULL;
                v_invoice_status text:=NULL;

                v_payment_payload jsonb;
                v_invoice_payload jsonb;
                v_ledger_payload jsonb;
                v_payment_hash text;
                v_invoice_hash text;
                v_ledger_hash text;
            BEGIN
                IF NOT (
                    pg_catalog.pg_has_role(
                        session_user,'app_runtime','MEMBER'
                    )
                    OR pg_catalog.pg_has_role(
                        session_user,'finance_payment_runtime','MEMBER'
                    )
                ) THEN
                    RAISE EXCEPTION
                        'PAY-9 application requires app/finance-payment runtime'
                        USING ERRCODE='42501';
                END IF;

                IF p_payment_id IS NULL OR p_payment_event_id IS NULL THEN
                    RAISE EXCEPTION
                        'PAY-9 application identity invalid'
                        USING ERRCODE='22023';
                END IF;

                SELECT e.id,e.payment_id,e.event_type
                INTO v_event
                FROM finance.payment_events e
                WHERE e.id=p_payment_event_id
                  AND e.payment_id=p_payment_id;
                IF NOT FOUND
                   OR v_event.event_type NOT IN (
                       'payment.captured','order.paid'
                   )
                THEN
                    RAISE EXCEPTION
                        'PAY-9 application requires verified captured evidence'
                        USING ERRCODE='23514';
                END IF;

                SELECT p.* INTO v_payment
                FROM finance.payments p
                WHERE p.id=p_payment_id
                FOR UPDATE;
                IF NOT FOUND
                   OR v_payment.organization_id IS NULL
                   OR v_payment.status NOT IN ('captured','settled')
                THEN
                    RAISE EXCEPTION
                        'PAY-9 application payment not eligible'
                        USING ERRCODE='23514';
                END IF;

                BEGIN
                    v_existing_org:=NULLIF(
                        pg_catalog.current_setting(
                            'app.current_org_id',true
                        ),''
                    )::uuid;
                EXCEPTION WHEN invalid_text_representation THEN
                    RAISE EXCEPTION
                        'PAY-9 tenant context invalid'
                        USING ERRCODE='42501';
                END;
                IF v_existing_org IS NOT NULL
                   AND v_existing_org IS DISTINCT FROM
                       v_payment.organization_id
                THEN
                    RAISE EXCEPTION
                        'PAY-9 cross-tenant application denied'
                        USING ERRCODE='42501';
                END IF;
                PERFORM pg_catalog.set_config(
                    'app.current_org_id',
                    v_payment.organization_id::text,
                    true
                );

                SELECT r.* INTO v_existing
                FROM finance.payment_application_records r
                WHERE r.payment_event_id=p_payment_event_id;
                IF FOUND THEN
                    IF v_existing.payment_id
                       IS DISTINCT FROM p_payment_id
                    THEN
                        RAISE EXCEPTION
                            'PAY-9 application replay conflict'
                            USING ERRCODE='23505';
                    END IF;
                    RETURN QUERY SELECT
                        v_existing.id,
                        v_existing.payment_id,
                        v_existing.invoice_id,
                        v_existing.allocation_id,
                        v_existing.allocated_amount,
                        v_existing.unapplied_amount,
                        v_existing.invoice_outstanding_amount,
                        v_existing.invoice_status::text,
                        v_existing.decision_code::text,
                        true;
                    RETURN;
                END IF;

                SELECT COALESCE(
                    pg_catalog.sum(a.allocated_amount),0
                )
                INTO v_payment_allocated
                FROM finance.payment_allocations a
                WHERE a.payment_id=v_payment.id;

                v_payment_available:=
                    GREATEST(
                        v_payment.amount-v_payment_allocated,
                        0::numeric
                    );
                v_after_unapplied:=v_payment_available;

                SELECT c.* INTO v_checkout
                FROM finance.member_subscription_checkout_bindings c
                WHERE c.checkout_intent_id=v_payment.id
                  AND c.organization_id=v_payment.organization_id;
                IF FOUND THEN
                    v_invoice_id:=v_checkout.invoice_id;

                    SELECT i.* INTO v_invoice
                    FROM finance.invoices i
                    WHERE i.id=v_invoice_id
                      AND i.organization_id=v_payment.organization_id
                    FOR UPDATE;

                    IF NOT FOUND THEN
                        RAISE EXCEPTION
                            'PAY-9 canonical checkout invoice unavailable'
                            USING ERRCODE='23514';
                    END IF;

                    SELECT COALESCE(
                        pg_catalog.sum(a.allocated_amount),0
                    )
                    INTO v_invoice_allocated
                    FROM finance.payment_allocations a
                    WHERE a.invoice_id=v_invoice.id;

                    v_invoice_outstanding:=
                        GREATEST(
                            v_invoice.grand_total_amount-v_invoice_allocated,
                            0::numeric
                        );
                    v_after_outstanding:=v_invoice_outstanding;
                    v_invoice_status:=v_invoice.status::text;

                    SELECT b.* INTO v_binding
                    FROM finance.member_subscription_finance_bindings b
                    WHERE b.finance_invoice_id=v_invoice.id
                      AND b.organization_id=v_payment.organization_id;

                    IF NOT FOUND THEN
                        v_decision:=
                            'unapplied_subscription_binding_missing';
                    ELSIF v_payment.currency_code
                            IS DISTINCT FROM v_invoice.currency_code
                       OR v_payment.currency_code
                            IS DISTINCT FROM v_binding.currency_code
                    THEN
                        v_decision:='unapplied_currency_mismatch';
                    ELSIF v_payment.legal_entity_id
                            IS DISTINCT FROM v_invoice.legal_entity_id
                       OR v_payment.gst_registration_id
                            IS DISTINCT FROM v_invoice.gst_registration_id
                       OR v_payment.division_id
                            IS DISTINCT FROM v_invoice.division_id
                       OR v_payment.brand_id
                            IS DISTINCT FROM v_invoice.brand_id
                       OR v_checkout.invoice_id
                            IS DISTINCT FROM v_binding.finance_invoice_id
                       OR v_binding.amount
                            IS DISTINCT FROM v_invoice.grand_total_amount
                    THEN
                        v_decision:='unapplied_relationship_mismatch';
                    ELSE
                        SELECT a.* INTO v_existing_allocation
                        FROM finance.payment_allocations a
                        WHERE a.payment_id=v_payment.id
                          AND a.invoice_id=v_invoice.id;

                        IF FOUND THEN
                            v_allocation_id:=v_existing_allocation.id;
                            v_decision:='replayed_existing_allocation';
                        ELSIF v_payment_available<=0 THEN
                            v_decision:='unapplied_payment_exhausted';
                        ELSIF v_invoice.status='paid'
                           OR v_invoice_outstanding<=0
                        THEN
                            v_decision:='unapplied_invoice_already_paid';
                        ELSIF v_invoice.status
                              NOT IN ('issued','partially_paid')
                        THEN
                            v_decision:='unapplied_invoice_not_payable';
                        ELSE
                            v_apply_amount:=
                                LEAST(
                                    v_payment_available,
                                    v_invoice_outstanding
                                );

                            INSERT INTO finance.payment_allocations(
                                payment_id,invoice_id,allocated_amount
                            )
                            VALUES(
                                v_payment.id,
                                v_invoice.id,
                                v_apply_amount
                            )
                            RETURNING * INTO v_allocation;
                            v_allocation_id:=v_allocation.id;

                            v_after_unapplied:=
                                v_payment_available-v_apply_amount;
                            v_after_outstanding:=
                                v_invoice_outstanding-v_apply_amount;

                            UPDATE finance.invoices
                            SET status=CASE
                                WHEN v_after_outstanding=0
                                THEN 'paid'
                                ELSE 'partially_paid'
                            END
                            WHERE id=v_invoice.id
                            RETURNING status::text
                            INTO v_invoice_status;

                            SELECT la.id INTO v_clearing_account
                            FROM finance.ledger_accounts la
                            WHERE la.legal_entity_id=v_payment.legal_entity_id
                              AND la.code='PAYMENT_CLEARING';
                            SELECT la.id INTO v_ar_account
                            FROM finance.ledger_accounts la
                            WHERE la.legal_entity_id=v_payment.legal_entity_id
                              AND la.code='AR';
                            IF v_clearing_account IS NULL
                               OR v_ar_account IS NULL
                            THEN
                                RAISE EXCEPTION
                                    'PAY-9 ledger accounts unavailable'
                                    USING ERRCODE='23514';
                            END IF;

                            INSERT INTO finance.ledger_entries(
                                legal_entity_id,division_id,brand_id,
                                entry_type,source_type,source_id,
                                status,posted_at
                            )
                            VALUES(
                                v_payment.legal_entity_id,
                                COALESCE(
                                    v_payment.division_id,
                                    v_invoice.division_id
                                ),
                                COALESCE(
                                    v_payment.brand_id,
                                    v_invoice.brand_id
                                ),
                                'payment','payment_allocation',
                                v_allocation.id,'posted',
                                pg_catalog.clock_timestamp()
                            )
                            RETURNING id INTO v_ledger_entry;

                            INSERT INTO finance.ledger_entry_lines(
                                ledger_entry_id,ledger_account_id,
                                debit_amount,credit_amount,memo
                            )
                            VALUES
                            (
                                v_ledger_entry,v_clearing_account,
                                v_apply_amount,0,'Payment clearing'
                            ),
                            (
                                v_ledger_entry,v_ar_account,
                                0,v_apply_amount,'Receivable settled'
                            );

                            v_payment_payload:=
                                pg_catalog.jsonb_build_object(
                                    'payment_id',v_payment.id::text,
                                    'invoice_id',v_invoice.id::text,
                                    'allocation_id',v_allocation.id::text,
                                    'allocated_amount',
                                        v_apply_amount::text,
                                    'unapplied_amount',
                                        v_after_unapplied::text
                                );
                            v_payment_hash:=
                                pg_catalog.encode(
                                    pg_catalog.sha256(
                                        pg_catalog.convert_to(
                                            v_payment_payload::text,
                                            'UTF8'
                                        )
                                    ),
                                    'hex'
                                );

                            INSERT INTO finance.outbox_events(
                                organization_id,legal_entity_id,
                                division_id,brand_id,
                                aggregate_type,aggregate_id,event_type,
                                idempotency_key,payload_json,
                                payload_sha256,status
                            )
                            VALUES(
                                v_payment.organization_id,
                                v_payment.legal_entity_id,
                                v_payment.division_id,
                                v_payment.brand_id,
                                'payment_allocation',v_allocation.id,
                                'finance.payment.applied',
                                'pay9-'||p_payment_event_id::text||
                                    '-payment',
                                v_payment_payload,v_payment_hash,'pending'
                            );

                            v_invoice_payload:=
                                pg_catalog.jsonb_build_object(
                                    'invoice_id',v_invoice.id::text,
                                    'status',v_invoice_status
                                );
                            v_invoice_hash:=
                                pg_catalog.encode(
                                    pg_catalog.sha256(
                                        pg_catalog.convert_to(
                                            v_invoice_payload::text,
                                            'UTF8'
                                        )
                                    ),
                                    'hex'
                                );
                            INSERT INTO finance.outbox_events(
                                organization_id,legal_entity_id,
                                division_id,brand_id,
                                aggregate_type,aggregate_id,event_type,
                                idempotency_key,payload_json,
                                payload_sha256,status
                            )
                            VALUES(
                                v_invoice.organization_id,
                                v_invoice.legal_entity_id,
                                v_invoice.division_id,
                                v_invoice.brand_id,
                                'invoice',v_invoice.id,
                                CASE
                                    WHEN v_invoice_status='paid'
                                    THEN 'finance.invoice.paid'
                                    ELSE 'finance.invoice.partially_paid'
                                END,
                                'pay9-'||p_payment_event_id::text||
                                    '-invoice',
                                v_invoice_payload,v_invoice_hash,'pending'
                            );

                            v_ledger_payload:=
                                pg_catalog.jsonb_build_object(
                                    'ledger_entry_id',
                                        v_ledger_entry::text,
                                    'source_type',
                                        'payment_allocation'
                                );
                            v_ledger_hash:=
                                pg_catalog.encode(
                                    pg_catalog.sha256(
                                        pg_catalog.convert_to(
                                            v_ledger_payload::text,
                                            'UTF8'
                                        )
                                    ),
                                    'hex'
                                );
                            INSERT INTO finance.outbox_events(
                                organization_id,legal_entity_id,
                                division_id,brand_id,
                                aggregate_type,aggregate_id,event_type,
                                idempotency_key,payload_json,
                                payload_sha256,status
                            )
                            VALUES(
                                v_payment.organization_id,
                                v_payment.legal_entity_id,
                                v_payment.division_id,
                                v_payment.brand_id,
                                'ledger_entry',v_ledger_entry,
                                'finance.ledger.entry.posted',
                                'pay9-'||p_payment_event_id::text||
                                    '-ledger',
                                v_ledger_payload,v_ledger_hash,'pending'
                            );

                            v_decision:=CASE
                                WHEN v_after_outstanding=0
                                THEN 'applied_paid'
                                ELSE 'applied_partial'
                            END;
                        END IF;
                    END IF;
                END IF;

                INSERT INTO finance.payment_application_records(
                    organization_id,payment_event_id,payment_id,
                    invoice_id,allocation_id,decision_code,
                    allocated_amount,unapplied_amount,
                    invoice_outstanding_amount,invoice_status
                )
                VALUES(
                    v_payment.organization_id,
                    v_event.id,
                    v_payment.id,
                    v_invoice_id,
                    v_allocation_id,
                    v_decision,
                    CASE
                        WHEN v_decision IN (
                            'applied_paid','applied_partial'
                        )
                        THEN v_apply_amount
                        ELSE 0
                    END,
                    v_after_unapplied,
                    v_after_outstanding,
                    v_invoice_status
                )
                RETURNING * INTO v_record;

                RETURN QUERY SELECT
                    v_record.id,
                    v_record.payment_id,
                    v_record.invoice_id,
                    v_record.allocation_id,
                    v_record.allocated_amount,
                    v_record.unapplied_amount,
                    v_record.invoice_outstanding_amount,
                    v_record.invoice_status::text,
                    v_record.decision_code::text,
                    false;
            END
            $function$
            """
        )

        op.execute(f"REVOKE ALL ON FUNCTION {_FUNCTION} FROM PUBLIC")
        op.execute(
            f"GRANT EXECUTE ON FUNCTION {_FUNCTION} TO app_runtime"
        )
        op.execute(
            "GRANT USAGE ON SCHEMA app_secure "
            "TO finance_payment_runtime"
        )
        op.execute(
            f"GRANT EXECUTE ON FUNCTION {_FUNCTION} "
            "TO finance_payment_runtime"
        )
    finally:
        op.execute("RESET ROLE")

    if not had_create:
        op.execute(
            "REVOKE CREATE ON SCHEMA app_secure FROM app_security_owner"
        )


def _assert_runtime_denials(bind) -> None:
    for role in (
        _APP,
        _PAYMENT_RUNTIME,
        "worker_runtime",
        "finance_reconciliation_runtime",
    ):
        leaked = bind.execute(
            sa.text(
                """
                SELECT
                    pg_catalog.has_table_privilege(
                        :role,
                        'finance.payment_application_records',
                        'SELECT'
                    )
                    OR pg_catalog.has_table_privilege(
                        :role,
                        'finance.payment_application_records',
                        'INSERT'
                    )
                    OR pg_catalog.has_table_privilege(
                        :role,
                        'finance.payment_application_records',
                        'UPDATE'
                    )
                    OR pg_catalog.has_table_privilege(
                        :role,
                        'finance.payment_application_records',
                        'DELETE'
                    )
                """
            ),
            {"role": role},
        ).scalar_one()
        if leaked:
            raise RuntimeError(
                f"PAY-9 leaked direct payment-application DML to {role}"
            )


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _require_identity(bind)
    _require_predecessor(bind)
    _install_table()
    _install_function(bind)
    _assert_runtime_denials(bind)


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _require_identity(bind)

    if bind.execute(
        sa.text(
            """
            SELECT pg_catalog.to_regclass(
                'finance.payment_application_records'
            ) IS NULL
            """
        )
    ).scalar_one():
        raise RuntimeError("PAY-9 downgrade relation missing")

    # migration_owner owns the table, but FORCE RLS intentionally denies it
    # normal visibility. Disable FORCE only transactionally to decide whether
    # durable financial application evidence exists. A refusal rolls this DDL
    # back together with the exception.
    op.execute(
        "ALTER TABLE finance.payment_application_records "
        "NO FORCE ROW LEVEL SECURITY"
    )
    count = bind.execute(
        sa.text(
            "SELECT count(*) FROM finance.payment_application_records"
        )
    ).scalar_one()
    if count:
        raise RuntimeError(
            "PAY-9 populated downgrade refused: "
            "payment application evidence exists"
        )

    # Function ACLs and ownership belong to app_security_owner. The reduced
    # migration identity intentionally has no app_secure USAGE, so all
    # function ACL teardown must run under the exact owner context.
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        op.execute(
            f"REVOKE EXECUTE ON FUNCTION {_FUNCTION} FROM app_runtime"
        )
        op.execute(
            f"REVOKE EXECUTE ON FUNCTION {_FUNCTION} "
            "FROM finance_payment_runtime"
        )
        op.execute(f"DROP FUNCTION {_FUNCTION}")
    finally:
        op.execute("RESET ROLE")

    op.execute(
        "DROP POLICY pay9_application_security_owner "
        "ON finance.payment_application_records"
    )
    op.execute(
        "DROP TABLE finance.payment_application_records"
    )
