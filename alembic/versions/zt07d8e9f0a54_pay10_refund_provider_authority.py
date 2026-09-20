"""PAY-10 refund provider authority and evidence boundary.

Revision ID: zt07d8e9f0a54
Revises: zs07d8e9f0a53
Create Date: 2026-09-20

This first PAY-10 slice adds durable provider-evidence and credit-note
provenance without enabling live provider money movement.  Runtime identities
remain table-blind; later PAY-10 slices expose only bounded app_secure
capabilities over this schema.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "zt07d8e9f0a54"
down_revision = "zs07d8e9f0a53"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_RUNTIME_ROLES = (
    "app_runtime",
    "worker_runtime",
    "finance_runtime",
    "finance_payment_runtime",
    "finance_refund_runtime",
    "finance_reconciliation_runtime",
    "finance_read_runtime",
    "finance_maintenance_runtime",
)
_NEW_TABLES = (
    "credit_note_series",
    "refund_credit_note_links",
    "refund_provider_evidence",
)
_EVIDENCE_TRIGGER = "pay10_immutable_refund_provider_evidence"
_REFUND_RUNTIME = "finance_refund_runtime"
_RECON_RUNTIME = "finance_reconciliation_runtime"
_CLAIM_REFUND = (
    "app_secure.claim_pay10_refund_provider_execution(uuid,integer)"
)
_BIND_REQUEST = (
    "app_secure.bind_pay10_refund_provider_request("
    "uuid,uuid,bigint,text,text,numeric,text,text)"
)
_RECORD_OUTCOME = (
    "app_secure.record_pay10_refund_provider_outcome("
    "uuid,uuid,bigint,text,text,text,timestamp with time zone)"
)
_RECORD_UNKNOWN = (
    "app_secure.record_pay10_refund_provider_unknown("
    "uuid,uuid,bigint,text)"
)
_RECORD_FAILURE = (
    "app_secure.record_pay10_refund_provider_failure("
    "uuid,uuid,bigint,text,boolean)"
)
_RECORD_EXTERNAL = (
    "app_secure.record_pay10_refund_external_evidence("
    "uuid,text,text,text,text,text,text,timestamp with time zone)"
)
_PAY10_FUNCTIONS = (
    _CLAIM_REFUND,
    _BIND_REQUEST,
    _RECORD_OUTCOME,
    _RECORD_UNKNOWN,
    _RECORD_FAILURE,
    _RECORD_EXTERNAL,
)


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
        raise RuntimeError(f"PAY-10 missing externally managed role: {role_name}")
    if bool(row["rolcanlogin"]) is not login:
        raise RuntimeError(f"PAY-10 login posture drift: {role_name}")
    for key in (
        "rolsuper",
        "rolinherit",
        "rolcreatedb",
        "rolcreaterole",
        "rolreplication",
        "rolbypassrls",
    ):
        if bool(row[key]):
            raise RuntimeError(
                f"PAY-10 reduced-role contract drift: {role_name}.{key}"
            )


def _relation_exists(bind, relation: str) -> bool:
    return bool(
        bind.execute(
            sa.text("SELECT pg_catalog.to_regclass(:relation) IS NOT NULL"),
            {"relation": relation},
        ).scalar_one()
    )


def _column_exists(bind, relation: str, column_name: str) -> bool:
    return bool(
        bind.execute(
            sa.text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_attribute
                    WHERE attrelid=pg_catalog.to_regclass(:relation)
                      AND attname=:column_name
                      AND attnum>0
                      AND NOT attisdropped
                )
                """
            ),
            {"relation": relation, "column_name": column_name},
        ).scalar_one()
    )


def _constraint_exists(bind, schema_name: str, table_name: str, name: str) -> bool:
    return bool(
        bind.execute(
            sa.text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_constraint c
                    JOIN pg_catalog.pg_class t ON t.oid=c.conrelid
                    JOIN pg_catalog.pg_namespace n ON n.oid=t.relnamespace
                    WHERE n.nspname=:schema_name
                      AND t.relname=:table_name
                      AND c.conname=:name
                )
                """
            ),
            {
                "schema_name": schema_name,
                "table_name": table_name,
                "name": name,
            },
        ).scalar_one()
    )


def _require_predecessor(bind) -> None:
    _require_reduced_role(bind, _MIGRATION_OWNER, login=True)
    _require_reduced_role(bind, _SECURITY_OWNER)
    for role_name in _RUNTIME_ROLES:
        _require_reduced_role(bind, role_name)

    identity = bind.execute(
        sa.text("SELECT session_user::text,current_user::text")
    ).one()
    if tuple(identity) != (_MIGRATION_OWNER, _MIGRATION_OWNER):
        raise RuntimeError(
            "PAY-10 migration requires session_user=current_user=migration_owner"
        )

    if not bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.pg_has_role("
                ":member,:target,'SET')"
            ),
            {"member": _MIGRATION_OWNER, "target": _SECURITY_OWNER},
        ).scalar_one()
    ):
        raise RuntimeError(
            "PAY-10 requires bounded migration_owner SET edge to app_security_owner"
        )

    if bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.has_schema_privilege("
                ":role,'app_secure','USAGE')"
            ),
            {"role": _MIGRATION_OWNER},
        ).scalar_one()
    ):
        raise RuntimeError(
            "PAY-10 migration_owner must remain blind to app_secure at runtime"
        )

    for relation in (
        "finance.refunds",
        "finance.refund_execution_commands",
        "finance.payments",
        "finance.payment_allocations",
        "finance.credit_notes",
        "finance.ledger_entries",
        "finance.ledger_entry_lines",
        "finance.outbox_events",
    ):
        if not _relation_exists(bind, relation):
            raise RuntimeError(f"PAY-10 missing predecessor relation: {relation}")

    for table_name in _NEW_TABLES:
        if _relation_exists(bind, f"finance.{table_name}"):
            raise RuntimeError(
                f"PAY-10 predecessor unexpectedly has finance.{table_name}"
            )

    for column_name in (
        "request_sha256",
        "first_attempted_at",
        "provider_accepted_at",
        "completed_at",
    ):
        if _column_exists(
            bind, "finance.refund_execution_commands", column_name
        ):
            raise RuntimeError(
                "PAY-10 predecessor unexpectedly has refund command column "
                f"{column_name}"
            )

    for schema_name, table_name, constraint_name in (
        (
            "finance",
            "refunds",
            "uq_pay10_refunds_id_org",
        ),
        (
            "finance",
            "credit_notes",
            "uq_pay10_credit_notes_id_org",
        ),
    ):
        if _constraint_exists(
            bind, schema_name, table_name, constraint_name
        ):
            raise RuntimeError(
                f"PAY-10 predecessor unexpectedly has {constraint_name}"
            )

    inherited_command_acl = bind.execute(
        sa.text(
            """
            SELECT
                pg_catalog.has_table_privilege(
                    'app_security_owner',
                    'finance.refund_execution_commands',
                    'SELECT'
                )
                AND pg_catalog.has_table_privilege(
                    'app_security_owner',
                    'finance.refund_execution_commands',
                    'INSERT'
                )
                AND pg_catalog.has_table_privilege(
                    'app_security_owner',
                    'finance.refund_execution_commands',
                    'UPDATE'
                )
            """
        )
    ).scalar_one()
    if not bool(inherited_command_acl):
        raise RuntimeError(
            "PAY-10 requires inherited P4D refund command owner authority"
        )

    owner_finance_acl = bind.execute(
        sa.text(
            """
            SELECT
                pg_catalog.has_table_privilege(
                    'app_security_owner','finance.payments','SELECT'
                )
                AND pg_catalog.has_table_privilege(
                    'app_security_owner','finance.refunds','SELECT'
                )
                AND pg_catalog.has_table_privilege(
                    'app_security_owner','finance.refunds','UPDATE'
                )
            """
        )
    ).scalar_one()
    if not bool(owner_finance_acl):
        raise RuntimeError(
            "PAY-10-C requires inherited payment/refund owner authority"
        )

    if bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.has_schema_privilege("
                ":role,'app_secure','USAGE')"
            ),
            {"role": _REFUND_RUNTIME},
        ).scalar_one()
    ):
        raise RuntimeError(
            "PAY-10-C refuses preexisting app_secure USAGE for "
            "finance_refund_runtime"
        )
    if not bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.has_schema_privilege("
                ":role,'app_secure','USAGE')"
            ),
            {"role": _RECON_RUNTIME},
        ).scalar_one()
    ):
        raise RuntimeError(
            "PAY-10-C requires inherited PAY-8 app_secure USAGE for "
            "finance_reconciliation_runtime"
        )
    if bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.has_schema_privilege("
                "'app_security_owner','app_secure','CREATE')"
            )
        ).scalar_one()
    ):
        raise RuntimeError(
            "PAY-10-C refuses preexisting app_security_owner CREATE on app_secure"
        )

    for signature in _PAY10_FUNCTIONS:
        if bind.execute(
            sa.text(
                "SELECT pg_catalog.to_regprocedure(:signature) IS NOT NULL"
            ),
            {"signature": signature},
        ).scalar_one():
            raise RuntimeError(
                f"PAY-10-C predecessor unexpectedly has {signature}"
            )


def _install() -> None:
    op.execute(
        """
        ALTER TABLE finance.refunds
        ADD CONSTRAINT uq_pay10_refunds_id_org
        UNIQUE (id, organization_id)
        """
    )
    op.execute(
        """
        ALTER TABLE finance.credit_notes
        ADD CONSTRAINT uq_pay10_credit_notes_id_org
        UNIQUE (id, organization_id)
        """
    )

    op.execute(
        """
        ALTER TABLE finance.refund_execution_commands
        ADD COLUMN request_sha256 CHAR(64) NULL,
        ADD COLUMN first_attempted_at TIMESTAMPTZ NULL,
        ADD COLUMN provider_accepted_at TIMESTAMPTZ NULL,
        ADD COLUMN completed_at TIMESTAMPTZ NULL,
        ADD CONSTRAINT chk_pay10_refund_command_request_hash
            CHECK (
                request_sha256 IS NULL
                OR request_sha256 ~ '^[0-9a-f]{64}$'
            ),
        ADD CONSTRAINT chk_pay10_refund_command_attempt_timestamps
            CHECK (
                provider_accepted_at IS NULL
                OR first_attempted_at IS NOT NULL
            ),
        ADD CONSTRAINT chk_pay10_refund_command_completion_timestamps
            CHECK (
                completed_at IS NULL
                OR provider_accepted_at IS NOT NULL
            )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_pay10_refund_command_provider_ref
        ON finance.refund_execution_commands(
            provider_code,
            provider_refund_ref
        )
        WHERE provider_code IS NOT NULL
          AND provider_refund_ref IS NOT NULL
        """
    )

    op.execute(
        """
        CREATE TABLE finance.credit_note_series (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            legal_entity_id UUID NOT NULL
                REFERENCES finance.legal_entities(id) ON DELETE RESTRICT,
            gst_registration_id UUID NOT NULL
                REFERENCES finance.gst_registrations(id) ON DELETE RESTRICT,
            financial_year CHAR(4) NOT NULL,
            series_code VARCHAR(20) NOT NULL,
            prefix VARCHAR(12) NOT NULL,
            last_number BIGINT NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT uq_pay10_credit_note_series_scope
                UNIQUE (
                    legal_entity_id,
                    gst_registration_id,
                    financial_year,
                    series_code
                ),
            CONSTRAINT chk_pay10_credit_note_series_year
                CHECK (financial_year ~ '^[0-9]{4}$'),
            CONSTRAINT chk_pay10_credit_note_series_code
                CHECK (series_code ~ '^[A-Z0-9_-]+$'),
            CONSTRAINT chk_pay10_credit_note_series_prefix
                CHECK (
                    prefix ~ '^[A-Z0-9/-]+$'
                    AND char_length(prefix) BETWEEN 1 AND 12
                ),
            CONSTRAINT chk_pay10_credit_note_series_last_number
                CHECK (last_number >= 0),
            CONSTRAINT chk_pay10_credit_note_series_status
                CHECK (status IN ('active','inactive'))
        )
        """
    )

    op.execute(
        """
        CREATE TABLE finance.refund_credit_note_links (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NOT NULL
                REFERENCES public.organizations(id) ON DELETE RESTRICT,
            refund_id UUID NOT NULL,
            credit_note_id UUID NOT NULL,
            amount NUMERIC(14,2) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT fk_pay10_refund_credit_link_refund_org
                FOREIGN KEY (refund_id, organization_id)
                REFERENCES finance.refunds(id, organization_id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_pay10_refund_credit_link_credit_org
                FOREIGN KEY (credit_note_id, organization_id)
                REFERENCES finance.credit_notes(id, organization_id)
                ON DELETE RESTRICT,
            CONSTRAINT uq_pay10_refund_credit_link
                UNIQUE (refund_id, credit_note_id),
            CONSTRAINT uq_pay10_refund_credit_note_single_refund
                UNIQUE (credit_note_id),
            CONSTRAINT chk_pay10_refund_credit_link_amount
                CHECK (amount > 0)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE finance.refund_provider_evidence (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            command_id UUID NOT NULL
                REFERENCES finance.refund_execution_commands(command_id)
                ON DELETE RESTRICT,
            refund_id UUID NOT NULL
                REFERENCES finance.refunds(id) ON DELETE RESTRICT,
            payment_id UUID NOT NULL
                REFERENCES finance.payments(id) ON DELETE RESTRICT,
            organization_id UUID NOT NULL
                REFERENCES public.organizations(id) ON DELETE RESTRICT,
            provider_code VARCHAR(40) NOT NULL,
            provider_event_id VARCHAR(200) NULL,
            provider_refund_ref VARCHAR(200) NULL,
            evidence_source VARCHAR(24) NOT NULL,
            normalized_status VARCHAR(24) NOT NULL,
            request_sha256 CHAR(64) NOT NULL,
            evidence_sha256 CHAR(64) NOT NULL,
            occurred_at TIMESTAMPTZ NULL,
            recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT chk_pay10_refund_evidence_provider
                CHECK (provider_code ~ '^[a-z0-9_]+$'),
            CONSTRAINT chk_pay10_refund_evidence_source
                CHECK (
                    evidence_source IN (
                        'submission',
                        'webhook',
                        'reconciliation'
                    )
                ),
            CONSTRAINT chk_pay10_refund_evidence_status
                CHECK (
                    normalized_status IN (
                        'pending',
                        'processed',
                        'failed'
                    )
                ),
            CONSTRAINT chk_pay10_refund_evidence_hashes
                CHECK (
                    request_sha256 ~ '^[0-9a-f]{64}$'
                    AND evidence_sha256 ~ '^[0-9a-f]{64}$'
                ),
            CONSTRAINT chk_pay10_refund_evidence_reference
                CHECK (
                    normalized_status = 'failed'
                    OR provider_refund_ref IS NOT NULL
                ),
            CONSTRAINT chk_pay10_refund_evidence_event_source
                CHECK (
                    evidence_source <> 'webhook'
                    OR provider_event_id IS NOT NULL
                ),
            CONSTRAINT uq_pay10_refund_evidence_hash
                UNIQUE (command_id, evidence_sha256)
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_pay10_refund_provider_event
        ON finance.refund_provider_evidence(
            provider_code,
            provider_event_id
        )
        WHERE provider_event_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE INDEX ix_pay10_refund_provider_reference
        ON finance.refund_provider_evidence(
            provider_code,
            provider_refund_ref,
            recorded_at,
            id
        )
        WHERE provider_refund_ref IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE INDEX ix_pay10_refund_credit_note_refund
        ON finance.refund_credit_note_links(
            organization_id,
            refund_id,
            created_at,
            id
        )
        """
    )

    for table_name in _NEW_TABLES:
        op.execute(
            f"ALTER TABLE finance.{table_name} ENABLE ROW LEVEL SECURITY"
        )
        op.execute(
            f"ALTER TABLE finance.{table_name} FORCE ROW LEVEL SECURITY"
        )
        op.execute(
            f"REVOKE ALL ON TABLE finance.{table_name} FROM PUBLIC"
        )

    op.execute(
        """
        GRANT SELECT, INSERT, UPDATE
        ON TABLE finance.credit_note_series
        TO app_security_owner
        """
    )
    op.execute(
        """
        GRANT SELECT, INSERT
        ON TABLE finance.refund_credit_note_links
        TO app_security_owner
        """
    )
    op.execute(
        """
        GRANT SELECT, INSERT
        ON TABLE finance.refund_provider_evidence
        TO app_security_owner
        """
    )

    for table_name in _NEW_TABLES:
        op.execute(
            f"""
            CREATE POLICY pay10_{table_name}_security_owner_all
            ON finance.{table_name}
            FOR ALL
            TO app_security_owner
            USING (true)
            WITH CHECK (true)
            """
        )

    op.execute(
        """
        GRANT TRIGGER
        ON TABLE finance.refund_provider_evidence
        TO app_security_owner
        """
    )
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        op.execute(
            f"""
            CREATE TRIGGER {_EVIDENCE_TRIGGER}
            BEFORE UPDATE OR DELETE
            ON finance.refund_provider_evidence
            FOR EACH ROW
            EXECUTE FUNCTION
                app_secure.pay2_reject_finance_immutable_history_mutation()
            """
        )
    finally:
        op.execute("RESET ROLE")
    op.execute(
        """
        REVOKE TRIGGER
        ON TABLE finance.refund_provider_evidence
        FROM app_security_owner
        """
    )

    # PAY-10-C installs only bounded SECURITY DEFINER capabilities. Runtime
    # identities remain table-blind and cannot reach the security owner.
    op.execute(
        "GRANT CREATE ON SCHEMA app_secure TO app_security_owner"
    )
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        op.execute(
            """
            CREATE FUNCTION app_secure.claim_pay10_refund_provider_execution(
                p_worker_id uuid,
                p_limit integer DEFAULT 1
            )
            RETURNS TABLE(
                command_id uuid,
                refund_id uuid,
                payment_id uuid,
                organization_id uuid,
                provider_code text,
                provider_payment_ref text,
                amount numeric,
                currency_code char(3),
                attempt_count integer,
                lease_fence bigint,
                reclaimed_existing_attempt boolean,
                lease_expires_at timestamptz
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            BEGIN
                IF NOT pg_catalog.pg_has_role(
                    session_user,
                    'finance_refund_runtime',
                    'MEMBER'
                ) THEN
                    RAISE EXCEPTION
                        'PAY-10 refund claim requires finance_refund_runtime'
                        USING ERRCODE='42501';
                END IF;
                IF p_worker_id IS NULL THEN
                    RAISE EXCEPTION
                        'PAY-10 refund claim requires worker id'
                        USING ERRCODE='22023';
                END IF;

                RETURN QUERY
                WITH candidates AS (
                    SELECT
                        c.command_id,
                        c.status='processing' AS reclaiming
                    FROM finance.refund_execution_commands c
                    JOIN finance.refunds r
                      ON r.id=c.refund_id
                    WHERE r.status IN (
                        'requested','approved','processing'
                    )
                      AND (
                        (
                            c.status IN ('pending','retry_pending')
                            AND c.attempt_count<c.max_attempts
                            AND c.process_after<=pg_catalog.clock_timestamp()
                        )
                        OR (
                            c.status='processing'
                            AND c.leased_until<=pg_catalog.clock_timestamp()
                        )
                      )
                    ORDER BY
                        c.process_after,
                        c.materialized_at,
                        c.command_id
                    LIMIT greatest(
                        1,
                        least(coalesce(p_limit,1),50)
                    )
                    FOR UPDATE OF c SKIP LOCKED
                ),
                claimed AS (
                    UPDATE finance.refund_execution_commands AS c
                    SET
                        status='processing',
                        attempt_count=CASE
                            WHEN candidates.reclaiming
                            THEN c.attempt_count
                            ELSE c.attempt_count+1
                        END,
                        lease_fence=c.lease_fence+1,
                        leased_by=p_worker_id,
                        leased_until=pg_catalog.clock_timestamp()
                            + interval '10 minutes',
                        last_error_code=NULL,
                        updated_at=pg_catalog.clock_timestamp()
                    FROM candidates
                    WHERE c.command_id=candidates.command_id
                    RETURNING
                        c.command_id,
                        c.refund_id,
                        c.payment_id,
                        c.organization_id,
                        c.amount,
                        c.currency_code,
                        c.attempt_count,
                        c.lease_fence,
                        c.leased_until,
                        candidates.reclaiming
                )
                SELECT
                    cl.command_id,
                    cl.refund_id,
                    cl.payment_id,
                    cl.organization_id,
                    p.provider_code::text,
                    p.provider_payment_ref::text,
                    cl.amount,
                    cl.currency_code,
                    cl.attempt_count,
                    cl.lease_fence,
                    cl.reclaiming,
                    cl.leased_until
                FROM claimed cl
                JOIN finance.payments p
                  ON p.id=cl.payment_id
                ORDER BY cl.command_id;
            END
            $function$
            """
        )

        op.execute(
            """
            CREATE FUNCTION app_secure.bind_pay10_refund_provider_request(
                p_command_id uuid,
                p_worker_id uuid,
                p_lease_fence bigint,
                p_provider_code text,
                p_provider_payment_ref text,
                p_amount numeric,
                p_currency_code text,
                p_request_sha256 text
            )
            RETURNS TABLE(
                command_id uuid,
                refund_id uuid,
                payment_id uuid,
                organization_id uuid,
                provider_code text,
                provider_payment_ref text,
                amount numeric,
                currency_code char(3),
                request_sha256 text,
                status text
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_payment finance.payments%ROWTYPE;
                v_refund finance.refunds%ROWTYPE;
                v_command finance.refund_execution_commands%ROWTYPE;
                v_allocated numeric;
                v_reserved numeric;
            BEGIN
                IF NOT pg_catalog.pg_has_role(
                    session_user,
                    'finance_refund_runtime',
                    'MEMBER'
                ) THEN
                    RAISE EXCEPTION
                        'PAY-10 request binding requires finance_refund_runtime'
                        USING ERRCODE='42501';
                END IF;
                IF p_command_id IS NULL
                   OR p_worker_id IS NULL
                   OR p_lease_fence IS NULL
                THEN
                    RAISE EXCEPTION
                        'PAY-10 request binding requires command/fence identity'
                        USING ERRCODE='22023';
                END IF;
                IF p_request_sha256 IS NULL
                   OR p_request_sha256 !~ '^[0-9a-f]{64}
    for table_name in _NEW_TABLES:
        row = bind.execute(
            sa.text(
                """
                SELECT c.relrowsecurity,c.relforcerowsecurity
                FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='finance'
                  AND c.relname=:table_name
                """
            ),
            {"table_name": table_name},
        ).one()
        if tuple(bool(v) for v in row) != (True, True):
            raise RuntimeError(
                f"PAY-10 RLS posture drift: finance.{table_name}"
            )

    trigger_count = bind.execute(
        sa.text(
            """
            SELECT count(*)
            FROM pg_catalog.pg_trigger t
            JOIN pg_catalog.pg_class c ON c.oid=t.tgrelid
            JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='finance'
              AND c.relname='refund_provider_evidence'
              AND t.tgname=:trigger_name
              AND NOT t.tgisinternal
              AND t.tgenabled='O'
            """
        ),
        {"trigger_name": _EVIDENCE_TRIGGER},
    ).scalar_one()
    if int(trigger_count) != 1:
        raise RuntimeError(
            "PAY-10 immutable refund provider evidence trigger missing"
        )

    if bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.has_table_privilege("
                "'app_security_owner',"
                "'finance.refund_provider_evidence','TRIGGER')"
            )
        ).scalar_one()
    ):
        raise RuntimeError(
            "PAY-10 installation-only TRIGGER authority leaked"
        )

    for role_name in _RUNTIME_ROLES:
        for table_name in _NEW_TABLES:
            has_direct = bool(
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
                            OR pg_catalog.has_any_column_privilege(
                                :role,:relation,'SELECT'
                            )
                            OR pg_catalog.has_any_column_privilege(
                                :role,:relation,'INSERT'
                            )
                            OR pg_catalog.has_any_column_privilege(
                                :role,:relation,'UPDATE'
                            )
                        """
                    ),
                    {
                        "role": role_name,
                        "relation": f"finance.{table_name}",
                    },
                ).scalar_one()
            )
            if has_direct:
                raise RuntimeError(
                    "PAY-10 direct Finance authority leaked: "
                    f"{role_name} -> finance.{table_name}"
                )

    if bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.has_schema_privilege("
                "'app_security_owner','app_secure','CREATE')"
            )
        ).scalar_one()
    ):
        raise RuntimeError(
            "PAY-10-C installation CREATE authority leaked"
        )

    if not bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.has_schema_privilege("
                ":role,'app_secure','USAGE')"
            ),
            {"role": _REFUND_RUNTIME},
        ).scalar_one()
    ):
        raise RuntimeError(
            "PAY-10-C refund runtime lacks bounded app_secure USAGE"
        )

    expected_execute = {
        _CLAIM_REFUND: {_REFUND_RUNTIME},
        _BIND_REQUEST: {_REFUND_RUNTIME},
        _RECORD_OUTCOME: {_REFUND_RUNTIME},
        _RECORD_UNKNOWN: {_REFUND_RUNTIME},
        _RECORD_FAILURE: {_REFUND_RUNTIME},
        _RECORD_EXTERNAL: {_RECON_RUNTIME},
    }
    for signature, allowed_roles in expected_execute.items():
        row = bind.execute(
            sa.text(
                """
                SELECT
                    pg_catalog.pg_get_userbyid(p.proowner) AS owner,
                    p.prosecdef,
                    coalesce(p.proconfig::text,'') AS config,
                    EXISTS (
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
                JOIN pg_catalog.pg_namespace n
                  ON n.oid=p.pronamespace
                WHERE n.nspname='app_secure'
                  AND p.oid=pg_catalog.to_regprocedure(:signature)
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
                f"PAY-10-C function security drift: {signature}"
            )
        for role_name in (
            "app_runtime",
            "worker_runtime",
            _REFUND_RUNTIME,
            _RECON_RUNTIME,
            "finance_payment_runtime",
            "finance_maintenance_runtime",
        ):
            actual = bool(
                bind.execute(
                    sa.text(
                        "SELECT pg_catalog.has_function_privilege("
                        ":role,:signature,'EXECUTE')"
                    ),
                    {
                        "role": role_name,
                        "signature": signature,
                    },
                ).scalar_one()
            )
            if actual is (role_name not in allowed_roles):
                raise RuntimeError(
                    "PAY-10-C execute ACL drift: "
                    f"{role_name} -> {signature}"
                )

    for role_name in (_REFUND_RUNTIME, _RECON_RUNTIME):
        for relation in (
            "finance.refund_execution_commands",
            "finance.refunds",
            "finance.payments",
            "finance.payment_allocations",
            "finance.refund_provider_evidence",
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
                    {"role": role_name, "relation": relation},
                ).scalar_one()
            ):
                raise RuntimeError(
                    "PAY-10-C runtime gained direct Finance table authority: "
                    f"{role_name} -> {relation}"
                )


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _require_predecessor(bind)
    _install()
    _post_install_proof(bind)


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _require_reduced_role(bind, _MIGRATION_OWNER, login=True)
    identity = bind.execute(
        sa.text("SELECT session_user::text,current_user::text")
    ).one()
    if tuple(identity) != (_MIGRATION_OWNER, _MIGRATION_OWNER):
        raise RuntimeError("PAY-10 downgrade requires migration_owner")

    # PAY-10 data tables are FORCE RLS and deliberately invisible to
    # migration_owner.  Use only the pre-existing bounded SET edge to the
    # NOLOGIN security owner for evidence inspection, then return to the
    # migration identity before schema teardown.
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        for table_name in _NEW_TABLES:
            if _relation_exists(bind, f"finance.{table_name}"):
                row_count = bind.execute(
                    sa.text(
                        f"SELECT count(*) FROM finance.{table_name}"
                    )
                ).scalar_one()
                if int(row_count) != 0:
                    raise RuntimeError(
                        "PAY-10 refuses populated downgrade: "
                        f"finance.{table_name}"
                    )

        command_evidence = bind.execute(
            sa.text(
                """
                SELECT count(*)
                FROM finance.refund_execution_commands
                WHERE request_sha256 IS NOT NULL
                   OR first_attempted_at IS NOT NULL
                   OR provider_accepted_at IS NOT NULL
                   OR completed_at IS NOT NULL
                """
            )
        ).scalar_one()
        if int(command_evidence) != 0:
            raise RuntimeError(
                "PAY-10 refuses downgrade with refund execution attempt evidence"
            )
    finally:
        op.execute("RESET ROLE")

    op.execute(
        f"""
        DROP TRIGGER IF EXISTS {_EVIDENCE_TRIGGER}
        ON finance.refund_provider_evidence
        """
    )

    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        for signature in (
            _CLAIM_REFUND,
            _BIND_REQUEST,
            _RECORD_OUTCOME,
            _RECORD_UNKNOWN,
            _RECORD_FAILURE,
        ):
            op.execute(
                f"REVOKE EXECUTE ON FUNCTION {signature} "
                "FROM finance_refund_runtime"
            )
        op.execute(
            f"REVOKE EXECUTE ON FUNCTION {_RECORD_EXTERNAL} "
            "FROM finance_reconciliation_runtime"
        )
        op.execute(
            "REVOKE USAGE ON SCHEMA app_secure "
            "FROM finance_refund_runtime"
        )
        for signature in reversed(_PAY10_FUNCTIONS):
            op.execute(f"DROP FUNCTION {signature}")
    finally:
        op.execute("RESET ROLE")

    for table_name in reversed(_NEW_TABLES):
        op.execute(
            f"DROP POLICY IF EXISTS "
            f"pay10_{table_name}_security_owner_all "
            f"ON finance.{table_name}"
        )

    op.execute(
        """
        REVOKE ALL
        ON TABLE finance.refund_provider_evidence
        FROM app_security_owner
        """
    )
    op.execute(
        """
        REVOKE ALL
        ON TABLE finance.refund_credit_note_links
        FROM app_security_owner
        """
    )
    op.execute(
        """
        REVOKE ALL
        ON TABLE finance.credit_note_series
        FROM app_security_owner
        """
    )

    op.execute("DROP TABLE finance.refund_provider_evidence")
    op.execute("DROP TABLE finance.refund_credit_note_links")
    op.execute("DROP TABLE finance.credit_note_series")

    op.execute(
        "DROP INDEX IF EXISTS finance.uq_pay10_refund_command_provider_ref"
    )
    op.execute(
        """
        ALTER TABLE finance.refund_execution_commands
        DROP CONSTRAINT chk_pay10_refund_command_completion_timestamps,
        DROP CONSTRAINT chk_pay10_refund_command_attempt_timestamps,
        DROP CONSTRAINT chk_pay10_refund_command_request_hash,
        DROP COLUMN completed_at,
        DROP COLUMN provider_accepted_at,
        DROP COLUMN first_attempted_at,
        DROP COLUMN request_sha256
        """
    )
    op.execute(
        """
        ALTER TABLE finance.credit_notes
        DROP CONSTRAINT uq_pay10_credit_notes_id_org
        """
    )
    op.execute(
        """
        ALTER TABLE finance.refunds
        DROP CONSTRAINT uq_pay10_refunds_id_org
        """
    )

                THEN
                    RAISE EXCEPTION
                        'PAY-10 request hash must be lowercase sha256'
                        USING ERRCODE='22023';
                END IF;

                SELECT p.* INTO v_payment
                FROM finance.payments p
                JOIN finance.refund_execution_commands c
                  ON c.payment_id=p.id
                WHERE c.command_id=p_command_id
                FOR UPDATE OF p;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'PAY-10 refund payment not found'
                        USING ERRCODE='P0002';
                END IF;

                SELECT r.* INTO v_refund
                FROM finance.refunds r
                JOIN finance.refund_execution_commands c
                  ON c.refund_id=r.id
                WHERE c.command_id=p_command_id
                FOR UPDATE OF r;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'PAY-10 refund intent not found'
                        USING ERRCODE='P0002';
                END IF;

                SELECT c.* INTO v_command
                FROM finance.refund_execution_commands c
                WHERE c.command_id=p_command_id
                FOR UPDATE;
                IF NOT FOUND
                   OR v_command.status<>'processing'
                   OR v_command.leased_by IS DISTINCT FROM p_worker_id
                   OR v_command.lease_fence IS DISTINCT FROM p_lease_fence
                   OR v_command.leased_until<=pg_catalog.clock_timestamp()
                THEN
                    RAISE EXCEPTION
                        'PAY-10 stale refund execution fence'
                        USING ERRCODE='40001';
                END IF;

                IF v_refund.payment_id IS DISTINCT FROM v_payment.id
                   OR v_command.refund_id IS DISTINCT FROM v_refund.id
                   OR v_command.payment_id IS DISTINCT FROM v_payment.id
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
                        'PAY-10 refund authority drift'
                        USING ERRCODE='23514';
                END IF;

                IF v_refund.status NOT IN (
                    'requested','approved','processing'
                )
                   OR v_payment.status NOT IN (
                    'captured','settled','partially_refunded'
                )
                   OR v_payment.organization_id IS NULL
                   OR v_payment.provider_payment_ref IS NULL
                   OR btrim(v_payment.provider_payment_ref)=''
                   OR btrim(v_payment.provider_code)=''
                THEN
                    RAISE EXCEPTION
                        'PAY-10 refund/provider state is not executable'
                        USING ERRCODE='23514';
                END IF;

                IF p_provider_code IS DISTINCT FROM v_payment.provider_code
                   OR p_provider_payment_ref
                        IS DISTINCT FROM v_payment.provider_payment_ref
                   OR p_amount IS DISTINCT FROM v_command.amount
                   OR upper(btrim(coalesce(p_currency_code,'')))
                        IS DISTINCT FROM v_command.currency_code
                THEN
                    RAISE EXCEPTION
                        'PAY-10 caller request differs from Finance authority'
                        USING ERRCODE='23514';
                END IF;

                IF v_command.provider_refund_ref IS NOT NULL THEN
                    RAISE EXCEPTION
                        'PAY-10 known provider refund must reconcile, not resubmit'
                        USING ERRCODE='23514';
                END IF;
                IF v_command.provider_code IS NOT NULL
                   AND v_command.provider_code<>v_payment.provider_code
                THEN
                    RAISE EXCEPTION
                        'PAY-10 provider binding drift'
                        USING ERRCODE='23514';
                END IF;
                IF v_command.request_sha256 IS NOT NULL
                   AND v_command.request_sha256<>p_request_sha256
                THEN
                    RAISE EXCEPTION
                        'PAY-10 request hash drift across retry'
                        USING ERRCODE='23514';
                END IF;

                SELECT coalesce(sum(a.allocated_amount),0)
                INTO v_allocated
                FROM finance.payment_allocations a
                WHERE a.payment_id=v_payment.id;

                SELECT coalesce(sum(r.amount),0)
                INTO v_reserved
                FROM finance.refunds r
                WHERE r.payment_id=v_payment.id
                  AND r.status IN (
                      'requested','approved','processing','succeeded'
                  );

                IF v_allocated<=0
                   OR v_reserved>v_allocated
                THEN
                    RAISE EXCEPTION
                        'PAY-10 refundable reservation exceeds applied value'
                        USING ERRCODE='23514';
                END IF;

                UPDATE finance.refund_execution_commands AS c
                SET
                    request_sha256=coalesce(
                        c.request_sha256,
                        p_request_sha256
                    ),
                    first_attempted_at=coalesce(
                        c.first_attempted_at,
                        pg_catalog.clock_timestamp()
                    ),
                    provider_code=coalesce(
                        c.provider_code,
                        v_payment.provider_code
                    ),
                    updated_at=pg_catalog.clock_timestamp()
                WHERE c.command_id=v_command.command_id
                RETURNING c.* INTO v_command;

                UPDATE finance.refunds AS r
                SET
                    status='processing',
                    updated_at=pg_catalog.clock_timestamp()
                WHERE r.id=v_refund.id
                  AND r.status IN ('requested','approved');

                RETURN QUERY SELECT
                    v_command.command_id,
                    v_command.refund_id,
                    v_command.payment_id,
                    v_command.organization_id,
                    v_payment.provider_code::text,
                    v_payment.provider_payment_ref::text,
                    v_command.amount,
                    v_command.currency_code,
                    v_command.request_sha256::text,
                    v_command.status::text;
            END
            $function$
            """
        )

        op.execute(
            """
            CREATE FUNCTION app_secure.record_pay10_refund_provider_outcome(
                p_command_id uuid,
                p_worker_id uuid,
                p_lease_fence bigint,
                p_provider_refund_ref text,
                p_normalized_status text,
                p_evidence_sha256 text,
                p_occurred_at timestamptz DEFAULT NULL
            )
            RETURNS TABLE(
                evidence_id uuid,
                command_id uuid,
                normalized_status text,
                command_status text,
                replayed boolean
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_payment finance.payments%ROWTYPE;
                v_refund finance.refunds%ROWTYPE;
                v_command finance.refund_execution_commands%ROWTYPE;
                v_existing finance.refund_provider_evidence%ROWTYPE;
                v_evidence finance.refund_provider_evidence%ROWTYPE;
                v_next_status text;
            BEGIN
                IF NOT pg_catalog.pg_has_role(
                    session_user,
                    'finance_refund_runtime',
                    'MEMBER'
                ) THEN
                    RAISE EXCEPTION
                        'PAY-10 outcome recording requires finance_refund_runtime'
                        USING ERRCODE='42501';
                END IF;
                IF p_normalized_status NOT IN (
                    'pending','processed','failed'
                ) THEN
                    RAISE EXCEPTION
                        'PAY-10 normalized provider status invalid'
                        USING ERRCODE='22023';
                END IF;
                IF p_evidence_sha256 IS NULL
                   OR p_evidence_sha256 !~ '^[0-9a-f]{64}
    for table_name in _NEW_TABLES:
        row = bind.execute(
            sa.text(
                """
                SELECT c.relrowsecurity,c.relforcerowsecurity
                FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='finance'
                  AND c.relname=:table_name
                """
            ),
            {"table_name": table_name},
        ).one()
        if tuple(bool(v) for v in row) != (True, True):
            raise RuntimeError(
                f"PAY-10 RLS posture drift: finance.{table_name}"
            )

    trigger_count = bind.execute(
        sa.text(
            """
            SELECT count(*)
            FROM pg_catalog.pg_trigger t
            JOIN pg_catalog.pg_class c ON c.oid=t.tgrelid
            JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='finance'
              AND c.relname='refund_provider_evidence'
              AND t.tgname=:trigger_name
              AND NOT t.tgisinternal
              AND t.tgenabled='O'
            """
        ),
        {"trigger_name": _EVIDENCE_TRIGGER},
    ).scalar_one()
    if int(trigger_count) != 1:
        raise RuntimeError(
            "PAY-10 immutable refund provider evidence trigger missing"
        )

    if bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.has_table_privilege("
                "'app_security_owner',"
                "'finance.refund_provider_evidence','TRIGGER')"
            )
        ).scalar_one()
    ):
        raise RuntimeError(
            "PAY-10 installation-only TRIGGER authority leaked"
        )

    for role_name in _RUNTIME_ROLES:
        for table_name in _NEW_TABLES:
            has_direct = bool(
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
                            OR pg_catalog.has_any_column_privilege(
                                :role,:relation,'SELECT'
                            )
                            OR pg_catalog.has_any_column_privilege(
                                :role,:relation,'INSERT'
                            )
                            OR pg_catalog.has_any_column_privilege(
                                :role,:relation,'UPDATE'
                            )
                        """
                    ),
                    {
                        "role": role_name,
                        "relation": f"finance.{table_name}",
                    },
                ).scalar_one()
            )
            if has_direct:
                raise RuntimeError(
                    "PAY-10 direct Finance authority leaked: "
                    f"{role_name} -> finance.{table_name}"
                )


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _require_predecessor(bind)
    _install()
    _post_install_proof(bind)


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _require_reduced_role(bind, _MIGRATION_OWNER, login=True)
    identity = bind.execute(
        sa.text("SELECT session_user::text,current_user::text")
    ).one()
    if tuple(identity) != (_MIGRATION_OWNER, _MIGRATION_OWNER):
        raise RuntimeError("PAY-10 downgrade requires migration_owner")

    # PAY-10 data tables are FORCE RLS and deliberately invisible to
    # migration_owner.  Use only the pre-existing bounded SET edge to the
    # NOLOGIN security owner for evidence inspection, then return to the
    # migration identity before schema teardown.
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        for table_name in _NEW_TABLES:
            if _relation_exists(bind, f"finance.{table_name}"):
                row_count = bind.execute(
                    sa.text(
                        f"SELECT count(*) FROM finance.{table_name}"
                    )
                ).scalar_one()
                if int(row_count) != 0:
                    raise RuntimeError(
                        "PAY-10 refuses populated downgrade: "
                        f"finance.{table_name}"
                    )

        command_evidence = bind.execute(
            sa.text(
                """
                SELECT count(*)
                FROM finance.refund_execution_commands
                WHERE request_sha256 IS NOT NULL
                   OR first_attempted_at IS NOT NULL
                   OR provider_accepted_at IS NOT NULL
                   OR completed_at IS NOT NULL
                """
            )
        ).scalar_one()
        if int(command_evidence) != 0:
            raise RuntimeError(
                "PAY-10 refuses downgrade with refund execution attempt evidence"
            )
    finally:
        op.execute("RESET ROLE")

    op.execute(
        f"""
        DROP TRIGGER IF EXISTS {_EVIDENCE_TRIGGER}
        ON finance.refund_provider_evidence
        """
    )

    for table_name in reversed(_NEW_TABLES):
        op.execute(
            f"DROP POLICY IF EXISTS "
            f"pay10_{table_name}_security_owner_all "
            f"ON finance.{table_name}"
        )

    op.execute(
        """
        REVOKE ALL
        ON TABLE finance.refund_provider_evidence
        FROM app_security_owner
        """
    )
    op.execute(
        """
        REVOKE ALL
        ON TABLE finance.refund_credit_note_links
        FROM app_security_owner
        """
    )
    op.execute(
        """
        REVOKE ALL
        ON TABLE finance.credit_note_series
        FROM app_security_owner
        """
    )

    op.execute("DROP TABLE finance.refund_provider_evidence")
    op.execute("DROP TABLE finance.refund_credit_note_links")
    op.execute("DROP TABLE finance.credit_note_series")

    op.execute(
        "DROP INDEX IF EXISTS finance.uq_pay10_refund_command_provider_ref"
    )
    op.execute(
        """
        ALTER TABLE finance.refund_execution_commands
        DROP CONSTRAINT chk_pay10_refund_command_completion_timestamps,
        DROP CONSTRAINT chk_pay10_refund_command_attempt_timestamps,
        DROP CONSTRAINT chk_pay10_refund_command_request_hash,
        DROP COLUMN completed_at,
        DROP COLUMN provider_accepted_at,
        DROP COLUMN first_attempted_at,
        DROP COLUMN request_sha256
        """
    )
    op.execute(
        """
        ALTER TABLE finance.credit_notes
        DROP CONSTRAINT uq_pay10_credit_notes_id_org
        """
    )
    op.execute(
        """
        ALTER TABLE finance.refunds
        DROP CONSTRAINT uq_pay10_refunds_id_org
        """
    )

                THEN
                    RAISE EXCEPTION
                        'PAY-10 provider evidence hash invalid'
                        USING ERRCODE='22023';
                END IF;
                IF p_provider_refund_ref IS NOT NULL
                   AND (
                       char_length(p_provider_refund_ref)>200
                       OR p_provider_refund_ref
                            !~ '^[A-Za-z0-9_-]+
    for table_name in _NEW_TABLES:
        row = bind.execute(
            sa.text(
                """
                SELECT c.relrowsecurity,c.relforcerowsecurity
                FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='finance'
                  AND c.relname=:table_name
                """
            ),
            {"table_name": table_name},
        ).one()
        if tuple(bool(v) for v in row) != (True, True):
            raise RuntimeError(
                f"PAY-10 RLS posture drift: finance.{table_name}"
            )

    trigger_count = bind.execute(
        sa.text(
            """
            SELECT count(*)
            FROM pg_catalog.pg_trigger t
            JOIN pg_catalog.pg_class c ON c.oid=t.tgrelid
            JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='finance'
              AND c.relname='refund_provider_evidence'
              AND t.tgname=:trigger_name
              AND NOT t.tgisinternal
              AND t.tgenabled='O'
            """
        ),
        {"trigger_name": _EVIDENCE_TRIGGER},
    ).scalar_one()
    if int(trigger_count) != 1:
        raise RuntimeError(
            "PAY-10 immutable refund provider evidence trigger missing"
        )

    if bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.has_table_privilege("
                "'app_security_owner',"
                "'finance.refund_provider_evidence','TRIGGER')"
            )
        ).scalar_one()
    ):
        raise RuntimeError(
            "PAY-10 installation-only TRIGGER authority leaked"
        )

    for role_name in _RUNTIME_ROLES:
        for table_name in _NEW_TABLES:
            has_direct = bool(
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
                            OR pg_catalog.has_any_column_privilege(
                                :role,:relation,'SELECT'
                            )
                            OR pg_catalog.has_any_column_privilege(
                                :role,:relation,'INSERT'
                            )
                            OR pg_catalog.has_any_column_privilege(
                                :role,:relation,'UPDATE'
                            )
                        """
                    ),
                    {
                        "role": role_name,
                        "relation": f"finance.{table_name}",
                    },
                ).scalar_one()
            )
            if has_direct:
                raise RuntimeError(
                    "PAY-10 direct Finance authority leaked: "
                    f"{role_name} -> finance.{table_name}"
                )


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _require_predecessor(bind)
    _install()
    _post_install_proof(bind)


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _require_reduced_role(bind, _MIGRATION_OWNER, login=True)
    identity = bind.execute(
        sa.text("SELECT session_user::text,current_user::text")
    ).one()
    if tuple(identity) != (_MIGRATION_OWNER, _MIGRATION_OWNER):
        raise RuntimeError("PAY-10 downgrade requires migration_owner")

    # PAY-10 data tables are FORCE RLS and deliberately invisible to
    # migration_owner.  Use only the pre-existing bounded SET edge to the
    # NOLOGIN security owner for evidence inspection, then return to the
    # migration identity before schema teardown.
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        for table_name in _NEW_TABLES:
            if _relation_exists(bind, f"finance.{table_name}"):
                row_count = bind.execute(
                    sa.text(
                        f"SELECT count(*) FROM finance.{table_name}"
                    )
                ).scalar_one()
                if int(row_count) != 0:
                    raise RuntimeError(
                        "PAY-10 refuses populated downgrade: "
                        f"finance.{table_name}"
                    )

        command_evidence = bind.execute(
            sa.text(
                """
                SELECT count(*)
                FROM finance.refund_execution_commands
                WHERE request_sha256 IS NOT NULL
                   OR first_attempted_at IS NOT NULL
                   OR provider_accepted_at IS NOT NULL
                   OR completed_at IS NOT NULL
                """
            )
        ).scalar_one()
        if int(command_evidence) != 0:
            raise RuntimeError(
                "PAY-10 refuses downgrade with refund execution attempt evidence"
            )
    finally:
        op.execute("RESET ROLE")

    op.execute(
        f"""
        DROP TRIGGER IF EXISTS {_EVIDENCE_TRIGGER}
        ON finance.refund_provider_evidence
        """
    )

    for table_name in reversed(_NEW_TABLES):
        op.execute(
            f"DROP POLICY IF EXISTS "
            f"pay10_{table_name}_security_owner_all "
            f"ON finance.{table_name}"
        )

    op.execute(
        """
        REVOKE ALL
        ON TABLE finance.refund_provider_evidence
        FROM app_security_owner
        """
    )
    op.execute(
        """
        REVOKE ALL
        ON TABLE finance.refund_credit_note_links
        FROM app_security_owner
        """
    )
    op.execute(
        """
        REVOKE ALL
        ON TABLE finance.credit_note_series
        FROM app_security_owner
        """
    )

    op.execute("DROP TABLE finance.refund_provider_evidence")
    op.execute("DROP TABLE finance.refund_credit_note_links")
    op.execute("DROP TABLE finance.credit_note_series")

    op.execute(
        "DROP INDEX IF EXISTS finance.uq_pay10_refund_command_provider_ref"
    )
    op.execute(
        """
        ALTER TABLE finance.refund_execution_commands
        DROP CONSTRAINT chk_pay10_refund_command_completion_timestamps,
        DROP CONSTRAINT chk_pay10_refund_command_attempt_timestamps,
        DROP CONSTRAINT chk_pay10_refund_command_request_hash,
        DROP COLUMN completed_at,
        DROP COLUMN provider_accepted_at,
        DROP COLUMN first_attempted_at,
        DROP COLUMN request_sha256
        """
    )
    op.execute(
        """
        ALTER TABLE finance.credit_notes
        DROP CONSTRAINT uq_pay10_credit_notes_id_org
        """
    )
    op.execute(
        """
        ALTER TABLE finance.refunds
        DROP CONSTRAINT uq_pay10_refunds_id_org
        """
    )

                   )
                THEN
                    RAISE EXCEPTION
                        'PAY-10 provider refund reference invalid'
                        USING ERRCODE='22023';
                END IF;
                IF p_normalized_status<>'failed'
                   AND p_provider_refund_ref IS NULL
                THEN
                    RAISE EXCEPTION
                        'PAY-10 accepted provider outcome requires refund reference'
                        USING ERRCODE='22023';
                END IF;

                SELECT e.* INTO v_existing
                FROM finance.refund_provider_evidence e
                WHERE e.command_id=p_command_id
                  AND e.evidence_sha256=p_evidence_sha256;
                IF FOUND THEN
                    SELECT c.* INTO v_command
                    FROM finance.refund_execution_commands c
                    WHERE c.command_id=p_command_id;
                    IF v_existing.evidence_source<>'submission'
                       OR v_existing.provider_refund_ref
                            IS DISTINCT FROM p_provider_refund_ref
                       OR v_existing.normalized_status
                            IS DISTINCT FROM p_normalized_status
                       OR v_existing.occurred_at
                            IS DISTINCT FROM p_occurred_at
                    THEN
                        RAISE EXCEPTION
                            'PAY-10 conflicting provider evidence replay'
                            USING ERRCODE='23505';
                    END IF;
                    RETURN QUERY SELECT
                        v_existing.id,
                        v_existing.command_id,
                        v_existing.normalized_status::text,
                        v_command.status::text,
                        true;
                    RETURN;
                END IF;

                SELECT p.* INTO v_payment
                FROM finance.payments p
                JOIN finance.refund_execution_commands c
                  ON c.payment_id=p.id
                WHERE c.command_id=p_command_id
                FOR UPDATE OF p;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'PAY-10 outcome payment not found'
                        USING ERRCODE='P0002';
                END IF;
                SELECT r.* INTO v_refund
                FROM finance.refunds r
                JOIN finance.refund_execution_commands c
                  ON c.refund_id=r.id
                WHERE c.command_id=p_command_id
                FOR UPDATE OF r;
                SELECT c.* INTO v_command
                FROM finance.refund_execution_commands c
                WHERE c.command_id=p_command_id
                FOR UPDATE;

                IF NOT FOUND
                   OR v_command.status<>'processing'
                   OR v_command.leased_by IS DISTINCT FROM p_worker_id
                   OR v_command.lease_fence IS DISTINCT FROM p_lease_fence
                   OR v_command.leased_until<=pg_catalog.clock_timestamp()
                THEN
                    RAISE EXCEPTION
                        'PAY-10 stale refund execution fence'
                        USING ERRCODE='40001';
                END IF;
                IF v_command.request_sha256 IS NULL
                   OR v_command.provider_code IS NULL
                   OR v_command.provider_code<>v_payment.provider_code
                   OR v_command.payment_id<>v_payment.id
                   OR v_command.refund_id<>v_refund.id
                   OR v_command.organization_id
                        IS DISTINCT FROM v_payment.organization_id
                   OR v_refund.payment_id<>v_payment.id
                   OR v_refund.organization_id
                        IS DISTINCT FROM v_payment.organization_id
                THEN
                    RAISE EXCEPTION
                        'PAY-10 provider outcome authority drift'
                        USING ERRCODE='23514';
                END IF;
                IF v_command.provider_refund_ref IS NOT NULL
                   AND v_command.provider_refund_ref
                        IS DISTINCT FROM p_provider_refund_ref
                THEN
                    RAISE EXCEPTION
                        'PAY-10 provider refund identity drift'
                        USING ERRCODE='23514';
                END IF;

                INSERT INTO finance.refund_provider_evidence(
                    command_id,
                    refund_id,
                    payment_id,
                    organization_id,
                    provider_code,
                    provider_event_id,
                    provider_refund_ref,
                    evidence_source,
                    normalized_status,
                    request_sha256,
                    evidence_sha256,
                    occurred_at
                )
                VALUES(
                    v_command.command_id,
                    v_command.refund_id,
                    v_command.payment_id,
                    v_command.organization_id,
                    v_command.provider_code,
                    NULL,
                    p_provider_refund_ref,
                    'submission',
                    p_normalized_status,
                    v_command.request_sha256,
                    p_evidence_sha256,
                    p_occurred_at
                )
                RETURNING * INTO v_evidence;

                v_next_status:=CASE
                    WHEN p_normalized_status='pending'
                    THEN 'provider_accepted'
                    WHEN p_normalized_status='processed'
                    THEN 'reconciliation_pending'
                    ELSE 'rejected'
                END;

                UPDATE finance.refund_execution_commands AS c
                SET
                    status=v_next_status,
                    leased_by=NULL,
                    leased_until=NULL,
                    provider_refund_ref=coalesce(
                        c.provider_refund_ref,
                        p_provider_refund_ref
                    ),
                    provider_evidence_sha256=p_evidence_sha256,
                    provider_accepted_at=CASE
                        WHEN p_normalized_status IN (
                            'pending','processed'
                        )
                        THEN coalesce(
                            c.provider_accepted_at,
                            pg_catalog.clock_timestamp()
                        )
                        ELSE c.provider_accepted_at
                    END,
                    last_error_code=CASE
                        WHEN p_normalized_status='failed'
                        THEN 'provider_failed'
                        ELSE NULL
                    END,
                    updated_at=pg_catalog.clock_timestamp()
                WHERE c.command_id=v_command.command_id;

                RETURN QUERY SELECT
                    v_evidence.id,
                    v_evidence.command_id,
                    v_evidence.normalized_status::text,
                    v_next_status,
                    false;
            END
            $function$
            """
        )

        op.execute(
            """
            CREATE FUNCTION app_secure.record_pay10_refund_provider_unknown(
                p_command_id uuid,
                p_worker_id uuid,
                p_lease_fence bigint,
                p_error_code text
            )
            RETURNS text
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_command finance.refund_execution_commands%ROWTYPE;
                v_error text;
            BEGIN
                IF NOT pg_catalog.pg_has_role(
                    session_user,
                    'finance_refund_runtime',
                    'MEMBER'
                ) THEN
                    RAISE EXCEPTION
                        'PAY-10 unknown outcome requires finance_refund_runtime'
                        USING ERRCODE='42501';
                END IF;
                v_error:=coalesce(
                    nullif(btrim(p_error_code),''),
                    'provider_unknown'
                );
                IF v_error !~ '^[a-z][a-z0-9_]{0,63}
    for table_name in _NEW_TABLES:
        row = bind.execute(
            sa.text(
                """
                SELECT c.relrowsecurity,c.relforcerowsecurity
                FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='finance'
                  AND c.relname=:table_name
                """
            ),
            {"table_name": table_name},
        ).one()
        if tuple(bool(v) for v in row) != (True, True):
            raise RuntimeError(
                f"PAY-10 RLS posture drift: finance.{table_name}"
            )

    trigger_count = bind.execute(
        sa.text(
            """
            SELECT count(*)
            FROM pg_catalog.pg_trigger t
            JOIN pg_catalog.pg_class c ON c.oid=t.tgrelid
            JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='finance'
              AND c.relname='refund_provider_evidence'
              AND t.tgname=:trigger_name
              AND NOT t.tgisinternal
              AND t.tgenabled='O'
            """
        ),
        {"trigger_name": _EVIDENCE_TRIGGER},
    ).scalar_one()
    if int(trigger_count) != 1:
        raise RuntimeError(
            "PAY-10 immutable refund provider evidence trigger missing"
        )

    if bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.has_table_privilege("
                "'app_security_owner',"
                "'finance.refund_provider_evidence','TRIGGER')"
            )
        ).scalar_one()
    ):
        raise RuntimeError(
            "PAY-10 installation-only TRIGGER authority leaked"
        )

    for role_name in _RUNTIME_ROLES:
        for table_name in _NEW_TABLES:
            has_direct = bool(
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
                            OR pg_catalog.has_any_column_privilege(
                                :role,:relation,'SELECT'
                            )
                            OR pg_catalog.has_any_column_privilege(
                                :role,:relation,'INSERT'
                            )
                            OR pg_catalog.has_any_column_privilege(
                                :role,:relation,'UPDATE'
                            )
                        """
                    ),
                    {
                        "role": role_name,
                        "relation": f"finance.{table_name}",
                    },
                ).scalar_one()
            )
            if has_direct:
                raise RuntimeError(
                    "PAY-10 direct Finance authority leaked: "
                    f"{role_name} -> finance.{table_name}"
                )


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _require_predecessor(bind)
    _install()
    _post_install_proof(bind)


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _require_reduced_role(bind, _MIGRATION_OWNER, login=True)
    identity = bind.execute(
        sa.text("SELECT session_user::text,current_user::text")
    ).one()
    if tuple(identity) != (_MIGRATION_OWNER, _MIGRATION_OWNER):
        raise RuntimeError("PAY-10 downgrade requires migration_owner")

    # PAY-10 data tables are FORCE RLS and deliberately invisible to
    # migration_owner.  Use only the pre-existing bounded SET edge to the
    # NOLOGIN security owner for evidence inspection, then return to the
    # migration identity before schema teardown.
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        for table_name in _NEW_TABLES:
            if _relation_exists(bind, f"finance.{table_name}"):
                row_count = bind.execute(
                    sa.text(
                        f"SELECT count(*) FROM finance.{table_name}"
                    )
                ).scalar_one()
                if int(row_count) != 0:
                    raise RuntimeError(
                        "PAY-10 refuses populated downgrade: "
                        f"finance.{table_name}"
                    )

        command_evidence = bind.execute(
            sa.text(
                """
                SELECT count(*)
                FROM finance.refund_execution_commands
                WHERE request_sha256 IS NOT NULL
                   OR first_attempted_at IS NOT NULL
                   OR provider_accepted_at IS NOT NULL
                   OR completed_at IS NOT NULL
                """
            )
        ).scalar_one()
        if int(command_evidence) != 0:
            raise RuntimeError(
                "PAY-10 refuses downgrade with refund execution attempt evidence"
            )
    finally:
        op.execute("RESET ROLE")

    op.execute(
        f"""
        DROP TRIGGER IF EXISTS {_EVIDENCE_TRIGGER}
        ON finance.refund_provider_evidence
        """
    )

    for table_name in reversed(_NEW_TABLES):
        op.execute(
            f"DROP POLICY IF EXISTS "
            f"pay10_{table_name}_security_owner_all "
            f"ON finance.{table_name}"
        )

    op.execute(
        """
        REVOKE ALL
        ON TABLE finance.refund_provider_evidence
        FROM app_security_owner
        """
    )
    op.execute(
        """
        REVOKE ALL
        ON TABLE finance.refund_credit_note_links
        FROM app_security_owner
        """
    )
    op.execute(
        """
        REVOKE ALL
        ON TABLE finance.credit_note_series
        FROM app_security_owner
        """
    )

    op.execute("DROP TABLE finance.refund_provider_evidence")
    op.execute("DROP TABLE finance.refund_credit_note_links")
    op.execute("DROP TABLE finance.credit_note_series")

    op.execute(
        "DROP INDEX IF EXISTS finance.uq_pay10_refund_command_provider_ref"
    )
    op.execute(
        """
        ALTER TABLE finance.refund_execution_commands
        DROP CONSTRAINT chk_pay10_refund_command_completion_timestamps,
        DROP CONSTRAINT chk_pay10_refund_command_attempt_timestamps,
        DROP CONSTRAINT chk_pay10_refund_command_request_hash,
        DROP COLUMN completed_at,
        DROP COLUMN provider_accepted_at,
        DROP COLUMN first_attempted_at,
        DROP COLUMN request_sha256
        """
    )
    op.execute(
        """
        ALTER TABLE finance.credit_notes
        DROP CONSTRAINT uq_pay10_credit_notes_id_org
        """
    )
    op.execute(
        """
        ALTER TABLE finance.refunds
        DROP CONSTRAINT uq_pay10_refunds_id_org
        """
    )

                   OR v_error ~ '(bearer|secret|token)'
                THEN
                    RAISE EXCEPTION
                        'PAY-10 unknown error code must be bounded machine token'
                        USING ERRCODE='22023';
                END IF;

                SELECT c.* INTO v_command
                FROM finance.refund_execution_commands c
                WHERE c.command_id=p_command_id
                  AND c.status='processing'
                  AND c.leased_by=p_worker_id
                  AND c.lease_fence=p_lease_fence
                  AND c.leased_until>pg_catalog.clock_timestamp()
                FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'PAY-10 stale refund execution fence'
                        USING ERRCODE='40001';
                END IF;
                IF v_command.request_sha256 IS NULL
                   OR v_command.provider_code IS NULL
                THEN
                    RAISE EXCEPTION
                        'PAY-10 unknown outcome requires bound provider request'
                        USING ERRCODE='23514';
                END IF;

                UPDATE finance.refund_execution_commands AS c
                SET
                    status='reconciliation_pending',
                    leased_by=NULL,
                    leased_until=NULL,
                    last_error_code=v_error,
                    updated_at=pg_catalog.clock_timestamp()
                WHERE c.command_id=v_command.command_id;
                RETURN 'reconciliation_pending';
            END
            $function$
            """
        )

        op.execute(
            """
            CREATE FUNCTION app_secure.record_pay10_refund_provider_failure(
                p_command_id uuid,
                p_worker_id uuid,
                p_lease_fence bigint,
                p_error_code text,
                p_permanent boolean DEFAULT false
            )
            RETURNS text
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_command finance.refund_execution_commands%ROWTYPE;
                v_error text;
                v_next_status text;
            BEGIN
                IF NOT pg_catalog.pg_has_role(
                    session_user,
                    'finance_refund_runtime',
                    'MEMBER'
                ) THEN
                    RAISE EXCEPTION
                        'PAY-10 provider failure requires finance_refund_runtime'
                        USING ERRCODE='42501';
                END IF;
                v_error:=coalesce(
                    nullif(btrim(p_error_code),''),
                    'provider_failure'
                );
                IF v_error !~ '^[a-z][a-z0-9_]{0,63}
    for table_name in _NEW_TABLES:
        row = bind.execute(
            sa.text(
                """
                SELECT c.relrowsecurity,c.relforcerowsecurity
                FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='finance'
                  AND c.relname=:table_name
                """
            ),
            {"table_name": table_name},
        ).one()
        if tuple(bool(v) for v in row) != (True, True):
            raise RuntimeError(
                f"PAY-10 RLS posture drift: finance.{table_name}"
            )

    trigger_count = bind.execute(
        sa.text(
            """
            SELECT count(*)
            FROM pg_catalog.pg_trigger t
            JOIN pg_catalog.pg_class c ON c.oid=t.tgrelid
            JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='finance'
              AND c.relname='refund_provider_evidence'
              AND t.tgname=:trigger_name
              AND NOT t.tgisinternal
              AND t.tgenabled='O'
            """
        ),
        {"trigger_name": _EVIDENCE_TRIGGER},
    ).scalar_one()
    if int(trigger_count) != 1:
        raise RuntimeError(
            "PAY-10 immutable refund provider evidence trigger missing"
        )

    if bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.has_table_privilege("
                "'app_security_owner',"
                "'finance.refund_provider_evidence','TRIGGER')"
            )
        ).scalar_one()
    ):
        raise RuntimeError(
            "PAY-10 installation-only TRIGGER authority leaked"
        )

    for role_name in _RUNTIME_ROLES:
        for table_name in _NEW_TABLES:
            has_direct = bool(
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
                            OR pg_catalog.has_any_column_privilege(
                                :role,:relation,'SELECT'
                            )
                            OR pg_catalog.has_any_column_privilege(
                                :role,:relation,'INSERT'
                            )
                            OR pg_catalog.has_any_column_privilege(
                                :role,:relation,'UPDATE'
                            )
                        """
                    ),
                    {
                        "role": role_name,
                        "relation": f"finance.{table_name}",
                    },
                ).scalar_one()
            )
            if has_direct:
                raise RuntimeError(
                    "PAY-10 direct Finance authority leaked: "
                    f"{role_name} -> finance.{table_name}"
                )


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _require_predecessor(bind)
    _install()
    _post_install_proof(bind)


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _require_reduced_role(bind, _MIGRATION_OWNER, login=True)
    identity = bind.execute(
        sa.text("SELECT session_user::text,current_user::text")
    ).one()
    if tuple(identity) != (_MIGRATION_OWNER, _MIGRATION_OWNER):
        raise RuntimeError("PAY-10 downgrade requires migration_owner")

    # PAY-10 data tables are FORCE RLS and deliberately invisible to
    # migration_owner.  Use only the pre-existing bounded SET edge to the
    # NOLOGIN security owner for evidence inspection, then return to the
    # migration identity before schema teardown.
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        for table_name in _NEW_TABLES:
            if _relation_exists(bind, f"finance.{table_name}"):
                row_count = bind.execute(
                    sa.text(
                        f"SELECT count(*) FROM finance.{table_name}"
                    )
                ).scalar_one()
                if int(row_count) != 0:
                    raise RuntimeError(
                        "PAY-10 refuses populated downgrade: "
                        f"finance.{table_name}"
                    )

        command_evidence = bind.execute(
            sa.text(
                """
                SELECT count(*)
                FROM finance.refund_execution_commands
                WHERE request_sha256 IS NOT NULL
                   OR first_attempted_at IS NOT NULL
                   OR provider_accepted_at IS NOT NULL
                   OR completed_at IS NOT NULL
                """
            )
        ).scalar_one()
        if int(command_evidence) != 0:
            raise RuntimeError(
                "PAY-10 refuses downgrade with refund execution attempt evidence"
            )
    finally:
        op.execute("RESET ROLE")

    op.execute(
        f"""
        DROP TRIGGER IF EXISTS {_EVIDENCE_TRIGGER}
        ON finance.refund_provider_evidence
        """
    )

    for table_name in reversed(_NEW_TABLES):
        op.execute(
            f"DROP POLICY IF EXISTS "
            f"pay10_{table_name}_security_owner_all "
            f"ON finance.{table_name}"
        )

    op.execute(
        """
        REVOKE ALL
        ON TABLE finance.refund_provider_evidence
        FROM app_security_owner
        """
    )
    op.execute(
        """
        REVOKE ALL
        ON TABLE finance.refund_credit_note_links
        FROM app_security_owner
        """
    )
    op.execute(
        """
        REVOKE ALL
        ON TABLE finance.credit_note_series
        FROM app_security_owner
        """
    )

    op.execute("DROP TABLE finance.refund_provider_evidence")
    op.execute("DROP TABLE finance.refund_credit_note_links")
    op.execute("DROP TABLE finance.credit_note_series")

    op.execute(
        "DROP INDEX IF EXISTS finance.uq_pay10_refund_command_provider_ref"
    )
    op.execute(
        """
        ALTER TABLE finance.refund_execution_commands
        DROP CONSTRAINT chk_pay10_refund_command_completion_timestamps,
        DROP CONSTRAINT chk_pay10_refund_command_attempt_timestamps,
        DROP CONSTRAINT chk_pay10_refund_command_request_hash,
        DROP COLUMN completed_at,
        DROP COLUMN provider_accepted_at,
        DROP COLUMN first_attempted_at,
        DROP COLUMN request_sha256
        """
    )
    op.execute(
        """
        ALTER TABLE finance.credit_notes
        DROP CONSTRAINT uq_pay10_credit_notes_id_org
        """
    )
    op.execute(
        """
        ALTER TABLE finance.refunds
        DROP CONSTRAINT uq_pay10_refunds_id_org
        """
    )

                   OR v_error ~ '(bearer|secret|token)'
                THEN
                    RAISE EXCEPTION
                        'PAY-10 failure error code must be bounded machine token'
                        USING ERRCODE='22023';
                END IF;

                SELECT c.* INTO v_command
                FROM finance.refund_execution_commands c
                WHERE c.command_id=p_command_id
                  AND c.status='processing'
                  AND c.leased_by=p_worker_id
                  AND c.lease_fence=p_lease_fence
                  AND c.leased_until>pg_catalog.clock_timestamp()
                FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'PAY-10 stale refund execution fence'
                        USING ERRCODE='40001';
                END IF;

                v_next_status:=CASE
                    WHEN coalesce(p_permanent,false)
                         OR v_command.attempt_count>=v_command.max_attempts
                    THEN 'dead_lettered'
                    ELSE 'retry_pending'
                END;

                UPDATE finance.refund_execution_commands AS c
                SET
                    status=v_next_status,
                    leased_by=NULL,
                    leased_until=NULL,
                    process_after=CASE
                        WHEN v_next_status='retry_pending'
                        THEN pg_catalog.clock_timestamp()+(
                            least(
                                1800,
                                30 * (
                                    2 ^ greatest(
                                        v_command.attempt_count-1,
                                        0
                                    )
                                )
                            ) * interval '1 second'
                        )
                        ELSE c.process_after
                    END,
                    last_error_code=v_error,
                    updated_at=pg_catalog.clock_timestamp()
                WHERE c.command_id=v_command.command_id;
                RETURN v_next_status;
            END
            $function$
            """
        )

        op.execute(
            """
            CREATE FUNCTION app_secure.record_pay10_refund_external_evidence(
                p_command_id uuid,
                p_provider_payment_ref text,
                p_provider_event_id text,
                p_provider_refund_ref text,
                p_evidence_source text,
                p_normalized_status text,
                p_evidence_sha256 text,
                p_occurred_at timestamptz DEFAULT NULL
            )
            RETURNS TABLE(
                evidence_id uuid,
                command_id uuid,
                normalized_status text,
                command_status text,
                replayed boolean
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_payment finance.payments%ROWTYPE;
                v_refund finance.refunds%ROWTYPE;
                v_command finance.refund_execution_commands%ROWTYPE;
                v_existing finance.refund_provider_evidence%ROWTYPE;
                v_evidence finance.refund_provider_evidence%ROWTYPE;
                v_has_processed boolean;
                v_next_status text;
            BEGIN
                IF NOT pg_catalog.pg_has_role(
                    session_user,
                    'finance_reconciliation_runtime',
                    'MEMBER'
                ) THEN
                    RAISE EXCEPTION
                        'PAY-10 external evidence requires reconciliation runtime'
                        USING ERRCODE='42501';
                END IF;
                IF p_evidence_source NOT IN (
                    'webhook','reconciliation'
                )
                   OR p_normalized_status NOT IN (
                       'pending','processed','failed'
                   )
                   OR p_evidence_sha256 IS NULL
                   OR p_evidence_sha256 !~ '^[0-9a-f]{64}
    for table_name in _NEW_TABLES:
        row = bind.execute(
            sa.text(
                """
                SELECT c.relrowsecurity,c.relforcerowsecurity
                FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='finance'
                  AND c.relname=:table_name
                """
            ),
            {"table_name": table_name},
        ).one()
        if tuple(bool(v) for v in row) != (True, True):
            raise RuntimeError(
                f"PAY-10 RLS posture drift: finance.{table_name}"
            )

    trigger_count = bind.execute(
        sa.text(
            """
            SELECT count(*)
            FROM pg_catalog.pg_trigger t
            JOIN pg_catalog.pg_class c ON c.oid=t.tgrelid
            JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='finance'
              AND c.relname='refund_provider_evidence'
              AND t.tgname=:trigger_name
              AND NOT t.tgisinternal
              AND t.tgenabled='O'
            """
        ),
        {"trigger_name": _EVIDENCE_TRIGGER},
    ).scalar_one()
    if int(trigger_count) != 1:
        raise RuntimeError(
            "PAY-10 immutable refund provider evidence trigger missing"
        )

    if bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.has_table_privilege("
                "'app_security_owner',"
                "'finance.refund_provider_evidence','TRIGGER')"
            )
        ).scalar_one()
    ):
        raise RuntimeError(
            "PAY-10 installation-only TRIGGER authority leaked"
        )

    for role_name in _RUNTIME_ROLES:
        for table_name in _NEW_TABLES:
            has_direct = bool(
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
                            OR pg_catalog.has_any_column_privilege(
                                :role,:relation,'SELECT'
                            )
                            OR pg_catalog.has_any_column_privilege(
                                :role,:relation,'INSERT'
                            )
                            OR pg_catalog.has_any_column_privilege(
                                :role,:relation,'UPDATE'
                            )
                        """
                    ),
                    {
                        "role": role_name,
                        "relation": f"finance.{table_name}",
                    },
                ).scalar_one()
            )
            if has_direct:
                raise RuntimeError(
                    "PAY-10 direct Finance authority leaked: "
                    f"{role_name} -> finance.{table_name}"
                )


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _require_predecessor(bind)
    _install()
    _post_install_proof(bind)


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _require_reduced_role(bind, _MIGRATION_OWNER, login=True)
    identity = bind.execute(
        sa.text("SELECT session_user::text,current_user::text")
    ).one()
    if tuple(identity) != (_MIGRATION_OWNER, _MIGRATION_OWNER):
        raise RuntimeError("PAY-10 downgrade requires migration_owner")

    # PAY-10 data tables are FORCE RLS and deliberately invisible to
    # migration_owner.  Use only the pre-existing bounded SET edge to the
    # NOLOGIN security owner for evidence inspection, then return to the
    # migration identity before schema teardown.
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        for table_name in _NEW_TABLES:
            if _relation_exists(bind, f"finance.{table_name}"):
                row_count = bind.execute(
                    sa.text(
                        f"SELECT count(*) FROM finance.{table_name}"
                    )
                ).scalar_one()
                if int(row_count) != 0:
                    raise RuntimeError(
                        "PAY-10 refuses populated downgrade: "
                        f"finance.{table_name}"
                    )

        command_evidence = bind.execute(
            sa.text(
                """
                SELECT count(*)
                FROM finance.refund_execution_commands
                WHERE request_sha256 IS NOT NULL
                   OR first_attempted_at IS NOT NULL
                   OR provider_accepted_at IS NOT NULL
                   OR completed_at IS NOT NULL
                """
            )
        ).scalar_one()
        if int(command_evidence) != 0:
            raise RuntimeError(
                "PAY-10 refuses downgrade with refund execution attempt evidence"
            )
    finally:
        op.execute("RESET ROLE")

    op.execute(
        f"""
        DROP TRIGGER IF EXISTS {_EVIDENCE_TRIGGER}
        ON finance.refund_provider_evidence
        """
    )

    for table_name in reversed(_NEW_TABLES):
        op.execute(
            f"DROP POLICY IF EXISTS "
            f"pay10_{table_name}_security_owner_all "
            f"ON finance.{table_name}"
        )

    op.execute(
        """
        REVOKE ALL
        ON TABLE finance.refund_provider_evidence
        FROM app_security_owner
        """
    )
    op.execute(
        """
        REVOKE ALL
        ON TABLE finance.refund_credit_note_links
        FROM app_security_owner
        """
    )
    op.execute(
        """
        REVOKE ALL
        ON TABLE finance.credit_note_series
        FROM app_security_owner
        """
    )

    op.execute("DROP TABLE finance.refund_provider_evidence")
    op.execute("DROP TABLE finance.refund_credit_note_links")
    op.execute("DROP TABLE finance.credit_note_series")

    op.execute(
        "DROP INDEX IF EXISTS finance.uq_pay10_refund_command_provider_ref"
    )
    op.execute(
        """
        ALTER TABLE finance.refund_execution_commands
        DROP CONSTRAINT chk_pay10_refund_command_completion_timestamps,
        DROP CONSTRAINT chk_pay10_refund_command_attempt_timestamps,
        DROP CONSTRAINT chk_pay10_refund_command_request_hash,
        DROP COLUMN completed_at,
        DROP COLUMN provider_accepted_at,
        DROP COLUMN first_attempted_at,
        DROP COLUMN request_sha256
        """
    )
    op.execute(
        """
        ALTER TABLE finance.credit_notes
        DROP CONSTRAINT uq_pay10_credit_notes_id_org
        """
    )
    op.execute(
        """
        ALTER TABLE finance.refunds
        DROP CONSTRAINT uq_pay10_refunds_id_org
        """
    )

                THEN
                    RAISE EXCEPTION
                        'PAY-10 external evidence shape invalid'
                        USING ERRCODE='22023';
                END IF;
                IF p_evidence_source='webhook'
                   AND (
                       p_provider_event_id IS NULL
                       OR btrim(p_provider_event_id)=''
                       OR char_length(p_provider_event_id)>200
                   )
                THEN
                    RAISE EXCEPTION
                        'PAY-10 webhook evidence requires event identity'
                        USING ERRCODE='22023';
                END IF;
                IF p_provider_event_id IS NOT NULL
                   AND (
                       char_length(p_provider_event_id)>200
                       OR p_provider_event_id
                            !~ '^[A-Za-z0-9_.:-]+
    for table_name in _NEW_TABLES:
        row = bind.execute(
            sa.text(
                """
                SELECT c.relrowsecurity,c.relforcerowsecurity
                FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='finance'
                  AND c.relname=:table_name
                """
            ),
            {"table_name": table_name},
        ).one()
        if tuple(bool(v) for v in row) != (True, True):
            raise RuntimeError(
                f"PAY-10 RLS posture drift: finance.{table_name}"
            )

    trigger_count = bind.execute(
        sa.text(
            """
            SELECT count(*)
            FROM pg_catalog.pg_trigger t
            JOIN pg_catalog.pg_class c ON c.oid=t.tgrelid
            JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='finance'
              AND c.relname='refund_provider_evidence'
              AND t.tgname=:trigger_name
              AND NOT t.tgisinternal
              AND t.tgenabled='O'
            """
        ),
        {"trigger_name": _EVIDENCE_TRIGGER},
    ).scalar_one()
    if int(trigger_count) != 1:
        raise RuntimeError(
            "PAY-10 immutable refund provider evidence trigger missing"
        )

    if bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.has_table_privilege("
                "'app_security_owner',"
                "'finance.refund_provider_evidence','TRIGGER')"
            )
        ).scalar_one()
    ):
        raise RuntimeError(
            "PAY-10 installation-only TRIGGER authority leaked"
        )

    for role_name in _RUNTIME_ROLES:
        for table_name in _NEW_TABLES:
            has_direct = bool(
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
                            OR pg_catalog.has_any_column_privilege(
                                :role,:relation,'SELECT'
                            )
                            OR pg_catalog.has_any_column_privilege(
                                :role,:relation,'INSERT'
                            )
                            OR pg_catalog.has_any_column_privilege(
                                :role,:relation,'UPDATE'
                            )
                        """
                    ),
                    {
                        "role": role_name,
                        "relation": f"finance.{table_name}",
                    },
                ).scalar_one()
            )
            if has_direct:
                raise RuntimeError(
                    "PAY-10 direct Finance authority leaked: "
                    f"{role_name} -> finance.{table_name}"
                )


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _require_predecessor(bind)
    _install()
    _post_install_proof(bind)


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _require_reduced_role(bind, _MIGRATION_OWNER, login=True)
    identity = bind.execute(
        sa.text("SELECT session_user::text,current_user::text")
    ).one()
    if tuple(identity) != (_MIGRATION_OWNER, _MIGRATION_OWNER):
        raise RuntimeError("PAY-10 downgrade requires migration_owner")

    # PAY-10 data tables are FORCE RLS and deliberately invisible to
    # migration_owner.  Use only the pre-existing bounded SET edge to the
    # NOLOGIN security owner for evidence inspection, then return to the
    # migration identity before schema teardown.
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        for table_name in _NEW_TABLES:
            if _relation_exists(bind, f"finance.{table_name}"):
                row_count = bind.execute(
                    sa.text(
                        f"SELECT count(*) FROM finance.{table_name}"
                    )
                ).scalar_one()
                if int(row_count) != 0:
                    raise RuntimeError(
                        "PAY-10 refuses populated downgrade: "
                        f"finance.{table_name}"
                    )

        command_evidence = bind.execute(
            sa.text(
                """
                SELECT count(*)
                FROM finance.refund_execution_commands
                WHERE request_sha256 IS NOT NULL
                   OR first_attempted_at IS NOT NULL
                   OR provider_accepted_at IS NOT NULL
                   OR completed_at IS NOT NULL
                """
            )
        ).scalar_one()
        if int(command_evidence) != 0:
            raise RuntimeError(
                "PAY-10 refuses downgrade with refund execution attempt evidence"
            )
    finally:
        op.execute("RESET ROLE")

    op.execute(
        f"""
        DROP TRIGGER IF EXISTS {_EVIDENCE_TRIGGER}
        ON finance.refund_provider_evidence
        """
    )

    for table_name in reversed(_NEW_TABLES):
        op.execute(
            f"DROP POLICY IF EXISTS "
            f"pay10_{table_name}_security_owner_all "
            f"ON finance.{table_name}"
        )

    op.execute(
        """
        REVOKE ALL
        ON TABLE finance.refund_provider_evidence
        FROM app_security_owner
        """
    )
    op.execute(
        """
        REVOKE ALL
        ON TABLE finance.refund_credit_note_links
        FROM app_security_owner
        """
    )
    op.execute(
        """
        REVOKE ALL
        ON TABLE finance.credit_note_series
        FROM app_security_owner
        """
    )

    op.execute("DROP TABLE finance.refund_provider_evidence")
    op.execute("DROP TABLE finance.refund_credit_note_links")
    op.execute("DROP TABLE finance.credit_note_series")

    op.execute(
        "DROP INDEX IF EXISTS finance.uq_pay10_refund_command_provider_ref"
    )
    op.execute(
        """
        ALTER TABLE finance.refund_execution_commands
        DROP CONSTRAINT chk_pay10_refund_command_completion_timestamps,
        DROP CONSTRAINT chk_pay10_refund_command_attempt_timestamps,
        DROP CONSTRAINT chk_pay10_refund_command_request_hash,
        DROP COLUMN completed_at,
        DROP COLUMN provider_accepted_at,
        DROP COLUMN first_attempted_at,
        DROP COLUMN request_sha256
        """
    )
    op.execute(
        """
        ALTER TABLE finance.credit_notes
        DROP CONSTRAINT uq_pay10_credit_notes_id_org
        """
    )
    op.execute(
        """
        ALTER TABLE finance.refunds
        DROP CONSTRAINT uq_pay10_refunds_id_org
        """
    )

                   )
                THEN
                    RAISE EXCEPTION
                        'PAY-10 provider event identity invalid'
                        USING ERRCODE='22023';
                END IF;
                IF p_provider_refund_ref IS NOT NULL
                   AND (
                       char_length(p_provider_refund_ref)>200
                       OR p_provider_refund_ref
                            !~ '^[A-Za-z0-9_-]+
    for table_name in _NEW_TABLES:
        row = bind.execute(
            sa.text(
                """
                SELECT c.relrowsecurity,c.relforcerowsecurity
                FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='finance'
                  AND c.relname=:table_name
                """
            ),
            {"table_name": table_name},
        ).one()
        if tuple(bool(v) for v in row) != (True, True):
            raise RuntimeError(
                f"PAY-10 RLS posture drift: finance.{table_name}"
            )

    trigger_count = bind.execute(
        sa.text(
            """
            SELECT count(*)
            FROM pg_catalog.pg_trigger t
            JOIN pg_catalog.pg_class c ON c.oid=t.tgrelid
            JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='finance'
              AND c.relname='refund_provider_evidence'
              AND t.tgname=:trigger_name
              AND NOT t.tgisinternal
              AND t.tgenabled='O'
            """
        ),
        {"trigger_name": _EVIDENCE_TRIGGER},
    ).scalar_one()
    if int(trigger_count) != 1:
        raise RuntimeError(
            "PAY-10 immutable refund provider evidence trigger missing"
        )

    if bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.has_table_privilege("
                "'app_security_owner',"
                "'finance.refund_provider_evidence','TRIGGER')"
            )
        ).scalar_one()
    ):
        raise RuntimeError(
            "PAY-10 installation-only TRIGGER authority leaked"
        )

    for role_name in _RUNTIME_ROLES:
        for table_name in _NEW_TABLES:
            has_direct = bool(
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
                            OR pg_catalog.has_any_column_privilege(
                                :role,:relation,'SELECT'
                            )
                            OR pg_catalog.has_any_column_privilege(
                                :role,:relation,'INSERT'
                            )
                            OR pg_catalog.has_any_column_privilege(
                                :role,:relation,'UPDATE'
                            )
                        """
                    ),
                    {
                        "role": role_name,
                        "relation": f"finance.{table_name}",
                    },
                ).scalar_one()
            )
            if has_direct:
                raise RuntimeError(
                    "PAY-10 direct Finance authority leaked: "
                    f"{role_name} -> finance.{table_name}"
                )


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _require_predecessor(bind)
    _install()
    _post_install_proof(bind)


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _require_reduced_role(bind, _MIGRATION_OWNER, login=True)
    identity = bind.execute(
        sa.text("SELECT session_user::text,current_user::text")
    ).one()
    if tuple(identity) != (_MIGRATION_OWNER, _MIGRATION_OWNER):
        raise RuntimeError("PAY-10 downgrade requires migration_owner")

    # PAY-10 data tables are FORCE RLS and deliberately invisible to
    # migration_owner.  Use only the pre-existing bounded SET edge to the
    # NOLOGIN security owner for evidence inspection, then return to the
    # migration identity before schema teardown.
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        for table_name in _NEW_TABLES:
            if _relation_exists(bind, f"finance.{table_name}"):
                row_count = bind.execute(
                    sa.text(
                        f"SELECT count(*) FROM finance.{table_name}"
                    )
                ).scalar_one()
                if int(row_count) != 0:
                    raise RuntimeError(
                        "PAY-10 refuses populated downgrade: "
                        f"finance.{table_name}"
                    )

        command_evidence = bind.execute(
            sa.text(
                """
                SELECT count(*)
                FROM finance.refund_execution_commands
                WHERE request_sha256 IS NOT NULL
                   OR first_attempted_at IS NOT NULL
                   OR provider_accepted_at IS NOT NULL
                   OR completed_at IS NOT NULL
                """
            )
        ).scalar_one()
        if int(command_evidence) != 0:
            raise RuntimeError(
                "PAY-10 refuses downgrade with refund execution attempt evidence"
            )
    finally:
        op.execute("RESET ROLE")

    op.execute(
        f"""
        DROP TRIGGER IF EXISTS {_EVIDENCE_TRIGGER}
        ON finance.refund_provider_evidence
        """
    )

    for table_name in reversed(_NEW_TABLES):
        op.execute(
            f"DROP POLICY IF EXISTS "
            f"pay10_{table_name}_security_owner_all "
            f"ON finance.{table_name}"
        )

    op.execute(
        """
        REVOKE ALL
        ON TABLE finance.refund_provider_evidence
        FROM app_security_owner
        """
    )
    op.execute(
        """
        REVOKE ALL
        ON TABLE finance.refund_credit_note_links
        FROM app_security_owner
        """
    )
    op.execute(
        """
        REVOKE ALL
        ON TABLE finance.credit_note_series
        FROM app_security_owner
        """
    )

    op.execute("DROP TABLE finance.refund_provider_evidence")
    op.execute("DROP TABLE finance.refund_credit_note_links")
    op.execute("DROP TABLE finance.credit_note_series")

    op.execute(
        "DROP INDEX IF EXISTS finance.uq_pay10_refund_command_provider_ref"
    )
    op.execute(
        """
        ALTER TABLE finance.refund_execution_commands
        DROP CONSTRAINT chk_pay10_refund_command_completion_timestamps,
        DROP CONSTRAINT chk_pay10_refund_command_attempt_timestamps,
        DROP CONSTRAINT chk_pay10_refund_command_request_hash,
        DROP COLUMN completed_at,
        DROP COLUMN provider_accepted_at,
        DROP COLUMN first_attempted_at,
        DROP COLUMN request_sha256
        """
    )
    op.execute(
        """
        ALTER TABLE finance.credit_notes
        DROP CONSTRAINT uq_pay10_credit_notes_id_org
        """
    )
    op.execute(
        """
        ALTER TABLE finance.refunds
        DROP CONSTRAINT uq_pay10_refunds_id_org
        """
    )

                   )
                THEN
                    RAISE EXCEPTION
                        'PAY-10 external provider refund reference invalid'
                        USING ERRCODE='22023';
                END IF;
                IF p_normalized_status<>'failed'
                   AND p_provider_refund_ref IS NULL
                THEN
                    RAISE EXCEPTION
                        'PAY-10 accepted external evidence requires refund reference'
                        USING ERRCODE='22023';
                END IF;

                SELECT p.* INTO v_payment
                FROM finance.payments p
                JOIN finance.refund_execution_commands c
                  ON c.payment_id=p.id
                WHERE c.command_id=p_command_id
                FOR UPDATE OF p;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'PAY-10 external evidence payment not found'
                        USING ERRCODE='P0002';
                END IF;
                SELECT r.* INTO v_refund
                FROM finance.refunds r
                JOIN finance.refund_execution_commands c
                  ON c.refund_id=r.id
                WHERE c.command_id=p_command_id
                FOR UPDATE OF r;
                SELECT c.* INTO v_command
                FROM finance.refund_execution_commands c
                WHERE c.command_id=p_command_id
                FOR UPDATE;

                IF NOT FOUND
                   OR v_command.request_sha256 IS NULL
                   OR v_command.provider_code IS NULL
                   OR v_command.provider_code<>v_payment.provider_code
                   OR v_payment.provider_payment_ref
                        IS DISTINCT FROM p_provider_payment_ref
                   OR v_command.payment_id<>v_payment.id
                   OR v_command.refund_id<>v_refund.id
                   OR v_command.organization_id
                        IS DISTINCT FROM v_payment.organization_id
                   OR v_refund.organization_id
                        IS DISTINCT FROM v_payment.organization_id
                THEN
                    RAISE EXCEPTION
                        'PAY-10 external evidence authority mismatch'
                        USING ERRCODE='23514';
                END IF;
                IF v_command.status='processing'
                   AND v_command.leased_until>pg_catalog.clock_timestamp()
                THEN
                    RAISE EXCEPTION
                        'PAY-10 active refund lease blocks reconciliation'
                        USING ERRCODE='40001';
                END IF;
                IF v_command.status NOT IN (
                    'processing',
                    'provider_accepted',
                    'reconciliation_pending',
                    'rejected',
                    'dead_lettered',
                    'succeeded'
                )
                THEN
                    RAISE EXCEPTION
                        'PAY-10 command is not reconcilable'
                        USING ERRCODE='23514';
                END IF;
                IF v_command.provider_refund_ref IS NOT NULL
                   AND p_provider_refund_ref IS NOT NULL
                   AND v_command.provider_refund_ref
                        <>p_provider_refund_ref
                THEN
                    RAISE EXCEPTION
                        'PAY-10 external provider refund identity drift'
                        USING ERRCODE='23514';
                END IF;

                IF p_provider_event_id IS NOT NULL THEN
                    SELECT e.* INTO v_existing
                    FROM finance.refund_provider_evidence e
                    WHERE e.provider_code=v_command.provider_code
                      AND e.provider_event_id=p_provider_event_id;
                    IF FOUND THEN
                        IF v_existing.command_id<>v_command.command_id
                           OR v_existing.payment_id<>v_command.payment_id
                           OR v_existing.organization_id
                                <>v_command.organization_id
                           OR v_existing.provider_refund_ref
                                IS DISTINCT FROM p_provider_refund_ref
                           OR v_existing.evidence_source
                                <>p_evidence_source
                           OR v_existing.normalized_status
                                <>p_normalized_status
                           OR v_existing.evidence_sha256
                                <>p_evidence_sha256
                           OR v_existing.occurred_at
                                IS DISTINCT FROM p_occurred_at
                        THEN
                            RAISE EXCEPTION
                                'PAY-10 conflicting provider event replay'
                                USING ERRCODE='23505';
                        END IF;
                        RETURN QUERY SELECT
                            v_existing.id,
                            v_existing.command_id,
                            v_existing.normalized_status::text,
                            v_command.status::text,
                            true;
                        RETURN;
                    END IF;
                END IF;

                SELECT e.* INTO v_existing
                FROM finance.refund_provider_evidence e
                WHERE e.command_id=v_command.command_id
                  AND e.evidence_sha256=p_evidence_sha256;
                IF FOUND THEN
                    IF v_existing.provider_event_id
                            IS DISTINCT FROM p_provider_event_id
                       OR v_existing.provider_refund_ref
                            IS DISTINCT FROM p_provider_refund_ref
                       OR v_existing.evidence_source<>p_evidence_source
                       OR v_existing.normalized_status<>p_normalized_status
                       OR v_existing.occurred_at
                            IS DISTINCT FROM p_occurred_at
                    THEN
                        RAISE EXCEPTION
                            'PAY-10 conflicting external evidence replay'
                            USING ERRCODE='23505';
                    END IF;
                    RETURN QUERY SELECT
                        v_existing.id,
                        v_existing.command_id,
                        v_existing.normalized_status::text,
                        v_command.status::text,
                        true;
                    RETURN;
                END IF;

                INSERT INTO finance.refund_provider_evidence(
                    command_id,
                    refund_id,
                    payment_id,
                    organization_id,
                    provider_code,
                    provider_event_id,
                    provider_refund_ref,
                    evidence_source,
                    normalized_status,
                    request_sha256,
                    evidence_sha256,
                    occurred_at
                )
                VALUES(
                    v_command.command_id,
                    v_command.refund_id,
                    v_command.payment_id,
                    v_command.organization_id,
                    v_command.provider_code,
                    p_provider_event_id,
                    p_provider_refund_ref,
                    p_evidence_source,
                    p_normalized_status,
                    v_command.request_sha256,
                    p_evidence_sha256,
                    p_occurred_at
                )
                RETURNING * INTO v_evidence;

                SELECT EXISTS(
                    SELECT 1
                    FROM finance.refund_provider_evidence e
                    WHERE e.command_id=v_command.command_id
                      AND e.normalized_status='processed'
                )
                INTO v_has_processed;

                v_next_status:=CASE
                    WHEN v_command.status='succeeded'
                    THEN 'succeeded'
                    WHEN v_has_processed
                    THEN 'reconciliation_pending'
                    WHEN p_normalized_status='failed'
                    THEN 'rejected'
                    WHEN v_command.status='reconciliation_pending'
                    THEN 'reconciliation_pending'
                    ELSE 'provider_accepted'
                END;

                UPDATE finance.refund_execution_commands AS c
                SET
                    status=v_next_status,
                    leased_by=NULL,
                    leased_until=NULL,
                    provider_refund_ref=coalesce(
                        c.provider_refund_ref,
                        p_provider_refund_ref
                    ),
                    provider_evidence_sha256=CASE
                        WHEN p_normalized_status='processed'
                             OR c.provider_evidence_sha256 IS NULL
                        THEN p_evidence_sha256
                        ELSE c.provider_evidence_sha256
                    END,
                    provider_accepted_at=CASE
                        WHEN p_normalized_status IN (
                            'pending','processed'
                        )
                        THEN coalesce(
                            c.provider_accepted_at,
                            pg_catalog.clock_timestamp()
                        )
                        ELSE c.provider_accepted_at
                    END,
                    last_error_code=CASE
                        WHEN v_next_status='rejected'
                        THEN 'provider_failed'
                        WHEN v_next_status='succeeded'
                        THEN c.last_error_code
                        ELSE NULL
                    END,
                    updated_at=pg_catalog.clock_timestamp()
                WHERE c.command_id=v_command.command_id;

                RETURN QUERY SELECT
                    v_evidence.id,
                    v_evidence.command_id,
                    v_evidence.normalized_status::text,
                    v_next_status,
                    false;
            END
            $function$
            """
        )

        for signature in _PAY10_FUNCTIONS:
            op.execute(
                f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC"
            )
        op.execute(
            "GRANT USAGE ON SCHEMA app_secure "
            "TO finance_refund_runtime"
        )
        for signature in (
            _CLAIM_REFUND,
            _BIND_REQUEST,
            _RECORD_OUTCOME,
            _RECORD_UNKNOWN,
            _RECORD_FAILURE,
        ):
            op.execute(
                f"GRANT EXECUTE ON FUNCTION {signature} "
                "TO finance_refund_runtime"
            )
        op.execute(
            f"GRANT EXECUTE ON FUNCTION {_RECORD_EXTERNAL} "
            "TO finance_reconciliation_runtime"
        )
    finally:
        op.execute("RESET ROLE")
    op.execute(
        "REVOKE CREATE ON SCHEMA app_secure FROM app_security_owner"
    )


def _post_install_proof(bind) -> None:
    for table_name in _NEW_TABLES:
        row = bind.execute(
            sa.text(
                """
                SELECT c.relrowsecurity,c.relforcerowsecurity
                FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='finance'
                  AND c.relname=:table_name
                """
            ),
            {"table_name": table_name},
        ).one()
        if tuple(bool(v) for v in row) != (True, True):
            raise RuntimeError(
                f"PAY-10 RLS posture drift: finance.{table_name}"
            )

    trigger_count = bind.execute(
        sa.text(
            """
            SELECT count(*)
            FROM pg_catalog.pg_trigger t
            JOIN pg_catalog.pg_class c ON c.oid=t.tgrelid
            JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='finance'
              AND c.relname='refund_provider_evidence'
              AND t.tgname=:trigger_name
              AND NOT t.tgisinternal
              AND t.tgenabled='O'
            """
        ),
        {"trigger_name": _EVIDENCE_TRIGGER},
    ).scalar_one()
    if int(trigger_count) != 1:
        raise RuntimeError(
            "PAY-10 immutable refund provider evidence trigger missing"
        )

    if bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.has_table_privilege("
                "'app_security_owner',"
                "'finance.refund_provider_evidence','TRIGGER')"
            )
        ).scalar_one()
    ):
        raise RuntimeError(
            "PAY-10 installation-only TRIGGER authority leaked"
        )

    for role_name in _RUNTIME_ROLES:
        for table_name in _NEW_TABLES:
            has_direct = bool(
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
                            OR pg_catalog.has_any_column_privilege(
                                :role,:relation,'SELECT'
                            )
                            OR pg_catalog.has_any_column_privilege(
                                :role,:relation,'INSERT'
                            )
                            OR pg_catalog.has_any_column_privilege(
                                :role,:relation,'UPDATE'
                            )
                        """
                    ),
                    {
                        "role": role_name,
                        "relation": f"finance.{table_name}",
                    },
                ).scalar_one()
            )
            if has_direct:
                raise RuntimeError(
                    "PAY-10 direct Finance authority leaked: "
                    f"{role_name} -> finance.{table_name}"
                )


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _require_predecessor(bind)
    _install()
    _post_install_proof(bind)


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _require_reduced_role(bind, _MIGRATION_OWNER, login=True)
    identity = bind.execute(
        sa.text("SELECT session_user::text,current_user::text")
    ).one()
    if tuple(identity) != (_MIGRATION_OWNER, _MIGRATION_OWNER):
        raise RuntimeError("PAY-10 downgrade requires migration_owner")

    # PAY-10 data tables are FORCE RLS and deliberately invisible to
    # migration_owner.  Use only the pre-existing bounded SET edge to the
    # NOLOGIN security owner for evidence inspection, then return to the
    # migration identity before schema teardown.
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        for table_name in _NEW_TABLES:
            if _relation_exists(bind, f"finance.{table_name}"):
                row_count = bind.execute(
                    sa.text(
                        f"SELECT count(*) FROM finance.{table_name}"
                    )
                ).scalar_one()
                if int(row_count) != 0:
                    raise RuntimeError(
                        "PAY-10 refuses populated downgrade: "
                        f"finance.{table_name}"
                    )

        command_evidence = bind.execute(
            sa.text(
                """
                SELECT count(*)
                FROM finance.refund_execution_commands
                WHERE request_sha256 IS NOT NULL
                   OR first_attempted_at IS NOT NULL
                   OR provider_accepted_at IS NOT NULL
                   OR completed_at IS NOT NULL
                """
            )
        ).scalar_one()
        if int(command_evidence) != 0:
            raise RuntimeError(
                "PAY-10 refuses downgrade with refund execution attempt evidence"
            )
    finally:
        op.execute("RESET ROLE")

    op.execute(
        f"""
        DROP TRIGGER IF EXISTS {_EVIDENCE_TRIGGER}
        ON finance.refund_provider_evidence
        """
    )

    for table_name in reversed(_NEW_TABLES):
        op.execute(
            f"DROP POLICY IF EXISTS "
            f"pay10_{table_name}_security_owner_all "
            f"ON finance.{table_name}"
        )

    op.execute(
        """
        REVOKE ALL
        ON TABLE finance.refund_provider_evidence
        FROM app_security_owner
        """
    )
    op.execute(
        """
        REVOKE ALL
        ON TABLE finance.refund_credit_note_links
        FROM app_security_owner
        """
    )
    op.execute(
        """
        REVOKE ALL
        ON TABLE finance.credit_note_series
        FROM app_security_owner
        """
    )

    op.execute("DROP TABLE finance.refund_provider_evidence")
    op.execute("DROP TABLE finance.refund_credit_note_links")
    op.execute("DROP TABLE finance.credit_note_series")

    op.execute(
        "DROP INDEX IF EXISTS finance.uq_pay10_refund_command_provider_ref"
    )
    op.execute(
        """
        ALTER TABLE finance.refund_execution_commands
        DROP CONSTRAINT chk_pay10_refund_command_completion_timestamps,
        DROP CONSTRAINT chk_pay10_refund_command_attempt_timestamps,
        DROP CONSTRAINT chk_pay10_refund_command_request_hash,
        DROP COLUMN completed_at,
        DROP COLUMN provider_accepted_at,
        DROP COLUMN first_attempted_at,
        DROP COLUMN request_sha256
        """
    )
    op.execute(
        """
        ALTER TABLE finance.credit_notes
        DROP CONSTRAINT uq_pay10_credit_notes_id_org
        """
    )
    op.execute(
        """
        ALTER TABLE finance.refunds
        DROP CONSTRAINT uq_pay10_refunds_id_org
        """
    )
