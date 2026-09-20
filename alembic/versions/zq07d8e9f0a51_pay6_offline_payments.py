"""PAY-6 first-class offline/manual payments.

Revision ID: zq07d8e9f0a51
Revises: zp07d8e9f0a50
Create Date: 2026-09-20

Introduces maker/checker offline payment preparation, approval and rejection
using the canonical PAY-3 monetary-command store and existing Finance Core
confirmed-payment application capability.

No free-text mark-paid shortcut exists. Approval requires a different
authenticated owner/admin actor, re-validates invoice balance/currency, creates
the authoritative Finance payment, and applies it through the same allocation,
ledger and outbox authority used by confirmed online payments.

No live provider call, live money movement, refund-provider execution, release
or deployment is introduced.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "zq07d8e9f0a51"
down_revision = "zp07d8e9f0a50"
branch_labels = None
depends_on = None


_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_APP = "app_runtime"
_ACL_STATE = "app_private.pay6_offline_payment_acl_delta"

_PREPARE = (
    "app_secure.prepare_offline_payment("
    "uuid,text,numeric,text,text,text,text)"
)
_APPROVE = "app_secure.approve_offline_payment(uuid,text)"
_REJECT = "app_secure.reject_offline_payment(uuid,text,text)"
_RESERVE = "app_secure.pay6_reserve_offline_command(text,text,text,text)"
_COMPLETE = "app_secure.pay6_complete_offline_command(uuid,text)"
_EVENT_GUARD = "app_secure.pay6_reject_offline_event_mutation()"

_TENANT = "NULLIF(pg_catalog.current_setting('app.current_org_id',true),'')::uuid"


_REQUIRED_COLUMN_PRIVILEGES = (
    (
        "monetary_commands",
        "SELECT",
        (
            "id","organization_id","scope","idempotency_key",
            "request_hash_sha256","business_reference","actor_type",
            "actor_ref_sha256","status","response_ref","created_at",
            "updated_at","completed_at",
        ),
    ),
    (
        "monetary_commands",
        "INSERT",
        (
            "organization_id","scope","idempotency_key","request_hash_sha256",
            "business_reference","correlation_id","actor_type",
            "actor_ref_sha256","status",
        ),
    ),
    (
        "monetary_commands",
        "UPDATE",
        ("status","response_ref","updated_at","completed_at"),
    ),
    (
        "invoices",
        "SELECT",
        (
            "id","organization_id","legal_entity_id","gst_registration_id",
            "division_id","brand_id","status","currency_code",
            "grand_total_amount",
        ),
    ),
    (
        "payment_allocations",
        "SELECT",
        ("invoice_id","allocated_amount"),
    ),
    (
        "payments",
        "INSERT",
        (
            "organization_id","legal_entity_id","gst_registration_id",
            "division_id","brand_id","provider_code","provider_payment_ref",
            "amount","currency_code","status","raw_status",
        ),
    ),
    (
        "payments",
        "SELECT",
        (
            "id","organization_id","legal_entity_id","gst_registration_id",
            "division_id","brand_id","provider_code","provider_payment_ref",
            "amount","currency_code","status","raw_status",
        ),
    ),
    (
        "payment_events",
        "INSERT",
        (
            "payment_id","provider_code","provider_event_id","event_type",
            "event_payload_sha256",
        ),
    ),
)


def _require_role(bind, role: str, *, login: bool = False) -> None:
    row=bind.execute(
        sa.text(
            """
            SELECT rolcanlogin,rolsuper,rolinherit,rolcreatedb,rolcreaterole,
                   rolreplication,rolbypassrls
            FROM pg_catalog.pg_roles WHERE rolname=:role
            """
        ),
        {"role":role},
    ).mappings().one_or_none()
    if row is None:
        raise RuntimeError(f"PAY-6 missing externally managed role: {role}")
    if bool(row["rolcanlogin"]) is not login:
        raise RuntimeError(f"PAY-6 role login posture drift: {role}")
    for key in (
        "rolsuper","rolinherit","rolcreatedb","rolcreaterole",
        "rolreplication","rolbypassrls",
    ):
        if bool(row[key]):
            raise RuntimeError(f"PAY-6 reduced-role drift: {role}.{key}")


def _require_identity(bind) -> None:
    _require_role(bind,_MIGRATION_OWNER,login=True)
    _require_role(bind,_SECURITY_OWNER)
    _require_role(bind,_APP)
    identity=bind.execute(
        sa.text("SELECT session_user::text,current_user::text")
    ).one()
    if tuple(identity)!=(_MIGRATION_OWNER,_MIGRATION_OWNER):
        raise RuntimeError("PAY-6 migration requires migration_owner")
    if not bind.execute(
        sa.text("SELECT pg_catalog.pg_has_role(:member,:target,'SET')"),
        {"member":_MIGRATION_OWNER,"target":_SECURITY_OWNER},
    ).scalar_one():
        raise RuntimeError("PAY-6 requires migration_owner SET edge to app_security_owner")
    if bind.execute(
        sa.text(
            "SELECT pg_catalog.pg_has_role(:member,:target,'MEMBER') "
            "OR pg_catalog.pg_has_role(:member,:target,'SET')"
        ),
        {"member":_APP,"target":_SECURITY_OWNER},
    ).scalar_one():
        raise RuntimeError("PAY-6 app_runtime can reach app_security_owner")


def _grant_column_if_missing(
    bind,
    *,
    table_name: str,
    privilege: str,
    column_name: str,
) -> None:
    relation=f"finance.{table_name}"
    present=bind.execute(
        sa.text(
            """
            SELECT pg_catalog.has_column_privilege(
                'app_security_owner',:relation,:column_name,:privilege
            )
            """
        ),
        {
            "relation":relation,
            "column_name":column_name,
            "privilege":privilege,
        },
    ).scalar_one()
    if present:
        return
    bind.execute(
        sa.text(
            f"GRANT {privilege} ({column_name}) "
            f"ON TABLE {relation} TO app_security_owner"
        )
    )
    bind.execute(
        sa.text(
            """
            INSERT INTO app_private.pay6_offline_payment_acl_delta(
                table_name,column_name,privilege_name
            ) VALUES (:table_name,:column_name,:privilege)
            """
        ),
        {
            "table_name":table_name,
            "column_name":column_name,
            "privilege":privilege,
        },
    )


def _install_acl_delta(bind) -> None:
    if bind.execute(
        sa.text("SELECT pg_catalog.to_regclass(:name) IS NOT NULL"),
        {"name":_ACL_STATE},
    ).scalar_one():
        raise RuntimeError("PAY-6 ACL delta state already exists")
    bind.execute(
        sa.text(
            """
            CREATE TABLE app_private.pay6_offline_payment_acl_delta(
                table_name text NOT NULL,
                column_name text NOT NULL,
                privilege_name text NOT NULL,
                PRIMARY KEY(table_name,column_name,privilege_name),
                CHECK (
                    table_name IN (
                        'monetary_commands','invoices','payment_allocations',
                        'payments','payment_events'
                    )
                ),
                CHECK (privilege_name IN ('SELECT','INSERT','UPDATE'))
            )
            """
        )
    )
    bind.execute(
        sa.text(
            "REVOKE ALL ON TABLE app_private.pay6_offline_payment_acl_delta FROM PUBLIC"
        )
    )
    for table_name,privilege,columns in _REQUIRED_COLUMN_PRIVILEGES:
        for column_name in columns:
            _grant_column_if_missing(
                bind,
                table_name=table_name,
                privilege=privilege,
                column_name=column_name,
            )


def _install_schema() -> None:
    op.execute(
        """
        CREATE TABLE finance.offline_payment_requests(
            id UUID PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
            organization_id UUID NOT NULL
                REFERENCES public.organizations(id) ON DELETE RESTRICT,
            invoice_id UUID NOT NULL,
            payment_method VARCHAR(24) NOT NULL,
            reference_code VARCHAR(120) NOT NULL,
            proof_sha256 CHAR(64) NOT NULL,
            amount NUMERIC(14,2) NOT NULL,
            currency_code CHAR(3) NOT NULL,
            status TEXT NOT NULL DEFAULT 'prepared',
            prepared_actor_id UUID NOT NULL,
            prepared_actor_type VARCHAR(40) NOT NULL,
            prepare_command_id UUID NOT NULL
                REFERENCES finance.monetary_commands(id) ON DELETE RESTRICT,
            approved_actor_id UUID NULL,
            approved_actor_type VARCHAR(40) NULL,
            approval_command_id UUID NULL
                REFERENCES finance.monetary_commands(id) ON DELETE RESTRICT,
            rejected_actor_id UUID NULL,
            rejected_actor_type VARCHAR(40) NULL,
            rejection_command_id UUID NULL
                REFERENCES finance.monetary_commands(id) ON DELETE RESTRICT,
            rejection_reason_code VARCHAR(80) NULL,
            payment_id UUID NULL,
            prepared_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            decided_at TIMESTAMPTZ NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            CONSTRAINT fk_pay6_offline_invoice_org
                FOREIGN KEY(invoice_id,organization_id)
                REFERENCES finance.invoices(id,organization_id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_pay6_offline_payment_org
                FOREIGN KEY(payment_id,organization_id)
                REFERENCES finance.payments(id,organization_id)
                ON DELETE RESTRICT,
            CONSTRAINT uq_pay6_offline_prepare_command UNIQUE(prepare_command_id),
            CONSTRAINT uq_pay6_offline_approval_command UNIQUE(approval_command_id),
            CONSTRAINT uq_pay6_offline_rejection_command UNIQUE(rejection_command_id),
            CONSTRAINT uq_pay6_offline_payment UNIQUE(payment_id),
            CONSTRAINT uq_pay6_offline_reference
                UNIQUE(organization_id,payment_method,reference_code),
            CONSTRAINT chk_pay6_offline_method
                CHECK(payment_method IN ('cash','bank_transfer','cheque')),
            CONSTRAINT chk_pay6_offline_reference
                CHECK(
                    char_length(reference_code) BETWEEN 1 AND 120
                    AND reference_code ~ '^[A-Za-z0-9][A-Za-z0-9:._/-]*$'
                ),
            CONSTRAINT chk_pay6_offline_proof
                CHECK(proof_sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT chk_pay6_offline_amount CHECK(amount > 0),
            CONSTRAINT chk_pay6_offline_currency
                CHECK(
                    char_length(currency_code)=3
                    AND upper(currency_code)=currency_code
                    AND currency_code !~ '[^A-Z]'
                ),
            CONSTRAINT chk_pay6_offline_status
                CHECK(status IN ('prepared','approved','rejected')),
            CONSTRAINT chk_pay6_offline_prepared_actor
                CHECK(
                    prepared_actor_type ~ '^[a-z][a-z0-9_]*$'
                ),
            CONSTRAINT chk_pay6_offline_decision_shape
                CHECK(
                    (
                        status='prepared'
                        AND approved_actor_id IS NULL
                        AND approval_command_id IS NULL
                        AND rejected_actor_id IS NULL
                        AND rejection_command_id IS NULL
                        AND rejection_reason_code IS NULL
                        AND payment_id IS NULL
                        AND decided_at IS NULL
                    )
                    OR (
                        status='approved'
                        AND approved_actor_id IS NOT NULL
                        AND approved_actor_type IS NOT NULL
                        AND approval_command_id IS NOT NULL
                        AND rejected_actor_id IS NULL
                        AND rejection_command_id IS NULL
                        AND rejection_reason_code IS NULL
                        AND payment_id IS NOT NULL
                        AND decided_at IS NOT NULL
                    )
                    OR (
                        status='rejected'
                        AND approved_actor_id IS NULL
                        AND approval_command_id IS NULL
                        AND rejected_actor_id IS NOT NULL
                        AND rejected_actor_type IS NOT NULL
                        AND rejection_command_id IS NOT NULL
                        AND rejection_reason_code IS NOT NULL
                        AND payment_id IS NULL
                        AND decided_at IS NOT NULL
                    )
                )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_pay6_offline_requests_org_status
        ON finance.offline_payment_requests(
            organization_id,status,prepared_at,id
        )
        """
    )
    op.execute(
        """
        CREATE TABLE finance.offline_payment_events(
            id UUID PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
            organization_id UUID NOT NULL
                REFERENCES public.organizations(id) ON DELETE RESTRICT,
            offline_payment_request_id UUID NOT NULL
                REFERENCES finance.offline_payment_requests(id)
                ON DELETE RESTRICT,
            monetary_command_id UUID NOT NULL
                REFERENCES finance.monetary_commands(id) ON DELETE RESTRICT,
            event_type VARCHAR(80) NOT NULL,
            actor_id UUID NOT NULL,
            actor_type VARCHAR(40) NOT NULL,
            request_hash_sha256 CHAR(64) NOT NULL,
            proof_sha256 CHAR(64) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            CONSTRAINT uq_pay6_offline_event_command UNIQUE(monetary_command_id),
            CONSTRAINT chk_pay6_offline_event_type
                CHECK(event_type IN (
                    'offline_payment.prepared',
                    'offline_payment.approved',
                    'offline_payment.rejected'
                )),
            CONSTRAINT chk_pay6_offline_event_actor
                CHECK(actor_type ~ '^[a-z][a-z0-9_]*$'),
            CONSTRAINT chk_pay6_offline_event_request_hash
                CHECK(request_hash_sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT chk_pay6_offline_event_proof
                CHECK(proof_sha256 ~ '^[0-9a-f]{64}$')
        )
        """
    )
    for table in ("offline_payment_requests","offline_payment_events"):
        op.execute(f"ALTER TABLE finance.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE finance.{table} FORCE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON TABLE finance.{table} FROM PUBLIC")
        op.execute(f"GRANT SELECT,INSERT ON TABLE finance.{table} TO app_security_owner")
        op.execute(
            f"""
            CREATE POLICY pay6_{table}_security_owner_select
            ON finance.{table}
            FOR SELECT TO app_security_owner
            USING (organization_id={_TENANT})
            """
        )
        op.execute(
            f"""
            CREATE POLICY pay6_{table}_security_owner_insert
            ON finance.{table}
            FOR INSERT TO app_security_owner
            WITH CHECK (organization_id={_TENANT})
            """
        )
    op.execute(
        """
        GRANT UPDATE(
            status,approved_actor_id,approved_actor_type,approval_command_id,
            rejected_actor_id,rejected_actor_type,rejection_command_id,
            rejection_reason_code,payment_id,decided_at,updated_at
        )
        ON TABLE finance.offline_payment_requests
        TO app_security_owner
        """
    )
    op.execute(
        f"""
        CREATE POLICY pay6_offline_payment_requests_security_owner_update
        ON finance.offline_payment_requests
        FOR UPDATE TO app_security_owner
        USING (organization_id={_TENANT})
        WITH CHECK (organization_id={_TENANT})
        """
    )


def _install_functions(bind) -> None:
    had_create = bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.has_schema_privilege("
                ":role_name, 'app_secure', 'CREATE')"
            ),
            {"role_name": _SECURITY_OWNER},
        ).scalar_one()
    )
    if not had_create:
        bind.execute(
            sa.text(
                "GRANT CREATE ON SCHEMA app_secure TO app_security_owner"
            )
        )

    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        op.execute(
            r"""
            CREATE FUNCTION app_secure.pay6_reject_offline_event_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            AS $function$
            BEGIN
                IF session_user='migration_owner' THEN
                    RETURN CASE WHEN TG_OP='DELETE' THEN OLD ELSE NEW END;
                END IF;
                RAISE EXCEPTION 'PAY-6 offline payment audit events are immutable'
                    USING ERRCODE='42501';
            END
            $function$
            """
        )
        op.execute(
            r"""
            CREATE FUNCTION app_secure.pay6_reserve_offline_command(
                p_scope text,
                p_idempotency_key text,
                p_request_hash text,
                p_business_reference text
            )
            RETURNS TABLE(
                command_id uuid,
                response_ref text,
                inserted boolean
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_org uuid;
                v_actor uuid;
                v_actor_type text;
                v_role text;
                v_actor_hash text;
                v_existing finance.monetary_commands%ROWTYPE;
            BEGIN
                IF NOT pg_catalog.pg_has_role(session_user,'app_runtime','MEMBER') THEN
                    RAISE EXCEPTION 'PAY-6 offline payment command requires app_runtime'
                        USING ERRCODE='42501';
                END IF;
                BEGIN
                    v_org:=NULLIF(
                        pg_catalog.current_setting('app.current_org_id',true),''
                    )::uuid;
                    v_actor:=NULLIF(
                        pg_catalog.current_setting('app.current_user_id',true),''
                    )::uuid;
                EXCEPTION WHEN invalid_text_representation THEN
                    RAISE EXCEPTION 'PAY-6 offline payment actor context invalid'
                        USING ERRCODE='42501';
                END;
                v_actor_type:=NULLIF(
                    pg_catalog.current_setting('app.current_principal_type',true),''
                );
                v_role:=NULLIF(
                    pg_catalog.current_setting('app.current_role',true),''
                );
                IF v_org IS NULL OR v_actor IS NULL OR v_actor_type IS NULL
                   OR v_actor_type !~ '^[a-z][a-z0-9_]{0,39}$'
                   OR v_role NOT IN ('owner','admin')
                THEN
                    RAISE EXCEPTION 'PAY-6 offline payment requires owner/admin actor context'
                        USING ERRCODE='42501';
                END IF;
                IF p_scope NOT IN (
                    'finance.offline_payment.prepare',
                    'finance.offline_payment.approve',
                    'finance.offline_payment.reject'
                )
                   OR p_idempotency_key IS NULL
                   OR char_length(p_idempotency_key) NOT BETWEEN 1 AND 200
                   OR p_idempotency_key !~ '^[A-Za-z0-9][A-Za-z0-9:._/-]*$'
                   OR p_request_hash !~ '^[0-9a-f]{64}$'
                   OR p_business_reference IS NULL
                   OR char_length(p_business_reference) NOT BETWEEN 1 AND 200
                   OR p_business_reference !~ '^[A-Za-z0-9][A-Za-z0-9:._/-]*$'
                THEN
                    RAISE EXCEPTION 'PAY-6 offline payment command identity invalid'
                        USING ERRCODE='22023';
                END IF;
                v_actor_hash:=pg_catalog.encode(
                    pg_catalog.sha256(
                        pg_catalog.convert_to(v_actor::text,'UTF8')
                    ),
                    'hex'
                );

                SELECT c.* INTO v_existing
                FROM finance.monetary_commands c
                WHERE c.organization_id=v_org
                  AND c.scope=p_scope
                  AND c.idempotency_key=p_idempotency_key
                FOR UPDATE;
                IF FOUND THEN
                    IF v_existing.request_hash_sha256 IS DISTINCT FROM p_request_hash
                       OR v_existing.business_reference IS DISTINCT FROM p_business_reference
                       OR v_existing.actor_type IS DISTINCT FROM v_actor_type
                       OR v_existing.actor_ref_sha256 IS DISTINCT FROM v_actor_hash
                    THEN
                        RAISE EXCEPTION 'PAY-6 offline payment idempotency conflict'
                            USING ERRCODE='23505';
                    END IF;
                    IF v_existing.status='succeeded'
                       AND v_existing.response_ref IS NOT NULL
                    THEN
                        RETURN QUERY SELECT
                            v_existing.id,v_existing.response_ref,false;
                        RETURN;
                    END IF;
                    RAISE EXCEPTION 'PAY-6 offline payment command already processing'
                        USING ERRCODE='40001';
                END IF;

                INSERT INTO finance.monetary_commands(
                    organization_id,scope,idempotency_key,request_hash_sha256,
                    business_reference,correlation_id,actor_type,
                    actor_ref_sha256,status
                ) VALUES (
                    v_org,p_scope,p_idempotency_key,p_request_hash,
                    p_business_reference,pg_catalog.gen_random_uuid(),v_actor_type,
                    v_actor_hash,'processing'
                )
                RETURNING id INTO command_id;
                response_ref:=NULL;
                inserted:=true;
                RETURN NEXT;
            END
            $function$
            """
        )
        op.execute(
            r"""
            CREATE FUNCTION app_secure.pay6_complete_offline_command(
                p_command_id uuid,
                p_response_ref text
            )
            RETURNS boolean
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            BEGIN
                IF current_user<>'app_security_owner'
                   OR p_command_id IS NULL
                   OR p_response_ref IS NULL
                   OR char_length(p_response_ref) NOT BETWEEN 1 AND 200
                   OR p_response_ref !~ '^[A-Za-z0-9][A-Za-z0-9:._/-]*$'
                THEN
                    RAISE EXCEPTION 'PAY-6 command completion invalid'
                        USING ERRCODE='42501';
                END IF;
                UPDATE finance.monetary_commands
                SET status='succeeded',
                    response_ref=p_response_ref,
                    completed_at=pg_catalog.clock_timestamp(),
                    updated_at=pg_catalog.clock_timestamp()
                WHERE id=p_command_id
                  AND status='processing';
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'PAY-6 command completion state invalid'
                        USING ERRCODE='40001';
                END IF;
                RETURN true;
            END
            $function$
            """
        )
        op.execute(
            r"""
            CREATE FUNCTION app_secure.prepare_offline_payment(
                p_invoice_id uuid,
                p_payment_method text,
                p_amount numeric,
                p_currency_code text,
                p_reference_code text,
                p_proof_sha256 text,
                p_idempotency_key text
            )
            RETURNS TABLE(
                offline_payment_request_id uuid,
                invoice_id uuid,
                status text,
                amount numeric,
                currency_code text,
                payment_method text,
                reference_code text,
                prepared_actor_id uuid,
                replayed boolean
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_org uuid;
                v_actor uuid;
                v_actor_type text;
                v_invoice finance.invoices%ROWTYPE;
                v_amount numeric(14,2);
                v_allocated numeric(14,2);
                v_outstanding numeric(14,2);
                v_method text;
                v_currency text;
                v_reference text;
                v_canonical text;
                v_hash text;
                v_cmd record;
                v_request finance.offline_payment_requests%ROWTYPE;
            BEGIN
                BEGIN
                    v_org:=NULLIF(
                        pg_catalog.current_setting('app.current_org_id',true),''
                    )::uuid;
                    v_actor:=NULLIF(
                        pg_catalog.current_setting('app.current_user_id',true),''
                    )::uuid;
                EXCEPTION WHEN invalid_text_representation THEN
                    RAISE EXCEPTION 'PAY-6 offline payment context invalid'
                        USING ERRCODE='42501';
                END;
                v_actor_type:=NULLIF(
                    pg_catalog.current_setting('app.current_principal_type',true),''
                );
                v_method:=pg_catalog.lower(pg_catalog.btrim(p_payment_method));
                v_currency:=pg_catalog.upper(pg_catalog.btrim(p_currency_code));
                v_reference:=pg_catalog.btrim(p_reference_code);
                v_amount:=pg_catalog.round(p_amount,2);

                IF v_org IS NULL OR v_actor IS NULL
                   OR p_invoice_id IS NULL
                   OR v_method NOT IN ('cash','bank_transfer','cheque')
                   OR p_amount IS NULL OR p_amount<>v_amount OR v_amount<=0
                   OR v_currency !~ '^[A-Z]{3}$'
                   OR v_reference !~ '^[A-Za-z0-9][A-Za-z0-9:._/-]{0,119}$'
                   OR p_proof_sha256 !~ '^[0-9a-f]{64}$'
                THEN
                    RAISE EXCEPTION 'PAY-6 offline payment preparation invalid'
                        USING ERRCODE='22023';
                END IF;

                SELECT i.* INTO v_invoice
                FROM finance.invoices i
                WHERE i.id=p_invoice_id
                  AND i.organization_id=v_org
                FOR UPDATE;
                IF NOT FOUND
                   OR v_invoice.status NOT IN ('issued','partially_paid')
                   OR pg_catalog.btrim(v_invoice.currency_code::text)
                        IS DISTINCT FROM v_currency
                THEN
                    RAISE EXCEPTION 'PAY-6 offline payment invoice unavailable'
                        USING ERRCODE='23514';
                END IF;
                SELECT COALESCE(pg_catalog.sum(a.allocated_amount),0)
                INTO v_allocated
                FROM finance.payment_allocations a
                WHERE a.invoice_id=v_invoice.id;
                v_outstanding:=v_invoice.grand_total_amount-v_allocated;
                IF v_amount>v_outstanding THEN
                    RAISE EXCEPTION 'PAY-6 offline payment exceeds invoice outstanding'
                        USING ERRCODE='23514';
                END IF;

                v_canonical :=
                    '{"amount":"'
                    || pg_catalog.to_char(
                        v_amount,'FM999999999999999999999999990.00'
                    )
                    || '","currency":"'
                    || v_currency
                    || '","invoice_id":"'
                    || v_invoice.id::text
                    || '","method":"'
                    || v_method
                    || '","proof_sha256":"'
                    || p_proof_sha256
                    || '","reference":"'
                    || v_reference
                    || '"}';
                v_hash:=pg_catalog.encode(
                    pg_catalog.sha256(
                        pg_catalog.convert_to(v_canonical,'UTF8')
                    ),
                    'hex'
                );
                SELECT * INTO v_cmd
                FROM app_secure.pay6_reserve_offline_command(
                    'finance.offline_payment.prepare',
                    p_idempotency_key,
                    v_hash,
                    'invoice:'||v_invoice.id::text
                );
                IF NOT v_cmd.inserted THEN
                    BEGIN
                        SELECT r.* INTO v_request
                        FROM finance.offline_payment_requests r
                        WHERE r.id=v_cmd.response_ref::uuid
                          AND r.organization_id=v_org;
                    EXCEPTION WHEN invalid_text_representation THEN
                        RAISE EXCEPTION 'PAY-6 offline payment replay unavailable'
                            USING ERRCODE='23514';
                    END;
                    IF NOT FOUND THEN
                        RAISE EXCEPTION 'PAY-6 offline payment replay unavailable'
                            USING ERRCODE='23514';
                    END IF;
                    RETURN QUERY SELECT
                        v_request.id,v_request.invoice_id,v_request.status,
                        v_request.amount,v_request.currency_code::text,
                        v_request.payment_method,v_request.reference_code,
                        v_request.prepared_actor_id,true;
                    RETURN;
                END IF;

                INSERT INTO finance.offline_payment_requests(
                    organization_id,invoice_id,payment_method,reference_code,
                    proof_sha256,amount,currency_code,status,prepared_actor_id,
                    prepared_actor_type,prepare_command_id
                ) VALUES (
                    v_org,v_invoice.id,v_method,v_reference,p_proof_sha256,
                    v_amount,v_currency,'prepared',v_actor,v_actor_type,
                    v_cmd.command_id
                )
                RETURNING * INTO v_request;

                INSERT INTO finance.offline_payment_events(
                    organization_id,offline_payment_request_id,
                    monetary_command_id,event_type,actor_id,actor_type,
                    request_hash_sha256,proof_sha256
                ) VALUES (
                    v_org,v_request.id,v_cmd.command_id,
                    'offline_payment.prepared',v_actor,v_actor_type,
                    v_hash,p_proof_sha256
                );

                PERFORM app_secure.pay6_complete_offline_command(
                    v_cmd.command_id,v_request.id::text
                );
                RETURN QUERY SELECT
                    v_request.id,v_request.invoice_id,v_request.status,
                    v_request.amount,v_request.currency_code::text,
                    v_request.payment_method,v_request.reference_code,
                    v_request.prepared_actor_id,false;
            END
            $function$
            """
        )
        op.execute(
            r"""
            CREATE FUNCTION app_secure.approve_offline_payment(
                p_offline_payment_request_id uuid,
                p_idempotency_key text
            )
            RETURNS TABLE(
                offline_payment_request_id uuid,
                payment_id uuid,
                allocation_id uuid,
                invoice_id uuid,
                invoice_status text,
                status text,
                replayed boolean
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_org uuid;
                v_actor uuid;
                v_actor_type text;
                v_role text;
                v_request finance.offline_payment_requests%ROWTYPE;
                v_invoice finance.invoices%ROWTYPE;
                v_allocated numeric(14,2);
                v_outstanding numeric(14,2);
                v_canonical text;
                v_hash text;
                v_cmd record;
                v_payment finance.payments%ROWTYPE;
                v_apply_hash text;
                v_apply record;
                v_payment_ref text;
                v_event_hash text;
            BEGIN
                BEGIN
                    v_org:=NULLIF(
                        pg_catalog.current_setting('app.current_org_id',true),''
                    )::uuid;
                    v_actor:=NULLIF(
                        pg_catalog.current_setting('app.current_user_id',true),''
                    )::uuid;
                EXCEPTION WHEN invalid_text_representation THEN
                    RAISE EXCEPTION 'PAY-6 offline payment approval context invalid'
                        USING ERRCODE='42501';
                END;
                v_actor_type:=NULLIF(
                    pg_catalog.current_setting('app.current_principal_type',true),''
                );
                v_role:=NULLIF(
                    pg_catalog.current_setting('app.current_role',true),''
                );
                IF v_org IS NULL OR v_actor IS NULL
                   OR v_actor_type IS NULL
                   OR v_role NOT IN ('owner','admin')
                   OR p_offline_payment_request_id IS NULL
                THEN
                    RAISE EXCEPTION 'PAY-6 offline payment approval invalid'
                        USING ERRCODE='22023';
                END IF;

                SELECT r.* INTO v_request
                FROM finance.offline_payment_requests r
                WHERE r.id=p_offline_payment_request_id
                  AND r.organization_id=v_org
                FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'PAY-6 offline payment request unavailable'
                        USING ERRCODE='42501';
                END IF;
                IF v_request.status='approved' THEN
                    SELECT c.* INTO v_cmd
                    FROM finance.monetary_commands c
                    WHERE c.id=v_request.approval_command_id;
                    IF NOT FOUND
                       OR v_cmd.idempotency_key IS DISTINCT FROM p_idempotency_key
                       OR v_cmd.actor_type IS DISTINCT FROM v_actor_type
                       OR v_cmd.actor_ref_sha256::text IS DISTINCT FROM
                          pg_catalog.encode(
                              pg_catalog.sha256(
                                  pg_catalog.convert_to(v_actor::text,'UTF8')
                              ),
                              'hex'
                          )
                    THEN
                        RAISE EXCEPTION 'PAY-6 offline payment already approved'
                            USING ERRCODE='23505';
                    END IF;
                    SELECT p.* INTO v_payment
                    FROM finance.payments p
                    WHERE p.id=v_request.payment_id;
                    SELECT a.id,a.invoice_id,i.status::text
                    INTO v_apply
                    FROM finance.payment_allocations a
                    JOIN finance.invoices i ON i.id=a.invoice_id
                    WHERE a.payment_id=v_request.payment_id
                      AND a.invoice_id=v_request.invoice_id;
                    RETURN QUERY SELECT
                        v_request.id,v_payment.id,v_apply.id,
                        v_request.invoice_id,v_apply.status,
                        v_request.status,true;
                    RETURN;
                END IF;
                IF v_request.status<>'prepared'
                   OR v_request.prepared_actor_id=v_actor
                THEN
                    RAISE EXCEPTION 'PAY-6 maker/checker approval boundary violated'
                        USING ERRCODE='42501';
                END IF;

                SELECT i.* INTO v_invoice
                FROM finance.invoices i
                WHERE i.id=v_request.invoice_id
                  AND i.organization_id=v_org
                FOR UPDATE;
                IF NOT FOUND
                   OR v_invoice.status NOT IN ('issued','partially_paid')
                   OR pg_catalog.btrim(v_invoice.currency_code::text)
                        IS DISTINCT FROM
                        pg_catalog.btrim(v_request.currency_code::text)
                THEN
                    RAISE EXCEPTION 'PAY-6 offline payment invoice no longer payable'
                        USING ERRCODE='23514';
                END IF;
                SELECT COALESCE(pg_catalog.sum(a.allocated_amount),0)
                INTO v_allocated
                FROM finance.payment_allocations a
                WHERE a.invoice_id=v_invoice.id;
                v_outstanding:=v_invoice.grand_total_amount-v_allocated;
                IF v_request.amount>v_outstanding THEN
                    RAISE EXCEPTION 'PAY-6 offline payment approval exceeds outstanding'
                        USING ERRCODE='23514';
                END IF;

                v_canonical :=
                    '{"offline_payment_request_id":"'
                    || v_request.id::text
                    || '","proof_sha256":"'
                    || v_request.proof_sha256
                    || '","status":"approved"}';
                v_hash:=pg_catalog.encode(
                    pg_catalog.sha256(
                        pg_catalog.convert_to(v_canonical,'UTF8')
                    ),
                    'hex'
                );
                SELECT * INTO v_cmd
                FROM app_secure.pay6_reserve_offline_command(
                    'finance.offline_payment.approve',
                    p_idempotency_key,
                    v_hash,
                    'offline_payment:'||v_request.id::text
                );
                IF NOT v_cmd.inserted THEN
                    RAISE EXCEPTION 'PAY-6 offline approval replay state unavailable'
                        USING ERRCODE='40001';
                END IF;

                v_payment_ref :=
                    'manual/'||v_org::text||'/'||v_request.payment_method
                    ||'/'||v_request.reference_code;
                INSERT INTO finance.payments(
                    organization_id,legal_entity_id,gst_registration_id,
                    division_id,brand_id,provider_code,provider_payment_ref,
                    amount,currency_code,status,raw_status
                ) VALUES (
                    v_org,v_invoice.legal_entity_id,v_invoice.gst_registration_id,
                    v_invoice.division_id,v_invoice.brand_id,'manual',
                    v_payment_ref,v_request.amount,v_request.currency_code,
                    'captured','offline_approved'
                )
                RETURNING * INTO v_payment;

                v_event_hash:=pg_catalog.encode(
                    pg_catalog.sha256(
                        pg_catalog.convert_to(
                            v_canonical||'|'||v_payment.id::text,'UTF8'
                        )
                    ),
                    'hex'
                );
                INSERT INTO finance.payment_events(
                    payment_id,provider_code,provider_event_id,event_type,
                    event_payload_sha256
                ) VALUES (
                    v_payment.id,'manual',
                    'offline/'||v_request.id::text||'/approved',
                    'manual.payment.approved',v_event_hash
                );

                v_apply_hash:=pg_catalog.encode(
                    pg_catalog.sha256(
                        pg_catalog.convert_to(
                            '{"amount":"'
                            || pg_catalog.to_char(
                                v_request.amount,
                                'FM999999999999999999999999990.00'
                            )
                            || '","invoice_id":"'
                            || v_invoice.id::text
                            || '","payment_id":"'
                            || v_payment.id::text
                            || '"}',
                            'UTF8'
                        )
                    ),
                    'hex'
                );
                SELECT * INTO v_apply
                FROM app_secure.apply_finance_confirmed_payment(
                    v_payment.id,v_invoice.id,v_request.amount,
                    v_request.currency_code::text,
                    'offline-payment/'||v_request.id::text||'/apply',
                    v_apply_hash
                );

                UPDATE finance.offline_payment_requests
                SET status='approved',
                    approved_actor_id=v_actor,
                    approved_actor_type=v_actor_type,
                    approval_command_id=v_cmd.command_id,
                    payment_id=v_payment.id,
                    decided_at=pg_catalog.clock_timestamp(),
                    updated_at=pg_catalog.clock_timestamp()
                WHERE id=v_request.id;

                INSERT INTO finance.offline_payment_events(
                    organization_id,offline_payment_request_id,
                    monetary_command_id,event_type,actor_id,actor_type,
                    request_hash_sha256,proof_sha256
                ) VALUES (
                    v_org,v_request.id,v_cmd.command_id,
                    'offline_payment.approved',v_actor,v_actor_type,
                    v_hash,v_request.proof_sha256
                );

                PERFORM app_secure.pay6_complete_offline_command(
                    v_cmd.command_id,v_payment.id::text
                );
                RETURN QUERY SELECT
                    v_request.id,v_payment.id,v_apply.allocation_id,
                    v_invoice.id,v_apply.invoice_status,'approved',false;
            END
            $function$
            """
        )
        op.execute(
            r"""
            CREATE FUNCTION app_secure.reject_offline_payment(
                p_offline_payment_request_id uuid,
                p_reason_code text,
                p_idempotency_key text
            )
            RETURNS TABLE(
                offline_payment_request_id uuid,
                status text,
                rejection_reason_code text,
                replayed boolean
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_org uuid;
                v_actor uuid;
                v_actor_type text;
                v_role text;
                v_request finance.offline_payment_requests%ROWTYPE;
                v_reason text;
                v_canonical text;
                v_hash text;
                v_cmd record;
            BEGIN
                BEGIN
                    v_org:=NULLIF(
                        pg_catalog.current_setting('app.current_org_id',true),''
                    )::uuid;
                    v_actor:=NULLIF(
                        pg_catalog.current_setting('app.current_user_id',true),''
                    )::uuid;
                EXCEPTION WHEN invalid_text_representation THEN
                    RAISE EXCEPTION 'PAY-6 offline rejection context invalid'
                        USING ERRCODE='42501';
                END;
                v_actor_type:=NULLIF(
                    pg_catalog.current_setting('app.current_principal_type',true),''
                );
                v_role:=NULLIF(
                    pg_catalog.current_setting('app.current_role',true),''
                );
                v_reason:=pg_catalog.lower(pg_catalog.btrim(p_reason_code));
                IF v_org IS NULL OR v_actor IS NULL
                   OR v_actor_type IS NULL
                   OR v_role NOT IN ('owner','admin')
                   OR p_offline_payment_request_id IS NULL
                   OR v_reason !~ '^[a-z][a-z0-9_]{0,79}$'
                   OR v_reason ~ '(secret|token|bearer)'
                THEN
                    RAISE EXCEPTION 'PAY-6 offline rejection invalid'
                        USING ERRCODE='22023';
                END IF;
                SELECT r.* INTO v_request
                FROM finance.offline_payment_requests r
                WHERE r.id=p_offline_payment_request_id
                  AND r.organization_id=v_org
                FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'PAY-6 offline payment request unavailable'
                        USING ERRCODE='42501';
                END IF;
                IF v_request.status='rejected' THEN
                    SELECT c.* INTO v_cmd
                    FROM finance.monetary_commands c
                    WHERE c.id=v_request.rejection_command_id;
                    IF NOT FOUND
                       OR v_cmd.idempotency_key IS DISTINCT FROM p_idempotency_key
                       OR v_cmd.actor_type IS DISTINCT FROM v_actor_type
                       OR v_cmd.actor_ref_sha256::text IS DISTINCT FROM
                          pg_catalog.encode(
                              pg_catalog.sha256(
                                  pg_catalog.convert_to(v_actor::text,'UTF8')
                              ),
                              'hex'
                          )
                       OR v_request.rejection_reason_code IS DISTINCT FROM v_reason
                    THEN
                        RAISE EXCEPTION 'PAY-6 offline payment already rejected'
                            USING ERRCODE='23505';
                    END IF;
                    RETURN QUERY SELECT
                        v_request.id,v_request.status,
                        v_request.rejection_reason_code,true;
                    RETURN;
                END IF;
                IF v_request.status<>'prepared'
                   OR v_request.prepared_actor_id=v_actor
                THEN
                    RAISE EXCEPTION 'PAY-6 maker/checker rejection boundary violated'
                        USING ERRCODE='42501';
                END IF;

                v_canonical :=
                    '{"offline_payment_request_id":"'
                    || v_request.id::text
                    || '","reason_code":"'
                    || v_reason
                    || '","status":"rejected"}';
                v_hash:=pg_catalog.encode(
                    pg_catalog.sha256(
                        pg_catalog.convert_to(v_canonical,'UTF8')
                    ),
                    'hex'
                );
                SELECT * INTO v_cmd
                FROM app_secure.pay6_reserve_offline_command(
                    'finance.offline_payment.reject',
                    p_idempotency_key,
                    v_hash,
                    'offline_payment:'||v_request.id::text
                );
                IF NOT v_cmd.inserted THEN
                    RAISE EXCEPTION 'PAY-6 offline rejection replay unavailable'
                        USING ERRCODE='40001';
                END IF;

                UPDATE finance.offline_payment_requests
                SET status='rejected',
                    rejected_actor_id=v_actor,
                    rejected_actor_type=v_actor_type,
                    rejection_command_id=v_cmd.command_id,
                    rejection_reason_code=v_reason,
                    decided_at=pg_catalog.clock_timestamp(),
                    updated_at=pg_catalog.clock_timestamp()
                WHERE id=v_request.id;

                INSERT INTO finance.offline_payment_events(
                    organization_id,offline_payment_request_id,
                    monetary_command_id,event_type,actor_id,actor_type,
                    request_hash_sha256,proof_sha256
                ) VALUES (
                    v_org,v_request.id,v_cmd.command_id,
                    'offline_payment.rejected',v_actor,v_actor_type,
                    v_hash,v_request.proof_sha256
                );

                PERFORM app_secure.pay6_complete_offline_command(
                    v_cmd.command_id,v_request.id::text
                );
                RETURN QUERY SELECT
                    v_request.id,'rejected'::text,v_reason,false;
            END
            $function$
            """
        )

        for signature in (
            _EVENT_GUARD,_RESERVE,_COMPLETE,_PREPARE,_APPROVE,_REJECT
        ):
            op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        for signature in (_PREPARE,_APPROVE,_REJECT):
            op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO app_runtime")

        # migration_owner owns the trigger target table but intentionally has
        # no persistent app_secure visibility. Grant only what PostgreSQL needs
        # to bind the trigger function, then revoke immediately after creation.
        op.execute("GRANT USAGE ON SCHEMA app_secure TO migration_owner")
        op.execute(
            "GRANT EXECUTE ON FUNCTION "
            "app_secure.pay6_reject_offline_event_mutation() "
            "TO migration_owner"
        )
    finally:
        op.execute("RESET ROLE")

    if not had_create:
        bind.execute(
            sa.text(
                "REVOKE CREATE ON SCHEMA app_secure FROM app_security_owner"
            )
        )

    op.execute(
        """
        CREATE TRIGGER trg_pay6_offline_events_immutable
        BEFORE UPDATE OR DELETE ON finance.offline_payment_events
        FOR EACH ROW EXECUTE FUNCTION app_secure.pay6_reject_offline_event_mutation()
        """
    )

    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        op.execute(
            "REVOKE EXECUTE ON FUNCTION "
            "app_secure.pay6_reject_offline_event_mutation() "
            "FROM migration_owner"
        )
        op.execute(
            "REVOKE USAGE ON SCHEMA app_secure FROM migration_owner"
        )
    finally:
        op.execute("RESET ROLE")


def upgrade() -> None:
    bind=op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _require_identity(bind)

    for relation in (
        "finance.offline_payment_requests",
        "finance.offline_payment_events",
    ):
        if bind.execute(
            sa.text("SELECT pg_catalog.to_regclass(:name) IS NOT NULL"),
            {"name":relation},
        ).scalar_one():
            raise RuntimeError(f"PAY-6 relation already exists: {relation}")

    _install_acl_delta(bind)
    _install_schema()
    _install_functions(bind)

    for relation in (
        "finance.offline_payment_requests",
        "finance.offline_payment_events",
        "finance.payments",
        "finance.payment_events",
        "finance.monetary_commands",
    ):
        for privilege in ("SELECT","INSERT","UPDATE","DELETE","TRUNCATE"):
            if bind.execute(
                sa.text(
                    "SELECT pg_catalog.has_table_privilege("
                    "'app_runtime',:relation,:privilege)"
                ),
                {"relation":relation,"privilege":privilege},
            ).scalar_one():
                raise RuntimeError(
                    f"PAY-6 leaked direct Finance DML: {relation} {privilege}"
                )


def downgrade() -> None:
    bind=op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _require_identity(bind)

    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        has_evidence=bind.execute(
            sa.text(
                """
                SELECT EXISTS(
                    SELECT 1
                    FROM finance.offline_payment_requests
                    LIMIT 1
                )
                """
            )
        ).scalar_one()
    finally:
        op.execute("RESET ROLE")
    if has_evidence:
        raise RuntimeError(
            "PAY-6 downgrade blocked: offline payment evidence exists"
        )

    op.execute(
        "DROP TRIGGER IF EXISTS trg_pay6_offline_events_immutable "
        "ON finance.offline_payment_events"
    )
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        for signature in (
            _REJECT,_APPROVE,_PREPARE,_COMPLETE,_RESERVE,_EVENT_GUARD
        ):
            op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    finally:
        op.execute("RESET ROLE")

    op.execute(
        "DROP POLICY pay6_offline_payment_requests_security_owner_update "
        "ON finance.offline_payment_requests"
    )
    for table in ("offline_payment_events","offline_payment_requests"):
        op.execute(
            f"DROP POLICY pay6_{table}_security_owner_insert "
            f"ON finance.{table}"
        )
        op.execute(
            f"DROP POLICY pay6_{table}_security_owner_select "
            f"ON finance.{table}"
        )
    op.execute("DROP TABLE finance.offline_payment_events")
    op.execute("DROP TABLE finance.offline_payment_requests")

    rows=bind.execute(
        sa.text(
            """
            SELECT table_name,column_name,privilege_name
            FROM app_private.pay6_offline_payment_acl_delta
            ORDER BY table_name DESC,privilege_name DESC,column_name DESC
            """
        )
    ).mappings().all()
    for row in rows:
        table_name=row["table_name"]
        column_name=row["column_name"]
        privilege=row["privilege_name"]
        if (
            not table_name.replace("_","").isalnum()
            or not column_name.replace("_","").isalnum()
            or privilege not in {"SELECT","INSERT","UPDATE"}
        ):
            raise RuntimeError("PAY-6 stored ACL delta identifier unsafe")
        bind.execute(
            sa.text(
                f"REVOKE {privilege} ({column_name}) "
                f"ON TABLE finance.{table_name} FROM app_security_owner"
            )
        )
    bind.execute(
        sa.text("DROP TABLE app_private.pay6_offline_payment_acl_delta")
    )
