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

    if not bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.has_table_privilege("
                "'app_security_owner',"
                "'finance.refund_execution_commands',"
                "'SELECT,INSERT,UPDATE')"
            )
        ).scalar_one()
    ):
        raise RuntimeError(
            "PAY-10 requires inherited P4D refund command owner authority"
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
