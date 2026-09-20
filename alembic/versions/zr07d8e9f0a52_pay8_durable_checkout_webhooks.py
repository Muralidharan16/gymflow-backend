"""PAY-8 durable checkout provider operations and webhook inbox.

Revision ID: zr07d8e9f0a52
Revises: zq07d8e9f0a51
Create Date: 2026-09-20

Adds durable Finance-side saga state around outbound checkout creation and
verified provider webhooks.  Provider adapters remain non-authoritative:
successful provider objects are bound to Finance only through bounded database
capabilities, ambiguous outbound outcomes are fenced as unknown, and webhook
delivery is durably recorded before provider evidence is applied.

No live provider mode, live money movement, refund-provider execution, release,
deployment, or customer-production activation is introduced.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "zr07d8e9f0a52"
down_revision = "zq07d8e9f0a51"
branch_labels = None
depends_on = None


_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_APP = "app_runtime"
_WORKER = "worker_runtime"
_TENANT = "NULLIF(pg_catalog.current_setting('app.current_org_id',true),'')::uuid"

_RESERVE_OPERATION = (
    "app_secure.reserve_finance_provider_operation("
    "uuid,text,text,text,text,text)"
)
_CLAIM_OPERATION = "app_secure.claim_finance_provider_operation(uuid,uuid)"
_FINISH_OPERATION = (
    "app_secure.finish_finance_provider_operation("
    "uuid,uuid,bigint,text,text,text,text)"
)
_RECONCILE_OPERATION = (
    "app_secure.reconcile_finance_provider_operation("
    "uuid,text,text,text,text)"
)
_RECORD_WEBHOOK = (
    "app_secure.record_finance_provider_webhook("
    "text,text,text,text,text,text,text,text,bigint,text,text,boolean,"
    "text,text,text,bigint)"
)
_CLAIM_WEBHOOK = "app_secure.claim_finance_provider_webhook(uuid,uuid)"
_CLAIM_NEXT_WEBHOOK = "app_secure.claim_next_finance_provider_webhook(uuid)"
_COMPLETE_WEBHOOK = (
    "app_secure.complete_finance_provider_webhook(uuid,uuid,bigint,uuid)"
)
_FAIL_WEBHOOK = (
    "app_secure.fail_finance_provider_webhook("
    "uuid,uuid,bigint,text,boolean)"
)


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
        raise RuntimeError(f"PAY-8 missing externally managed role: {role}")
    if bool(row["rolcanlogin"]) is not login:
        raise RuntimeError(f"PAY-8 role login posture drift: {role}")
    for key in (
        "rolsuper","rolinherit","rolcreatedb","rolcreaterole",
        "rolreplication","rolbypassrls",
    ):
        if bool(row[key]):
            raise RuntimeError(f"PAY-8 reduced-role drift: {role}.{key}")


def _require_identity(bind) -> None:
    _require_role(bind, _MIGRATION_OWNER, login=True)
    _require_role(bind, _SECURITY_OWNER)
    _require_role(bind, _APP)
    _require_role(bind, _WORKER)
    identity = bind.execute(
        sa.text("SELECT session_user::text,current_user::text")
    ).one()
    if tuple(identity) != (_MIGRATION_OWNER, _MIGRATION_OWNER):
        raise RuntimeError("PAY-8 migration requires migration_owner")
    if not bind.execute(
        sa.text("SELECT pg_catalog.pg_has_role(:member,:target,'SET')"),
        {"member": _MIGRATION_OWNER, "target": _SECURITY_OWNER},
    ).scalar_one():
        raise RuntimeError(
            "PAY-8 requires migration_owner SET edge to app_security_owner"
        )
    for runtime in (_APP, _WORKER):
        if bind.execute(
            sa.text(
                "SELECT pg_catalog.pg_has_role(:member,:target,'MEMBER') "
                "OR pg_catalog.pg_has_role(:member,:target,'SET')"
            ),
            {"member": runtime, "target": _SECURITY_OWNER},
        ).scalar_one():
            raise RuntimeError(
                f"PAY-8 runtime may reach app_security_owner: {runtime}"
            )


def _require_predecessor(bind) -> None:
    for relation in (
        "finance.payments",
        "finance.payment_events",
        "finance.invoices",
        "finance.member_subscription_checkout_bindings",
    ):
        if bind.execute(
            sa.text("SELECT pg_catalog.to_regclass(:relation) IS NULL"),
            {"relation": relation},
        ).scalar_one():
            raise RuntimeError(f"PAY-8 missing predecessor relation: {relation}")

    if not bind.execute(
        sa.text(
            """
            SELECT pg_catalog.has_column_privilege(
                'app_security_owner',
                'finance.payments',
                'provider_order_ref',
                'UPDATE'
            )
            AND pg_catalog.has_column_privilege(
                'app_security_owner',
                'finance.payments',
                'updated_at',
                'UPDATE'
            )
            AND pg_catalog.has_column_privilege(
                'app_security_owner',
                'finance.payments',
                'id',
                'SELECT'
            )
            AND pg_catalog.has_column_privilege(
                'app_security_owner',
                'finance.payments',
                'organization_id',
                'SELECT'
            )
            AND pg_catalog.has_column_privilege(
                'app_security_owner',
                'finance.payments',
                'provider_code',
                'SELECT'
            )
            AND pg_catalog.has_column_privilege(
                'app_security_owner',
                'finance.payments',
                'provider_order_ref',
                'SELECT'
            )
            """
        )
    ).scalar_one():
        raise RuntimeError(
            "PAY-8 predecessor payment provider-reference authority drift"
        )


def _install_tables() -> None:
    op.execute(
        """
        CREATE TABLE finance.provider_operations(
            id UUID PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
            organization_id UUID NOT NULL
                REFERENCES public.organizations(id) ON DELETE RESTRICT,
            payment_id UUID NOT NULL
                REFERENCES finance.payments(id) ON DELETE RESTRICT,
            provider_code VARCHAR(40) NOT NULL,
            environment VARCHAR(16) NOT NULL,
            operation_type VARCHAR(40) NOT NULL,
            idempotency_key VARCHAR(200) NOT NULL,
            request_hash_sha256 CHAR(64) NOT NULL,
            status VARCHAR(24) NOT NULL DEFAULT 'reserved',
            provider_object_id VARCHAR(200) NULL,
            provider_evidence_sha256 CHAR(64) NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 5,
            lease_owner UUID NULL,
            lease_until TIMESTAMPTZ NULL,
            lease_fence BIGINT NOT NULL DEFAULT 0,
            last_error_code VARCHAR(80) NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            last_started_at TIMESTAMPTZ NULL,
            completed_at TIMESTAMPTZ NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            CONSTRAINT uq_pay8_provider_operation_key
                UNIQUE(
                    organization_id,provider_code,environment,
                    operation_type,idempotency_key
                ),
            CONSTRAINT uq_pay8_provider_operation_payment
                UNIQUE(payment_id,operation_type),
            CONSTRAINT uq_pay8_provider_object
                UNIQUE(provider_code,environment,provider_object_id),
            CONSTRAINT chk_pay8_provider_code
                CHECK(provider_code ~ '^[a-z0-9_]{1,40}$'),
            CONSTRAINT chk_pay8_provider_environment
                CHECK(environment IN ('sandbox','test')),
            CONSTRAINT chk_pay8_provider_operation_type
                CHECK(operation_type='create_checkout'),
            CONSTRAINT chk_pay8_provider_operation_key
                CHECK(
                    char_length(idempotency_key) BETWEEN 1 AND 200
                    AND idempotency_key ~
                        '^[A-Za-z0-9][A-Za-z0-9:._/-]*$'
                ),
            CONSTRAINT chk_pay8_provider_operation_hash
                CHECK(request_hash_sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT chk_pay8_provider_operation_status
                CHECK(status IN (
                    'reserved','in_flight','succeeded',
                    'failed_retryable','failed_final','unknown'
                )),
            CONSTRAINT chk_pay8_provider_operation_object
                CHECK(
                    provider_object_id IS NULL
                    OR provider_object_id ~ '^[A-Za-z0-9_:-]{1,200}$'
                ),
            CONSTRAINT chk_pay8_provider_operation_evidence
                CHECK(
                    provider_evidence_sha256 IS NULL
                    OR provider_evidence_sha256 ~ '^[0-9a-f]{64}$'
                ),
            CONSTRAINT chk_pay8_provider_operation_attempts
                CHECK(
                    attempt_count >= 0
                    AND max_attempts BETWEEN 1 AND 20
                    AND attempt_count <= max_attempts
                    AND lease_fence >= 0
                ),
            CONSTRAINT chk_pay8_provider_operation_lease
                CHECK(
                    (status='in_flight') =
                    (lease_owner IS NOT NULL AND lease_until IS NOT NULL)
                ),
            CONSTRAINT chk_pay8_provider_operation_error
                CHECK(
                    last_error_code IS NULL
                    OR (
                        last_error_code ~ '^[a-z][a-z0-9_]{0,79}$'
                        AND last_error_code !~ '(secret|token|bearer|password)'
                    )
                ),
            CONSTRAINT chk_pay8_provider_operation_terminal
                CHECK(
                    (status='succeeded'
                     AND provider_object_id IS NOT NULL
                     AND provider_evidence_sha256 IS NOT NULL
                     AND completed_at IS NOT NULL)
                    OR
                    (status='failed_final' AND completed_at IS NOT NULL)
                    OR
                    (status NOT IN ('succeeded','failed_final')
                     AND completed_at IS NULL)
                )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_pay8_provider_operations_org_status
        ON finance.provider_operations(
            organization_id,status,updated_at,id
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_pay8_provider_operations_recovery
        ON finance.provider_operations(lease_until,id)
        WHERE status='in_flight'
        """
    )

    op.execute(
        """
        CREATE TABLE finance.provider_webhook_inbox(
            id UUID PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
            provider_code VARCHAR(40) NOT NULL,
            environment VARCHAR(16) NOT NULL,
            provider_event_id VARCHAR(200) NOT NULL,
            payload_sha256 CHAR(64) NOT NULL,
            signature_sha256 CHAR(64) NOT NULL,
            event_type VARCHAR(120) NOT NULL,
            provider_order_ref VARCHAR(200) NOT NULL,
            provider_payment_ref VARCHAR(200) NOT NULL,
            provider_amount_subunits BIGINT NOT NULL,
            provider_currency CHAR(3) NOT NULL,
            provider_payment_status VARCHAR(80) NOT NULL,
            provider_captured BOOLEAN NOT NULL,
            provider_payment_order_ref VARCHAR(200) NOT NULL,
            provider_order_entity_ref VARCHAR(200) NULL,
            provider_order_status VARCHAR(80) NULL,
            provider_event_timestamp BIGINT NULL,
            organization_id UUID NULL
                REFERENCES public.organizations(id) ON DELETE RESTRICT,
            payment_id UUID NULL
                REFERENCES finance.payments(id) ON DELETE RESTRICT,
            payment_event_id UUID NULL
                REFERENCES finance.payment_events(id) ON DELETE RESTRICT,
            status VARCHAR(24) NOT NULL DEFAULT 'received',
            processing_attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 15,
            lease_owner UUID NULL,
            lease_until TIMESTAMPTZ NULL,
            lease_fence BIGINT NOT NULL DEFAULT 0,
            last_error_code VARCHAR(80) NULL,
            received_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            processed_at TIMESTAMPTZ NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            CONSTRAINT uq_pay8_webhook_provider_event
                UNIQUE(provider_code,environment,provider_event_id),
            CONSTRAINT chk_pay8_webhook_provider
                CHECK(provider_code ~ '^[a-z0-9_]{1,40}$'),
            CONSTRAINT chk_pay8_webhook_environment
                CHECK(environment IN ('sandbox','test')),
            CONSTRAINT chk_pay8_webhook_event_id
                CHECK(provider_event_id ~ '^[A-Za-z0-9_-]{1,200}$'),
            CONSTRAINT chk_pay8_webhook_hashes
                CHECK(
                    payload_sha256 ~ '^[0-9a-f]{64}$'
                    AND signature_sha256 ~ '^[0-9a-f]{64}$'
                ),
            CONSTRAINT chk_pay8_webhook_event_type
                CHECK(event_type IN (
                    'payment.authorized','payment.captured',
                    'payment.failed','order.paid'
                )),
            CONSTRAINT chk_pay8_webhook_refs
                CHECK(
                    provider_order_ref ~ '^[A-Za-z0-9_:-]{1,200}$'
                    AND provider_payment_ref ~ '^[A-Za-z0-9_:-]{1,200}$'
                    AND provider_payment_order_ref ~
                        '^[A-Za-z0-9_:-]{1,200}$'
                    AND (
                        provider_order_entity_ref IS NULL
                        OR provider_order_entity_ref ~
                            '^[A-Za-z0-9_:-]{1,200}$'
                    )
                ),
            CONSTRAINT chk_pay8_webhook_amount
                CHECK(provider_amount_subunits >= 0),
            CONSTRAINT chk_pay8_webhook_currency
                CHECK(provider_currency ~ '^[A-Z]{3}$'),
            CONSTRAINT chk_pay8_webhook_status
                CHECK(status IN (
                    'received','processing','processed',
                    'retry','dead_letter'
                )),
            CONSTRAINT chk_pay8_webhook_attempts
                CHECK(
                    processing_attempts >= 0
                    AND max_attempts BETWEEN 1 AND 50
                    AND processing_attempts <= max_attempts
                    AND lease_fence >= 0
                ),
            CONSTRAINT chk_pay8_webhook_lease
                CHECK(
                    (status='processing') =
                    (lease_owner IS NOT NULL AND lease_until IS NOT NULL)
                ),
            CONSTRAINT chk_pay8_webhook_error
                CHECK(
                    last_error_code IS NULL
                    OR (
                        last_error_code ~ '^[a-z][a-z0-9_]{0,79}$'
                        AND last_error_code !~ '(secret|token|bearer|password)'
                    )
                ),
            CONSTRAINT chk_pay8_webhook_processed
                CHECK(
                    (status='processed') =
                    (
                        payment_event_id IS NOT NULL
                        AND payment_id IS NOT NULL
                        AND organization_id IS NOT NULL
                        AND processed_at IS NOT NULL
                    )
                )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_pay8_webhook_inbox_status
        ON finance.provider_webhook_inbox(
            status,received_at,id
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_pay8_webhook_inbox_recovery
        ON finance.provider_webhook_inbox(lease_until,id)
        WHERE status='processing'
        """
    )

    op.execute(
        "ALTER TABLE finance.provider_operations "
        "ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        "ALTER TABLE finance.provider_operations "
        "FORCE ROW LEVEL SECURITY"
    )
    op.execute(
        "REVOKE ALL ON TABLE finance.provider_operations FROM PUBLIC"
    )
    op.execute(
        "GRANT SELECT,INSERT ON TABLE finance.provider_operations "
        "TO app_security_owner"
    )
    op.execute(
        """
        GRANT UPDATE(
            status,provider_object_id,provider_evidence_sha256,
            attempt_count,lease_owner,lease_until,lease_fence,
            last_error_code,last_started_at,completed_at,updated_at
        )
        ON TABLE finance.provider_operations
        TO app_security_owner
        """
    )
    op.execute(
        f"""
        CREATE POLICY pay8_provider_operations_security_owner_select
        ON finance.provider_operations
        FOR SELECT TO app_security_owner
        USING (organization_id={_TENANT})
        """
    )
    op.execute(
        f"""
        CREATE POLICY pay8_provider_operations_security_owner_insert
        ON finance.provider_operations
        FOR INSERT TO app_security_owner
        WITH CHECK (organization_id={_TENANT})
        """
    )
    op.execute(
        f"""
        CREATE POLICY pay8_provider_operations_security_owner_update
        ON finance.provider_operations
        FOR UPDATE TO app_security_owner
        USING (organization_id={_TENANT})
        WITH CHECK (organization_id={_TENANT})
        """
    )

    op.execute(
        "ALTER TABLE finance.provider_webhook_inbox "
        "ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        "ALTER TABLE finance.provider_webhook_inbox "
        "FORCE ROW LEVEL SECURITY"
    )
    op.execute(
        "REVOKE ALL ON TABLE finance.provider_webhook_inbox FROM PUBLIC"
    )
    op.execute(
        "GRANT SELECT,INSERT ON TABLE finance.provider_webhook_inbox "
        "TO app_security_owner"
    )
    op.execute(
        """
        GRANT UPDATE(
            organization_id,payment_id,payment_event_id,status,
            processing_attempts,lease_owner,lease_until,lease_fence,
            last_error_code,processed_at,updated_at
        )
        ON TABLE finance.provider_webhook_inbox
        TO app_security_owner
        """
    )
    op.execute(
        """
        CREATE POLICY pay8_webhook_security_owner_all
        ON finance.provider_webhook_inbox
        FOR ALL TO app_security_owner
        USING (true)
        WITH CHECK (true)
        """
    )


def _install_functions(bind) -> None:
    had_create = bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.has_schema_privilege("
                ":role_name,'app_secure','CREATE')"
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
            CREATE FUNCTION app_secure.reserve_finance_provider_operation(
                p_payment_id uuid,
                p_provider_code text,
                p_environment text,
                p_operation_type text,
                p_idempotency_key text,
                p_request_hash text
            )
            RETURNS TABLE(
                operation_id uuid,
                status text,
                provider_object_id text,
                attempt_count integer,
                replayed boolean
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_org uuid;
                v_payment finance.payments%ROWTYPE;
                v_operation finance.provider_operations%ROWTYPE;
                v_inserted_count integer:=0;
            BEGIN
                IF NOT pg_catalog.pg_has_role(
                    session_user,'app_runtime','MEMBER'
                ) THEN
                    RAISE EXCEPTION
                        'PAY-8 provider operation requires app_runtime'
                        USING ERRCODE='42501';
                END IF;
                BEGIN
                    v_org:=NULLIF(
                        pg_catalog.current_setting(
                            'app.current_org_id',true
                        ),''
                    )::uuid;
                EXCEPTION WHEN invalid_text_representation THEN
                    RAISE EXCEPTION
                        'PAY-8 provider operation tenant invalid'
                        USING ERRCODE='42501';
                END;
                IF v_org IS NULL
                   OR p_payment_id IS NULL
                   OR p_provider_code !~ '^[a-z0-9_]{1,40}$'
                   OR p_environment NOT IN ('sandbox','test')
                   OR p_operation_type<>'create_checkout'
                   OR p_idempotency_key IS NULL
                   OR char_length(p_idempotency_key) NOT BETWEEN 1 AND 200
                   OR p_idempotency_key !~
                      '^[A-Za-z0-9][A-Za-z0-9:._/-]*$'
                   OR p_request_hash !~ '^[0-9a-f]{64}$'
                THEN
                    RAISE EXCEPTION
                        'PAY-8 provider operation identity invalid'
                        USING ERRCODE='22023';
                END IF;

                SELECT p.* INTO v_payment
                FROM finance.payments p
                WHERE p.id=p_payment_id
                  AND p.organization_id=v_org
                FOR UPDATE;
                IF NOT FOUND
                   OR v_payment.provider_code
                      IS DISTINCT FROM p_provider_code
                   OR v_payment.status NOT IN ('created','pending')
                THEN
                    RAISE EXCEPTION
                        'PAY-8 provider operation payment unavailable'
                        USING ERRCODE='23514';
                END IF;

                INSERT INTO finance.provider_operations(
                    organization_id,payment_id,provider_code,environment,
                    operation_type,idempotency_key,request_hash_sha256
                ) VALUES (
                    v_org,p_payment_id,p_provider_code,p_environment,
                    p_operation_type,p_idempotency_key,p_request_hash
                )
                ON CONFLICT DO NOTHING;
                GET DIAGNOSTICS v_inserted_count=ROW_COUNT;

                SELECT o.* INTO v_operation
                FROM finance.provider_operations o
                WHERE o.payment_id=p_payment_id
                  AND o.operation_type=p_operation_type
                FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'PAY-8 provider operation reservation disappeared'
                        USING ERRCODE='40001';
                END IF;
                IF v_operation.organization_id IS DISTINCT FROM v_org
                   OR v_operation.provider_code
                      IS DISTINCT FROM p_provider_code
                   OR v_operation.environment
                      IS DISTINCT FROM p_environment
                   OR v_operation.idempotency_key
                      IS DISTINCT FROM p_idempotency_key
                   OR v_operation.request_hash_sha256::text
                      IS DISTINCT FROM p_request_hash
                THEN
                    RAISE EXCEPTION
                        'PAY-8 provider operation idempotency conflict'
                        USING ERRCODE='23505';
                END IF;

                RETURN QUERY SELECT
                    v_operation.id,
                    v_operation.status::text,
                    v_operation.provider_object_id::text,
                    v_operation.attempt_count,
                    v_inserted_count=0;
            END
            $function$
            """
        )
        op.execute(
            r"""
            CREATE FUNCTION app_secure.claim_finance_provider_operation(
                p_operation_id uuid,
                p_lease_owner uuid
            )
            RETURNS TABLE(
                operation_id uuid,
                status text,
                lease_fence bigint,
                provider_object_id text,
                attempt_count integer,
                claimed boolean
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_org uuid;
                v_operation finance.provider_operations%ROWTYPE;
            BEGIN
                IF NOT pg_catalog.pg_has_role(
                    session_user,'app_runtime','MEMBER'
                ) THEN
                    RAISE EXCEPTION
                        'PAY-8 provider claim requires app_runtime'
                        USING ERRCODE='42501';
                END IF;
                BEGIN
                    v_org:=NULLIF(
                        pg_catalog.current_setting(
                            'app.current_org_id',true
                        ),''
                    )::uuid;
                EXCEPTION WHEN invalid_text_representation THEN
                    RAISE EXCEPTION
                        'PAY-8 provider claim tenant invalid'
                        USING ERRCODE='42501';
                END;
                IF v_org IS NULL
                   OR p_operation_id IS NULL
                   OR p_lease_owner IS NULL
                THEN
                    RAISE EXCEPTION
                        'PAY-8 provider claim identity invalid'
                        USING ERRCODE='22023';
                END IF;

                SELECT o.* INTO v_operation
                FROM finance.provider_operations o
                WHERE o.id=p_operation_id
                  AND o.organization_id=v_org
                FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'PAY-8 provider operation unavailable'
                        USING ERRCODE='P0002';
                END IF;

                IF v_operation.status='in_flight'
                   AND v_operation.lease_until<=
                       pg_catalog.clock_timestamp()
                THEN
                    UPDATE finance.provider_operations
                    SET status='unknown',
                        lease_owner=NULL,
                        lease_until=NULL,
                        last_error_code='lease_expired_unknown',
                        updated_at=pg_catalog.clock_timestamp()
                    WHERE id=v_operation.id
                    RETURNING * INTO v_operation;
                    RETURN QUERY SELECT
                        v_operation.id,v_operation.status::text,
                        v_operation.lease_fence,
                        v_operation.provider_object_id::text,
                        v_operation.attempt_count,false;
                    RETURN;
                END IF;

                IF v_operation.status='failed_retryable'
                   AND v_operation.attempt_count>=v_operation.max_attempts
                THEN
                    UPDATE finance.provider_operations
                    SET status='failed_final',
                        last_error_code='retry_exhausted',
                        completed_at=pg_catalog.clock_timestamp(),
                        updated_at=pg_catalog.clock_timestamp()
                    WHERE id=v_operation.id
                    RETURNING * INTO v_operation;
                    RETURN QUERY SELECT
                        v_operation.id,v_operation.status::text,
                        v_operation.lease_fence,
                        v_operation.provider_object_id::text,
                        v_operation.attempt_count,false;
                    RETURN;
                END IF;

                IF v_operation.status NOT IN (
                    'reserved','failed_retryable'
                ) THEN
                    RETURN QUERY SELECT
                        v_operation.id,v_operation.status::text,
                        v_operation.lease_fence,
                        v_operation.provider_object_id::text,
                        v_operation.attempt_count,false;
                    RETURN;
                END IF;

                UPDATE finance.provider_operations
                SET status='in_flight',
                    attempt_count=attempt_count+1,
                    lease_owner=p_lease_owner,
                    lease_until=
                        pg_catalog.clock_timestamp()+interval '30 seconds',
                    lease_fence=lease_fence+1,
                    last_error_code=NULL,
                    last_started_at=pg_catalog.clock_timestamp(),
                    updated_at=pg_catalog.clock_timestamp()
                WHERE id=v_operation.id
                RETURNING * INTO v_operation;

                RETURN QUERY SELECT
                    v_operation.id,v_operation.status::text,
                    v_operation.lease_fence,
                    v_operation.provider_object_id::text,
                    v_operation.attempt_count,true;
            END
            $function$
            """
        )
        op.execute(
            r"""
            CREATE FUNCTION app_secure.finish_finance_provider_operation(
                p_operation_id uuid,
                p_lease_owner uuid,
                p_lease_fence bigint,
                p_outcome text,
                p_provider_object_id text,
                p_error_code text,
                p_evidence_sha256 text
            )
            RETURNS TABLE(
                operation_id uuid,
                status text,
                provider_object_id text,
                attempt_count integer
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_org uuid;
                v_operation finance.provider_operations%ROWTYPE;
                v_payment finance.payments%ROWTYPE;
            BEGIN
                IF NOT pg_catalog.pg_has_role(
                    session_user,'app_runtime','MEMBER'
                ) THEN
                    RAISE EXCEPTION
                        'PAY-8 provider completion requires app_runtime'
                        USING ERRCODE='42501';
                END IF;
                BEGIN
                    v_org:=NULLIF(
                        pg_catalog.current_setting(
                            'app.current_org_id',true
                        ),''
                    )::uuid;
                EXCEPTION WHEN invalid_text_representation THEN
                    RAISE EXCEPTION
                        'PAY-8 provider completion tenant invalid'
                        USING ERRCODE='42501';
                END;
                IF v_org IS NULL
                   OR p_operation_id IS NULL
                   OR p_lease_owner IS NULL
                   OR p_lease_fence IS NULL
                   OR p_outcome NOT IN (
                       'succeeded','failed_retryable',
                       'failed_final','unknown'
                   )
                   OR (
                       p_error_code IS NOT NULL
                       AND p_error_code !~
                           '^[a-z][a-z0-9_]{0,79}$'
                   )
                   OR (
                       p_evidence_sha256 IS NOT NULL
                       AND p_evidence_sha256 !~ '^[0-9a-f]{64}$'
                   )
                THEN
                    RAISE EXCEPTION
                        'PAY-8 provider completion invalid'
                        USING ERRCODE='22023';
                END IF;

                SELECT o.* INTO v_operation
                FROM finance.provider_operations o
                WHERE o.id=p_operation_id
                  AND o.organization_id=v_org
                FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'PAY-8 provider operation unavailable'
                        USING ERRCODE='P0002';
                END IF;
                IF v_operation.status<>'in_flight'
                   OR v_operation.lease_owner
                      IS DISTINCT FROM p_lease_owner
                   OR v_operation.lease_fence
                      IS DISTINCT FROM p_lease_fence
                THEN
                    RAISE EXCEPTION
                        'PAY-8 provider operation fence conflict'
                        USING ERRCODE='40001';
                END IF;

                IF p_outcome='succeeded' THEN
                    IF p_provider_object_id IS NULL
                       OR p_provider_object_id !~
                          '^[A-Za-z0-9_:-]{1,200}$'
                       OR p_evidence_sha256 IS NULL
                    THEN
                        RAISE EXCEPTION
                            'PAY-8 provider success evidence invalid'
                            USING ERRCODE='22023';
                    END IF;
                    SELECT p.* INTO v_payment
                    FROM finance.payments p
                    WHERE p.id=v_operation.payment_id
                      AND p.organization_id=v_org
                    FOR UPDATE;
                    IF NOT FOUND
                       OR v_payment.provider_code
                          IS DISTINCT FROM v_operation.provider_code
                    THEN
                        RAISE EXCEPTION
                            'PAY-8 provider payment unavailable'
                            USING ERRCODE='23514';
                    END IF;
                    IF v_payment.provider_order_ref IS NOT NULL
                       AND v_payment.provider_order_ref NOT LIKE 'intent_%'
                       AND v_payment.provider_order_ref
                          IS DISTINCT FROM p_provider_object_id
                    THEN
                        RAISE EXCEPTION
                            'PAY-8 provider object replay conflict'
                            USING ERRCODE='23505';
                    END IF;
                    BEGIN
                        UPDATE finance.payments
                        SET provider_order_ref=p_provider_object_id,
                            updated_at=pg_catalog.clock_timestamp()
                        WHERE id=v_payment.id;
                    EXCEPTION WHEN unique_violation THEN
                        RAISE EXCEPTION
                            'PAY-8 provider object already bound'
                            USING ERRCODE='23505';
                    END;
                ELSIF p_outcome IN (
                    'failed_retryable','failed_final','unknown'
                ) THEN
                    IF p_error_code IS NULL THEN
                        RAISE EXCEPTION
                            'PAY-8 provider failure code required'
                            USING ERRCODE='22023';
                    END IF;
                END IF;

                UPDATE finance.provider_operations
                SET status=p_outcome,
                    provider_object_id=CASE
                        WHEN p_provider_object_id IS NOT NULL
                        THEN p_provider_object_id
                        ELSE provider_object_id
                    END,
                    provider_evidence_sha256=CASE
                        WHEN p_evidence_sha256 IS NOT NULL
                        THEN p_evidence_sha256
                        ELSE provider_evidence_sha256
                    END,
                    lease_owner=NULL,
                    lease_until=NULL,
                    last_error_code=CASE
                        WHEN p_outcome='succeeded' THEN NULL
                        ELSE p_error_code
                    END,
                    completed_at=CASE
                        WHEN p_outcome IN (
                            'succeeded','failed_final'
                        )
                        THEN pg_catalog.clock_timestamp()
                        ELSE NULL
                    END,
                    updated_at=pg_catalog.clock_timestamp()
                WHERE id=v_operation.id
                RETURNING * INTO v_operation;

                RETURN QUERY SELECT
                    v_operation.id,v_operation.status::text,
                    v_operation.provider_object_id::text,
                    v_operation.attempt_count;
            END
            $function$
            """
        )
        op.execute(
            r"""
            CREATE FUNCTION app_secure.reconcile_finance_provider_operation(
                p_operation_id uuid,
                p_outcome text,
                p_provider_object_id text,
                p_error_code text,
                p_evidence_sha256 text
            )
            RETURNS TABLE(
                operation_id uuid,
                status text,
                provider_object_id text
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_org uuid;
                v_operation finance.provider_operations%ROWTYPE;
                v_payment finance.payments%ROWTYPE;
            BEGIN
                IF NOT pg_catalog.pg_has_role(
                    session_user,'worker_runtime','MEMBER'
                ) THEN
                    RAISE EXCEPTION
                        'PAY-8 provider reconciliation requires worker_runtime'
                        USING ERRCODE='42501';
                END IF;
                BEGIN
                    v_org:=NULLIF(
                        pg_catalog.current_setting(
                            'app.current_org_id',true
                        ),''
                    )::uuid;
                EXCEPTION WHEN invalid_text_representation THEN
                    RAISE EXCEPTION
                        'PAY-8 reconciliation tenant invalid'
                        USING ERRCODE='42501';
                END;
                IF v_org IS NULL
                   OR p_operation_id IS NULL
                   OR p_outcome NOT IN ('succeeded','failed_final')
                   OR p_evidence_sha256 !~ '^[0-9a-f]{64}$'
                   OR (
                       p_error_code IS NOT NULL
                       AND p_error_code !~
                           '^[a-z][a-z0-9_]{0,79}$'
                   )
                THEN
                    RAISE EXCEPTION
                        'PAY-8 reconciliation evidence invalid'
                        USING ERRCODE='22023';
                END IF;

                SELECT o.* INTO v_operation
                FROM finance.provider_operations o
                WHERE o.id=p_operation_id
                  AND o.organization_id=v_org
                FOR UPDATE;
                IF NOT FOUND OR v_operation.status<>'unknown' THEN
                    RAISE EXCEPTION
                        'PAY-8 provider reconciliation state invalid'
                        USING ERRCODE='23514';
                END IF;

                IF p_outcome='succeeded' THEN
                    IF p_provider_object_id IS NULL
                       OR p_provider_object_id !~
                          '^[A-Za-z0-9_:-]{1,200}$'
                    THEN
                        RAISE EXCEPTION
                            'PAY-8 reconciliation provider object invalid'
                            USING ERRCODE='22023';
                    END IF;
                    SELECT p.* INTO v_payment
                    FROM finance.payments p
                    WHERE p.id=v_operation.payment_id
                      AND p.organization_id=v_org
                    FOR UPDATE;
                    IF NOT FOUND THEN
                        RAISE EXCEPTION
                            'PAY-8 reconciliation payment unavailable'
                            USING ERRCODE='23514';
                    END IF;
                    IF v_payment.provider_order_ref IS NOT NULL
                       AND v_payment.provider_order_ref NOT LIKE 'intent_%'
                       AND v_payment.provider_order_ref
                          IS DISTINCT FROM p_provider_object_id
                    THEN
                        RAISE EXCEPTION
                            'PAY-8 reconciliation object conflict'
                            USING ERRCODE='23505';
                    END IF;
                    BEGIN
                        UPDATE finance.payments
                        SET provider_order_ref=p_provider_object_id,
                            updated_at=pg_catalog.clock_timestamp()
                        WHERE id=v_payment.id;
                    EXCEPTION WHEN unique_violation THEN
                        RAISE EXCEPTION
                            'PAY-8 reconciliation object already bound'
                            USING ERRCODE='23505';
                    END;
                ELSIF p_error_code IS NULL THEN
                    RAISE EXCEPTION
                        'PAY-8 reconciliation final failure code required'
                        USING ERRCODE='22023';
                END IF;

                UPDATE finance.provider_operations
                SET status=p_outcome,
                    provider_object_id=CASE
                        WHEN p_provider_object_id IS NOT NULL
                        THEN p_provider_object_id
                        ELSE provider_object_id
                    END,
                    provider_evidence_sha256=p_evidence_sha256,
                    last_error_code=CASE
                        WHEN p_outcome='succeeded' THEN NULL
                        ELSE p_error_code
                    END,
                    completed_at=pg_catalog.clock_timestamp(),
                    updated_at=pg_catalog.clock_timestamp()
                WHERE id=v_operation.id
                RETURNING * INTO v_operation;

                RETURN QUERY SELECT
                    v_operation.id,v_operation.status::text,
                    v_operation.provider_object_id::text;
            END
            $function$
            """
        )
        op.execute(
            r"""
            CREATE FUNCTION app_secure.record_finance_provider_webhook(
                p_provider_code text,
                p_environment text,
                p_provider_event_id text,
                p_payload_sha256 text,
                p_signature_sha256 text,
                p_event_type text,
                p_provider_order_ref text,
                p_provider_payment_ref text,
                p_provider_amount_subunits bigint,
                p_provider_currency text,
                p_provider_payment_status text,
                p_provider_captured boolean,
                p_provider_payment_order_ref text,
                p_provider_order_entity_ref text,
                p_provider_order_status text,
                p_provider_event_timestamp bigint
            )
            RETURNS TABLE(
                inbox_id uuid,
                status text,
                processing_attempts integer,
                replayed boolean
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_row finance.provider_webhook_inbox%ROWTYPE;
                v_inserted boolean:=false;
            BEGIN
                IF NOT pg_catalog.pg_has_role(
                    session_user,'app_runtime','MEMBER'
                ) THEN
                    RAISE EXCEPTION
                        'PAY-8 webhook inbox requires app_runtime'
                        USING ERRCODE='42501';
                END IF;
                IF p_provider_code !~ '^[a-z0-9_]{1,40}$'
                   OR p_environment NOT IN ('sandbox','test')
                   OR p_provider_event_id !~
                      '^[A-Za-z0-9_-]{1,200}$'
                   OR p_payload_sha256 !~ '^[0-9a-f]{64}$'
                   OR p_signature_sha256 !~ '^[0-9a-f]{64}$'
                   OR p_event_type NOT IN (
                       'payment.authorized','payment.captured',
                       'payment.failed','order.paid'
                   )
                   OR p_provider_order_ref !~
                      '^[A-Za-z0-9_:-]{1,200}$'
                   OR p_provider_payment_ref !~
                      '^[A-Za-z0-9_:-]{1,200}$'
                   OR p_provider_amount_subunits IS NULL
                   OR p_provider_amount_subunits<0
                   OR p_provider_currency !~ '^[A-Z]{3}$'
                   OR p_provider_payment_status IS NULL
                   OR char_length(p_provider_payment_status)
                      NOT BETWEEN 1 AND 80
                   OR p_provider_captured IS NULL
                   OR p_provider_payment_order_ref !~
                      '^[A-Za-z0-9_:-]{1,200}$'
                   OR (
                       p_provider_order_entity_ref IS NOT NULL
                       AND p_provider_order_entity_ref !~
                           '^[A-Za-z0-9_:-]{1,200}$'
                   )
                   OR (
                       p_provider_event_timestamp IS NOT NULL
                       AND p_provider_event_timestamp<0
                   )
                THEN
                    RAISE EXCEPTION
                        'PAY-8 webhook normalized evidence invalid'
                        USING ERRCODE='22023';
                END IF;

                INSERT INTO finance.provider_webhook_inbox(
                    provider_code,environment,provider_event_id,
                    payload_sha256,signature_sha256,event_type,
                    provider_order_ref,provider_payment_ref,
                    provider_amount_subunits,provider_currency,
                    provider_payment_status,provider_captured,
                    provider_payment_order_ref,provider_order_entity_ref,
                    provider_order_status,provider_event_timestamp
                ) VALUES (
                    p_provider_code,p_environment,p_provider_event_id,
                    p_payload_sha256,p_signature_sha256,p_event_type,
                    p_provider_order_ref,p_provider_payment_ref,
                    p_provider_amount_subunits,p_provider_currency,
                    p_provider_payment_status,p_provider_captured,
                    p_provider_payment_order_ref,p_provider_order_entity_ref,
                    p_provider_order_status,p_provider_event_timestamp
                )
                ON CONFLICT DO NOTHING
                RETURNING * INTO v_row;
                v_inserted:=FOUND;

                IF NOT v_inserted THEN
                    SELECT w.* INTO v_row
                    FROM finance.provider_webhook_inbox w
                    WHERE w.provider_code=p_provider_code
                      AND w.environment=p_environment
                      AND w.provider_event_id=p_provider_event_id
                    FOR UPDATE;
                    IF NOT FOUND
                       OR v_row.payload_sha256::text
                          IS DISTINCT FROM p_payload_sha256
                       OR v_row.signature_sha256::text
                          IS DISTINCT FROM p_signature_sha256
                       OR v_row.event_type
                          IS DISTINCT FROM p_event_type
                       OR v_row.provider_order_ref
                          IS DISTINCT FROM p_provider_order_ref
                       OR v_row.provider_payment_ref
                          IS DISTINCT FROM p_provider_payment_ref
                       OR v_row.provider_amount_subunits
                          IS DISTINCT FROM p_provider_amount_subunits
                       OR v_row.provider_currency::text
                          IS DISTINCT FROM p_provider_currency
                       OR v_row.provider_payment_status
                          IS DISTINCT FROM p_provider_payment_status
                       OR v_row.provider_captured
                          IS DISTINCT FROM p_provider_captured
                       OR v_row.provider_payment_order_ref
                          IS DISTINCT FROM p_provider_payment_order_ref
                       OR v_row.provider_order_entity_ref
                          IS DISTINCT FROM p_provider_order_entity_ref
                       OR v_row.provider_order_status
                          IS DISTINCT FROM p_provider_order_status
                       OR v_row.provider_event_timestamp
                          IS DISTINCT FROM p_provider_event_timestamp
                    THEN
                        RAISE EXCEPTION
                            'PAY-8 webhook event replay conflict'
                            USING ERRCODE='23505';
                    END IF;
                END IF;

                RETURN QUERY SELECT
                    v_row.id,v_row.status::text,
                    v_row.processing_attempts,NOT v_inserted;
            END
            $function$
            """
        )
        op.execute(
            r"""
            CREATE FUNCTION app_secure.claim_finance_provider_webhook(
                p_inbox_id uuid,
                p_lease_owner uuid
            )
            RETURNS TABLE(
                inbox_id uuid,
                status text,
                lease_fence bigint,
                processing_attempts integer,
                claimed boolean,
                provider_code text,
                environment text,
                provider_event_id text,
                payload_sha256 text,
                event_type text,
                provider_order_ref text,
                provider_payment_ref text,
                provider_amount_subunits bigint,
                provider_currency text,
                provider_payment_status text,
                provider_captured boolean,
                provider_payment_order_ref text,
                provider_order_entity_ref text,
                provider_order_status text,
                provider_event_timestamp bigint
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_row finance.provider_webhook_inbox%ROWTYPE;
            BEGIN
                IF NOT (
                    pg_catalog.pg_has_role(
                        session_user,'app_runtime','MEMBER'
                    )
                    OR pg_catalog.pg_has_role(
                        session_user,'worker_runtime','MEMBER'
                    )
                ) THEN
                    RAISE EXCEPTION
                        'PAY-8 webhook claim requires app/worker runtime'
                        USING ERRCODE='42501';
                END IF;
                IF p_inbox_id IS NULL OR p_lease_owner IS NULL THEN
                    RAISE EXCEPTION
                        'PAY-8 webhook claim identity invalid'
                        USING ERRCODE='22023';
                END IF;
                SELECT w.* INTO v_row
                FROM finance.provider_webhook_inbox w
                WHERE w.id=p_inbox_id
                FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'PAY-8 webhook inbox unavailable'
                        USING ERRCODE='P0002';
                END IF;

                IF v_row.status='processing'
                   AND v_row.lease_until<=
                       pg_catalog.clock_timestamp()
                THEN
                    UPDATE finance.provider_webhook_inbox
                    SET status='retry',
                        lease_owner=NULL,
                        lease_until=NULL,
                        last_error_code='lease_expired_retry',
                        updated_at=pg_catalog.clock_timestamp()
                    WHERE id=v_row.id
                    RETURNING * INTO v_row;
                END IF;

                IF v_row.status NOT IN ('received','retry') THEN
                    RETURN QUERY SELECT
                        v_row.id,v_row.status::text,
                        v_row.lease_fence,
                        v_row.processing_attempts,false,
                        v_row.provider_code::text,
                        v_row.environment::text,
                        v_row.provider_event_id::text,
                        v_row.payload_sha256::text,
                        v_row.event_type::text,
                        v_row.provider_order_ref::text,
                        v_row.provider_payment_ref::text,
                        v_row.provider_amount_subunits,
                        v_row.provider_currency::text,
                        v_row.provider_payment_status::text,
                        v_row.provider_captured,
                        v_row.provider_payment_order_ref::text,
                        v_row.provider_order_entity_ref::text,
                        v_row.provider_order_status::text,
                        v_row.provider_event_timestamp;
                    RETURN;
                END IF;

                IF v_row.status='retry'
                   AND v_row.processing_attempts>=v_row.max_attempts
                THEN
                    UPDATE finance.provider_webhook_inbox
                    SET status='dead_letter',
                        lease_owner=NULL,
                        lease_until=NULL,
                        last_error_code='retry_exhausted',
                        updated_at=pg_catalog.clock_timestamp()
                    WHERE id=v_row.id
                    RETURNING * INTO v_row;
                    RETURN QUERY SELECT
                        v_row.id,v_row.status::text,
                        v_row.lease_fence,
                        v_row.processing_attempts,false,
                        v_row.provider_code::text,
                        v_row.environment::text,
                        v_row.provider_event_id::text,
                        v_row.payload_sha256::text,
                        v_row.event_type::text,
                        v_row.provider_order_ref::text,
                        v_row.provider_payment_ref::text,
                        v_row.provider_amount_subunits,
                        v_row.provider_currency::text,
                        v_row.provider_payment_status::text,
                        v_row.provider_captured,
                        v_row.provider_payment_order_ref::text,
                        v_row.provider_order_entity_ref::text,
                        v_row.provider_order_status::text,
                        v_row.provider_event_timestamp;
                    RETURN;
                END IF;

                UPDATE finance.provider_webhook_inbox
                SET status='processing',
                    processing_attempts=processing_attempts+1,
                    lease_owner=p_lease_owner,
                    lease_until=
                        pg_catalog.clock_timestamp()+interval '30 seconds',
                    lease_fence=lease_fence+1,
                    last_error_code=NULL,
                    updated_at=pg_catalog.clock_timestamp()
                WHERE id=v_row.id
                RETURNING * INTO v_row;

                RETURN QUERY SELECT
                    v_row.id,v_row.status::text,
                    v_row.lease_fence,
                    v_row.processing_attempts,true,
                    v_row.provider_code::text,
                    v_row.environment::text,
                    v_row.provider_event_id::text,
                    v_row.payload_sha256::text,
                    v_row.event_type::text,
                    v_row.provider_order_ref::text,
                    v_row.provider_payment_ref::text,
                    v_row.provider_amount_subunits,
                    v_row.provider_currency::text,
                    v_row.provider_payment_status::text,
                    v_row.provider_captured,
                    v_row.provider_payment_order_ref::text,
                    v_row.provider_order_entity_ref::text,
                    v_row.provider_order_status::text,
                    v_row.provider_event_timestamp;
            END
            $function$
            """
        )
        op.execute(
            r"""
            CREATE FUNCTION app_secure.claim_next_finance_provider_webhook(
                p_lease_owner uuid
            )
            RETURNS TABLE(
                inbox_id uuid,
                status text,
                lease_fence bigint,
                processing_attempts integer,
                claimed boolean,
                provider_code text,
                environment text,
                provider_event_id text,
                payload_sha256 text,
                event_type text,
                provider_order_ref text,
                provider_payment_ref text,
                provider_amount_subunits bigint,
                provider_currency text,
                provider_payment_status text,
                provider_captured boolean,
                provider_payment_order_ref text,
                provider_order_entity_ref text,
                provider_order_status text,
                provider_event_timestamp bigint
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_inbox_id uuid;
            BEGIN
                IF NOT pg_catalog.pg_has_role(
                    session_user,'worker_runtime','MEMBER'
                ) THEN
                    RAISE EXCEPTION
                        'PAY-8 webhook recovery requires worker_runtime'
                        USING ERRCODE='42501';
                END IF;
                IF p_lease_owner IS NULL THEN
                    RAISE EXCEPTION
                        'PAY-8 webhook recovery lease owner required'
                        USING ERRCODE='22023';
                END IF;

                SELECT w.id INTO v_inbox_id
                FROM finance.provider_webhook_inbox w
                WHERE w.status IN ('received','retry')
                   OR (
                       w.status='processing'
                       AND w.lease_until<=pg_catalog.clock_timestamp()
                   )
                ORDER BY w.received_at,w.id
                FOR UPDATE SKIP LOCKED
                LIMIT 1;

                IF v_inbox_id IS NULL THEN
                    RETURN;
                END IF;

                RETURN QUERY
                SELECT *
                FROM app_secure.claim_finance_provider_webhook(
                    v_inbox_id,p_lease_owner
                );
            END
            $function$
            """
        )
        op.execute(
            r"""
            CREATE FUNCTION app_secure.complete_finance_provider_webhook(
                p_inbox_id uuid,
                p_lease_owner uuid,
                p_lease_fence bigint,
                p_payment_event_id uuid
            )
            RETURNS TABLE(
                inbox_id uuid,
                status text,
                payment_event_id uuid,
                payment_id uuid,
                organization_id uuid
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_row finance.provider_webhook_inbox%ROWTYPE;
                v_event finance.payment_events%ROWTYPE;
                v_payment finance.payments%ROWTYPE;
            BEGIN
                IF NOT (
                    pg_catalog.pg_has_role(
                        session_user,'app_runtime','MEMBER'
                    )
                    OR pg_catalog.pg_has_role(
                        session_user,'worker_runtime','MEMBER'
                    )
                ) THEN
                    RAISE EXCEPTION
                        'PAY-8 webhook completion requires app/worker runtime'
                        USING ERRCODE='42501';
                END IF;
                SELECT w.* INTO v_row
                FROM finance.provider_webhook_inbox w
                WHERE w.id=p_inbox_id
                FOR UPDATE;
                IF NOT FOUND
                   OR v_row.status<>'processing'
                   OR v_row.lease_owner
                      IS DISTINCT FROM p_lease_owner
                   OR v_row.lease_fence
                      IS DISTINCT FROM p_lease_fence
                THEN
                    RAISE EXCEPTION
                        'PAY-8 webhook completion fence conflict'
                        USING ERRCODE='40001';
                END IF;

                SELECT e.* INTO v_event
                FROM finance.payment_events e
                WHERE e.id=p_payment_event_id;
                IF NOT FOUND
                   OR v_event.provider_code
                      IS DISTINCT FROM v_row.provider_code
                   OR v_event.provider_event_id
                      IS DISTINCT FROM v_row.provider_event_id
                   OR v_event.payment_id IS NULL
                THEN
                    RAISE EXCEPTION
                        'PAY-8 webhook payment event mismatch'
                        USING ERRCODE='23514';
                END IF;
                SELECT p.* INTO v_payment
                FROM finance.payments p
                WHERE p.id=v_event.payment_id;
                IF NOT FOUND OR v_payment.organization_id IS NULL THEN
                    RAISE EXCEPTION
                        'PAY-8 webhook payment unavailable'
                        USING ERRCODE='23514';
                END IF;

                UPDATE finance.provider_webhook_inbox
                SET organization_id=v_payment.organization_id,
                    payment_id=v_payment.id,
                    payment_event_id=v_event.id,
                    status='processed',
                    lease_owner=NULL,
                    lease_until=NULL,
                    last_error_code=NULL,
                    processed_at=pg_catalog.clock_timestamp(),
                    updated_at=pg_catalog.clock_timestamp()
                WHERE id=v_row.id
                RETURNING * INTO v_row;

                RETURN QUERY SELECT
                    v_row.id,v_row.status::text,
                    v_row.payment_event_id,v_row.payment_id,
                    v_row.organization_id;
            END
            $function$
            """
        )
        op.execute(
            r"""
            CREATE FUNCTION app_secure.fail_finance_provider_webhook(
                p_inbox_id uuid,
                p_lease_owner uuid,
                p_lease_fence bigint,
                p_error_code text,
                p_retryable boolean
            )
            RETURNS TABLE(
                inbox_id uuid,
                status text,
                processing_attempts integer
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_row finance.provider_webhook_inbox%ROWTYPE;
            BEGIN
                IF NOT (
                    pg_catalog.pg_has_role(
                        session_user,'app_runtime','MEMBER'
                    )
                    OR pg_catalog.pg_has_role(
                        session_user,'worker_runtime','MEMBER'
                    )
                ) THEN
                    RAISE EXCEPTION
                        'PAY-8 webhook failure requires app/worker runtime'
                        USING ERRCODE='42501';
                END IF;
                IF p_error_code !~ '^[a-z][a-z0-9_]{0,79}$'
                   OR p_retryable IS NULL
                THEN
                    RAISE EXCEPTION
                        'PAY-8 webhook failure evidence invalid'
                        USING ERRCODE='22023';
                END IF;
                SELECT w.* INTO v_row
                FROM finance.provider_webhook_inbox w
                WHERE w.id=p_inbox_id
                FOR UPDATE;
                IF NOT FOUND
                   OR v_row.status<>'processing'
                   OR v_row.lease_owner
                      IS DISTINCT FROM p_lease_owner
                   OR v_row.lease_fence
                      IS DISTINCT FROM p_lease_fence
                THEN
                    RAISE EXCEPTION
                        'PAY-8 webhook failure fence conflict'
                        USING ERRCODE='40001';
                END IF;

                UPDATE finance.provider_webhook_inbox
                SET status=CASE
                        WHEN p_retryable
                             AND processing_attempts<max_attempts
                        THEN 'retry'
                        ELSE 'dead_letter'
                    END,
                    lease_owner=NULL,
                    lease_until=NULL,
                    last_error_code=p_error_code,
                    updated_at=pg_catalog.clock_timestamp()
                WHERE id=v_row.id
                RETURNING * INTO v_row;

                RETURN QUERY SELECT
                    v_row.id,v_row.status::text,
                    v_row.processing_attempts;
            END
            $function$
            """
        )

        for signature in (
            _RESERVE_OPERATION,_CLAIM_OPERATION,_FINISH_OPERATION,
            _RECONCILE_OPERATION,_RECORD_WEBHOOK,_CLAIM_WEBHOOK,
            _CLAIM_NEXT_WEBHOOK,_COMPLETE_WEBHOOK,_FAIL_WEBHOOK,
        ):
            op.execute(
                f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC"
            )
        for signature in (
            _RESERVE_OPERATION,_CLAIM_OPERATION,_FINISH_OPERATION,
            _RECORD_WEBHOOK,_CLAIM_WEBHOOK,_COMPLETE_WEBHOOK,
            _FAIL_WEBHOOK,
        ):
            op.execute(
                f"GRANT EXECUTE ON FUNCTION {signature} TO app_runtime"
            )
        op.execute(
            f"GRANT EXECUTE ON FUNCTION {_RECONCILE_OPERATION} "
            "TO worker_runtime"
        )
        for signature in (
            _CLAIM_WEBHOOK,_CLAIM_NEXT_WEBHOOK,
            _COMPLETE_WEBHOOK,_FAIL_WEBHOOK,
        ):
            op.execute(
                f"GRANT EXECUTE ON FUNCTION {signature} TO worker_runtime"
            )
    finally:
        op.execute("RESET ROLE")

    if not had_create:
        bind.execute(
            sa.text(
                "REVOKE CREATE ON SCHEMA app_secure FROM app_security_owner"
            )
        )


def _assert_runtime_denials(bind) -> None:
    for role in (_APP, _WORKER):
        leaked = bind.execute(
            sa.text(
                """
                SELECT
                    pg_catalog.has_table_privilege(
                        :role,'finance.provider_operations','SELECT'
                    )
                    OR pg_catalog.has_table_privilege(
                        :role,'finance.provider_operations','INSERT'
                    )
                    OR pg_catalog.has_table_privilege(
                        :role,'finance.provider_operations','UPDATE'
                    )
                    OR pg_catalog.has_table_privilege(
                        :role,'finance.provider_webhook_inbox','SELECT'
                    )
                    OR pg_catalog.has_table_privilege(
                        :role,'finance.provider_webhook_inbox','INSERT'
                    )
                    OR pg_catalog.has_table_privilege(
                        :role,'finance.provider_webhook_inbox','UPDATE'
                    )
                """
            ),
            {"role": role},
        ).scalar_one()
        if leaked:
            raise RuntimeError(
                f"PAY-8 leaked direct durable provider DML to {role}"
            )


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _require_identity(bind)
    _require_predecessor(bind)
    _install_tables()
    _install_functions(bind)
    _assert_runtime_denials(bind)


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _require_identity(bind)

    evidence = bind.execute(
        sa.text(
            """
            SELECT
                (SELECT pg_catalog.count(*)
                 FROM finance.provider_operations)
                +
                (SELECT pg_catalog.count(*)
                 FROM finance.provider_webhook_inbox)
            """
        )
    ).scalar_one()
    if int(evidence) != 0:
        raise RuntimeError(
            "PAY-8 downgrade blocked: durable provider evidence exists"
        )

    for signature in (
        _RECONCILE_OPERATION,_CLAIM_WEBHOOK,_CLAIM_NEXT_WEBHOOK,
        _COMPLETE_WEBHOOK,_FAIL_WEBHOOK,
    ):
        op.execute(
            f"REVOKE EXECUTE ON FUNCTION {signature} FROM worker_runtime"
        )
    for signature in (
        _RESERVE_OPERATION,_CLAIM_OPERATION,_FINISH_OPERATION,
        _RECORD_WEBHOOK,_CLAIM_WEBHOOK,_COMPLETE_WEBHOOK,
        _FAIL_WEBHOOK,
    ):
        op.execute(
            f"REVOKE EXECUTE ON FUNCTION {signature} FROM app_runtime"
        )

    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        for signature in (
            _FAIL_WEBHOOK,_COMPLETE_WEBHOOK,_CLAIM_NEXT_WEBHOOK,
            _CLAIM_WEBHOOK,_RECORD_WEBHOOK,_RECONCILE_OPERATION,_FINISH_OPERATION,
            _CLAIM_OPERATION,_RESERVE_OPERATION,
        ):
            op.execute(f"DROP FUNCTION {signature}")
    finally:
        op.execute("RESET ROLE")

    op.execute("DROP TABLE finance.provider_webhook_inbox")
    op.execute("DROP TABLE finance.provider_operations")
    _require_predecessor(bind)
