"""PAY-24-A durable payment activation authority.

Revision ID: zz57d8e9f0a65
Revises: zz47d8e9f0a64
Create Date: 2026-09-24

This revision installs the fail-closed PostgreSQL control plane used by later
PAY-24 slices.  It does not authorize Stage 1 and it does not perform provider
I/O.  All mutable authority is held in one generation-fenced singleton row;
release measurements and human authorizations are immutable; every authority
change is paired transactionally with hash-chained evidence; and provider
admissions are durable, single-logical-operation leases.

Downgrade is deliberately possible only from the pristine generation-zero
Stage-0 posture with no release, authorization, transition, or admission
history.  Evidence and provider-operation history are never erased by a
downgrade.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "zz57d8e9f0a65"
down_revision = "zz47d8e9f0a64"
branch_labels = None
depends_on = None


_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_CONFIG = "finance_config_runtime"
_PAYMENT = "finance_payment_runtime"
_REFUND = "finance_refund_runtime"
_READ = "finance_read_runtime"
_FINANCE_MAINTENANCE = "finance_maintenance_runtime"
_LIFECYCLE_MAINTENANCE = "lifecycle_maintenance_runtime"

_REQUIRED_ROLES = (
    _MIGRATION_OWNER,
    _SECURITY_OWNER,
    _CONFIG,
    _PAYMENT,
    _REFUND,
    _READ,
    _FINANCE_MAINTENANCE,
    _LIFECYCLE_MAINTENANCE,
    "app_runtime",
    "worker_runtime",
)

_TABLES = (
    "payment_activation_release_identities",
    "payment_activation_authorizations",
    "payment_activation_authority",
    "payment_activation_transition_events",
    "provider_admission_leases",
)

_FUNCTION_SIGNATURES = (
    "app_secure.pay24a_compute_posture_digest(bigint,smallint,text,uuid,boolean,boolean,boolean,boolean,boolean,boolean,boolean,boolean,uuid,uuid,text)",
    "app_secure.pay24a_append_transition_event(uuid,text,text,bigint,bigint,smallint,smallint,text,text,text,smallint,text,text,text,text,uuid,jsonb)",
    "app_secure.pay24a_reject_immutable_mutation()",
    "app_secure.pay24a_guard_authority_update()",
    "app_secure.pay24a_enforce_authority_evidence()",
    "app_secure.pay24a_reject_admission_erasure()",
    "app_secure.pay24a_guard_admission_update()",
    "app_secure.pay24a_bind_release_identity(uuid,bigint,text,text,text,timestamp with time zone,text)",
    "app_secure.pay24a_bind_human_authorization(uuid,bigint,text,text,smallint,text,timestamp with time zone,text)",
    "app_secure.pay24a_transition_activation(uuid,bigint,smallint,text,uuid,boolean,boolean,boolean,boolean,boolean,boolean,boolean,boolean,text)",
    "app_secure.pay24a_begin_emergency_rollback(uuid,bigint,text)",
    "app_secure.pay24a_expire_provider_admissions(uuid,text)",
    "app_secure.pay24a_finalize_emergency_rollback(uuid,bigint,text)",
    "app_secure.pay24a_activation_snapshot()",
    "app_secure.pay24a_transition_evidence(bigint,integer)",
    "app_secure.pay24a_request_provider_admission(bigint,text,text,text,integer)",
    "app_secure.pay24a_start_provider_admission(uuid,uuid)",
    "app_secure.pay24a_finish_provider_admission(uuid,uuid,text)",
    "app_secure.pay24a_admission_drain_snapshot()",
)

_CONFIG_FUNCTIONS = (
    _FUNCTION_SIGNATURES[7],
    _FUNCTION_SIGNATURES[8],
    _FUNCTION_SIGNATURES[9],
    _FUNCTION_SIGNATURES[10],
    _FUNCTION_SIGNATURES[12],
)
_ADMISSION_FUNCTIONS = (
    _FUNCTION_SIGNATURES[15],
    _FUNCTION_SIGNATURES[16],
    _FUNCTION_SIGNATURES[17],
)
_READ_FUNCTIONS = (
    _FUNCTION_SIGNATURES[13],
    _FUNCTION_SIGNATURES[14],
    _FUNCTION_SIGNATURES[18],
)
_EXPIRY_FUNCTION = _FUNCTION_SIGNATURES[11]
_MAX_OUTSTANDING_ADMISSIONS = 64


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
        raise RuntimeError(f"PAY-24-A missing externally managed role: {role}")
    if bool(row["rolcanlogin"]) is not login:
        raise RuntimeError(f"PAY-24-A role login posture drift: {role}")
    for attribute in (
        "rolsuper",
        "rolinherit",
        "rolcreatedb",
        "rolcreaterole",
        "rolreplication",
        "rolbypassrls",
    ):
        if bool(row[attribute]):
            raise RuntimeError(
                f"PAY-24-A reduced-role drift: {role}.{attribute}"
            )


def _require_identity(bind) -> None:
    for role in _REQUIRED_ROLES:
        _require_role(bind, role, login=role == _MIGRATION_OWNER)

    identity = bind.execute(
        sa.text("SELECT session_user::text,current_user::text")
    ).one()
    if tuple(identity) != (_MIGRATION_OWNER, _MIGRATION_OWNER):
        raise RuntimeError("PAY-24-A migration requires migration_owner")

    if not bind.execute(
        sa.text(
            "SELECT pg_catalog.pg_has_role(:member,:target,'SET')"
        ),
        {"member": _MIGRATION_OWNER, "target": _SECURITY_OWNER},
    ).scalar_one():
        raise RuntimeError(
            "PAY-24-A requires migration_owner SET edge to app_security_owner"
        )

    isolated = (
        _CONFIG,
        _PAYMENT,
        _REFUND,
        _READ,
        _FINANCE_MAINTENANCE,
        _LIFECYCLE_MAINTENANCE,
        "app_runtime",
        "worker_runtime",
    )
    for role in isolated:
        if bind.execute(
            sa.text(
                "SELECT pg_catalog.pg_has_role(:member,:target,'MEMBER') "
                "OR pg_catalog.pg_has_role(:member,:target,'SET')"
            ),
            {"member": role, "target": _SECURITY_OWNER},
        ).scalar_one():
            raise RuntimeError(
                "PAY-24-A runtime may reach app_security_owner: " + role
            )


def _preflight(bind) -> None:
    if bind.execute(
        sa.text("SELECT pg_catalog.to_regclass('public.organizations') IS NULL")
    ).scalar_one():
        raise RuntimeError("PAY-24-A predecessor organizations table missing")

    for table_name in _TABLES:
        if bind.execute(
            sa.text("SELECT pg_catalog.to_regclass(:name) IS NOT NULL"),
            {"name": f"finance.{table_name}"},
        ).scalar_one():
            raise RuntimeError(
                "PAY-24-A unexpected pre-existing relation: "
                f"finance.{table_name}"
            )

    function_names = tuple(
        signature.split(".", 1)[1].split("(", 1)[0]
        for signature in _FUNCTION_SIGNATURES
    )
    collision = bind.execute(
        sa.text(
            """
            SELECT p.proname
            FROM pg_catalog.pg_proc AS p
            JOIN pg_catalog.pg_namespace AS n ON n.oid=p.pronamespace
            WHERE n.nspname='app_secure'
              AND p.proname = ANY(CAST(:names AS text[]))
            ORDER BY p.proname
            LIMIT 1
            """
        ),
        {"names": list(function_names)},
    ).scalar_one_or_none()
    if collision is not None:
        raise RuntimeError(
            "PAY-24-A unexpected pre-existing function: app_secure."
            + collision
        )

    for required_function in (
        "pg_catalog.gen_random_uuid()",
        "pg_catalog.sha256(bytea)",
    ):
        if bind.execute(
            sa.text("SELECT pg_catalog.to_regprocedure(:name) IS NULL"),
            {"name": required_function},
        ).scalar_one():
            raise RuntimeError(
                "PAY-24-A required PostgreSQL function missing: "
                + required_function
            )

    for role in (
        _SECURITY_OWNER,
        _CONFIG,
        _PAYMENT,
        _REFUND,
        _LIFECYCLE_MAINTENANCE,
    ):
        if not bind.execute(
            sa.text(
                "SELECT pg_catalog.has_schema_privilege("
                ":role,'app_secure','USAGE')"
            ),
            {"role": role},
        ).scalar_one():
            raise RuntimeError(
                "PAY-24-A predecessor app_secure USAGE missing: " + role
            )
    if not bind.execute(
        sa.text(
            "SELECT pg_catalog.has_schema_privilege("
            ":role,'finance','USAGE')"
        ),
        {"role": _SECURITY_OWNER},
    ).scalar_one():
        raise RuntimeError(
            "PAY-24-A predecessor app_security_owner Finance USAGE missing"
        )

    # These two capabilities were reserved but intentionally had no
    # app_secure surface at the immutable base.  Failing on unexpected prior
    # USAGE makes the exact grant/revoke delta safe across downgrade.
    for role in (_READ, _FINANCE_MAINTENANCE):
        if bind.execute(
            sa.text(
                "SELECT pg_catalog.has_schema_privilege("
                ":role,'app_secure','USAGE')"
            ),
            {"role": role},
        ).scalar_one():
            raise RuntimeError(
                "PAY-24-A unexpected pre-existing app_secure USAGE: " + role
            )


def _install_tables() -> None:
    op.execute(
        r"""
        CREATE TABLE finance.payment_activation_release_identities(
            release_identity_id UUID PRIMARY KEY,
            operation_id UUID NOT NULL UNIQUE,
            certified_sha TEXT NOT NULL,
            deployed_sha TEXT NOT NULL,
            measured_by TEXT NOT NULL,
            measured_at TIMESTAMPTZ NOT NULL,
            bound_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT uq_pay24a_release_binding
                UNIQUE(release_identity_id,certified_sha,deployed_sha),
            CONSTRAINT chk_pay24a_release_certified_sha
                CHECK(certified_sha ~ '^[0-9a-f]{40}$'),
            CONSTRAINT chk_pay24a_release_deployed_sha
                CHECK(deployed_sha ~ '^[0-9a-f]{40}$'),
            CONSTRAINT chk_pay24a_release_measurer
                CHECK(
                    char_length(measured_by) BETWEEN 3 AND 128
                    AND measured_by !~ '[[:cntrl:]]'
                ),
            CONSTRAINT chk_pay24a_release_measurement_time
                CHECK(measured_at <= bound_at)
        )
        """
    )
    op.execute(
        r"""
        CREATE TABLE finance.payment_activation_authorizations(
            authorization_record_id UUID PRIMARY KEY,
            operation_id UUID NOT NULL UNIQUE,
            authorization_id TEXT NOT NULL UNIQUE,
            authorized_sha TEXT NOT NULL,
            authorized_stage SMALLINT NOT NULL,
            authorized_by TEXT NOT NULL,
            authorized_at TIMESTAMPTZ NOT NULL,
            bound_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT uq_pay24a_authorization_binding
                UNIQUE(
                    authorization_record_id,
                    authorization_id,
                    authorized_sha,
                    authorized_stage,
                    authorized_by,
                    authorized_at
                ),
            CONSTRAINT chk_pay24a_authorization_id
                CHECK(
                    char_length(authorization_id) BETWEEN 3 AND 128
                    AND authorization_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:/-]{2,127}$'
                ),
            CONSTRAINT chk_pay24a_authorized_sha
                CHECK(authorized_sha ~ '^[0-9a-f]{40}$'),
            CONSTRAINT chk_pay24a_authorized_stage
                CHECK(authorized_stage IN (0,1)),
            CONSTRAINT chk_pay24a_authorized_by
                CHECK(
                    char_length(authorized_by) BETWEEN 3 AND 128
                    AND authorized_by !~ '[[:cntrl:]]'
                ),
            CONSTRAINT chk_pay24a_authorization_time
                CHECK(authorized_at <= bound_at)
        )
        """
    )
    op.execute(
        r"""
        CREATE TABLE finance.payment_activation_authority(
            singleton BOOLEAN PRIMARY KEY DEFAULT true,
            generation BIGINT NOT NULL,
            stage SMALLINT NOT NULL,
            provider_egress_state TEXT NOT NULL,
            release_identity_id UUID NULL,
            certified_sha TEXT NULL,
            deployed_sha TEXT NULL,
            authorization_record_id UUID NULL,
            authorization_id TEXT NULL,
            authorized_sha TEXT NULL,
            authorized_stage SMALLINT NULL,
            authorized_by TEXT NULL,
            authorized_at TIMESTAMPTZ NULL,
            internal_organization_id UUID NULL
                REFERENCES public.organizations(id) ON DELETE RESTRICT,
            checkout BOOLEAN NOT NULL,
            webhooks BOOLEAN NOT NULL,
            payment_application BOOLEAN NOT NULL,
            subscription_activation BOOLEAN NOT NULL,
            refund_execution BOOLEAN NOT NULL,
            recurring_billing BOOLEAN NOT NULL,
            dunning BOOLEAN NOT NULL,
            platform_billing BOOLEAN NOT NULL,
            last_operation_id UUID NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            posture_digest CHAR(64) NOT NULL,
            CONSTRAINT chk_pay24a_authority_singleton CHECK(singleton),
            CONSTRAINT chk_pay24a_authority_generation CHECK(generation >= 0),
            CONSTRAINT chk_pay24a_authority_stage CHECK(stage IN (0,1)),
            CONSTRAINT chk_pay24a_authority_egress
                CHECK(provider_egress_state IN ('blocked','open','closing')),
            CONSTRAINT chk_pay24a_authority_release_shape CHECK(
                (release_identity_id IS NULL
                 AND certified_sha IS NULL
                 AND deployed_sha IS NULL)
                OR
                (release_identity_id IS NOT NULL
                 AND certified_sha IS NOT NULL
                 AND deployed_sha IS NOT NULL
                 AND certified_sha ~ '^[0-9a-f]{40}$'
                 AND deployed_sha ~ '^[0-9a-f]{40}$')
            ),
            CONSTRAINT chk_pay24a_authority_authorization_shape CHECK(
                (authorization_record_id IS NULL
                 AND authorization_id IS NULL
                 AND authorized_sha IS NULL
                 AND authorized_stage IS NULL
                 AND authorized_by IS NULL
                 AND authorized_at IS NULL)
                OR
                (authorization_record_id IS NOT NULL
                 AND authorization_id IS NOT NULL
                 AND authorized_sha IS NOT NULL
                 AND authorized_stage IN (0,1)
                 AND authorized_by IS NOT NULL
                 AND authorized_at IS NOT NULL)
            ),
            CONSTRAINT chk_pay24a_authority_posture_digest
                CHECK(posture_digest ~ '^[0-9a-f]{64}$'),
            CONSTRAINT chk_pay24a_authority_stage_posture CHECK(
                (
                    stage=0
                    AND provider_egress_state='blocked'
                    AND internal_organization_id IS NULL
                    AND NOT checkout
                    AND NOT webhooks
                    AND NOT payment_application
                    AND NOT subscription_activation
                    AND NOT refund_execution
                    AND NOT recurring_billing
                    AND NOT dunning
                    AND NOT platform_billing
                )
                OR
                (
                    stage=1
                    AND provider_egress_state IN ('blocked','open','closing')
                    AND internal_organization_id IS NOT NULL
                    AND release_identity_id IS NOT NULL
                    AND authorization_record_id IS NOT NULL
                    AND certified_sha=deployed_sha
                    AND certified_sha=authorized_sha
                    AND authorized_stage=1
                    AND NOT refund_execution
                    AND NOT recurring_billing
                    AND NOT dunning
                    AND NOT platform_billing
                )
            ),
            CONSTRAINT fk_pay24a_authority_release
                FOREIGN KEY(release_identity_id,certified_sha,deployed_sha)
                REFERENCES finance.payment_activation_release_identities(
                    release_identity_id,certified_sha,deployed_sha
                ) ON DELETE RESTRICT,
            CONSTRAINT fk_pay24a_authority_authorization
                FOREIGN KEY(
                    authorization_record_id,
                    authorization_id,
                    authorized_sha,
                    authorized_stage,
                    authorized_by,
                    authorized_at
                )
                REFERENCES finance.payment_activation_authorizations(
                    authorization_record_id,
                    authorization_id,
                    authorized_sha,
                    authorized_stage,
                    authorized_by,
                    authorized_at
                ) ON DELETE RESTRICT
        )
        """
    )
    op.execute(
        r"""
        CREATE TABLE finance.payment_activation_transition_events(
            event_id UUID PRIMARY KEY,
            event_sequence BIGINT NOT NULL UNIQUE,
            operation_id UUID NOT NULL UNIQUE,
            event_type TEXT NOT NULL,
            request_digest CHAR(64) NOT NULL,
            prior_generation BIGINT NOT NULL,
            new_generation BIGINT NOT NULL,
            prior_stage SMALLINT NOT NULL,
            new_stage SMALLINT NOT NULL,
            prior_provider_egress TEXT NOT NULL,
            new_provider_egress TEXT NOT NULL,
            authorization_id TEXT NULL,
            authorized_stage SMALLINT NULL,
            certified_sha TEXT NULL,
            deployed_sha TEXT NULL,
            database_principal TEXT NOT NULL,
            actor TEXT NOT NULL,
            occurred_at TIMESTAMPTZ NOT NULL,
            posture_digest CHAR(64) NOT NULL,
            subject_id UUID NULL,
            event_details JSONB NOT NULL,
            previous_event_hash CHAR(64) NULL,
            event_hash CHAR(64) NOT NULL,
            CONSTRAINT chk_pay24a_event_sequence CHECK(event_sequence > 0),
            CONSTRAINT chk_pay24a_event_type CHECK(event_type IN (
                'release_identity_bound',
                'human_authorization_bound',
                'activation_transitioned',
                'emergency_rollback_begun',
                'provider_admissions_expired',
                'emergency_rollback_finalized'
            )),
            CONSTRAINT chk_pay24a_event_request_digest
                CHECK(request_digest ~ '^[0-9a-f]{64}$'),
            CONSTRAINT chk_pay24a_event_generations CHECK(
                prior_generation >= 0
                AND new_generation >= prior_generation
                AND (
                    (event_type='provider_admissions_expired'
                     AND new_generation=prior_generation)
                    OR
                    (event_type<>'provider_admissions_expired'
                     AND new_generation=prior_generation+1)
                )
            ),
            CONSTRAINT chk_pay24a_event_stages
                CHECK(prior_stage IN (0,1) AND new_stage IN (0,1)),
            CONSTRAINT chk_pay24a_event_egress CHECK(
                prior_provider_egress IN ('blocked','open','closing')
                AND new_provider_egress IN ('blocked','open','closing')
            ),
            CONSTRAINT chk_pay24a_event_authorized_stage
                CHECK(authorized_stage IS NULL OR authorized_stage IN (0,1)),
            CONSTRAINT chk_pay24a_event_sha CHECK(
                (certified_sha IS NULL OR certified_sha ~ '^[0-9a-f]{40}$')
                AND
                (deployed_sha IS NULL OR deployed_sha ~ '^[0-9a-f]{40}$')
            ),
            CONSTRAINT chk_pay24a_event_actor CHECK(
                char_length(database_principal) BETWEEN 1 AND 128
                AND char_length(actor) BETWEEN 1 AND 128
                AND actor !~ '[[:cntrl:]]'
            ),
            CONSTRAINT chk_pay24a_event_posture_digest
                CHECK(posture_digest ~ '^[0-9a-f]{64}$'),
            CONSTRAINT chk_pay24a_event_details
                CHECK(pg_catalog.jsonb_typeof(event_details)='object'),
            CONSTRAINT chk_pay24a_event_previous_hash CHECK(
                previous_event_hash IS NULL
                OR previous_event_hash ~ '^[0-9a-f]{64}$'
            ),
            CONSTRAINT chk_pay24a_event_hash
                CHECK(event_hash ~ '^[0-9a-f]{64}$'),
            CONSTRAINT chk_pay24a_event_chain_shape CHECK(
                (event_sequence=1 AND previous_event_hash IS NULL)
                OR
                (event_sequence>1 AND previous_event_hash IS NOT NULL)
            )
        )
        """
    )
    op.execute(
        r"""
        CREATE TABLE finance.provider_admission_leases(
            admission_id UUID PRIMARY KEY,
            activation_generation BIGINT NOT NULL,
            organization_id UUID NOT NULL
                REFERENCES public.organizations(id) ON DELETE RESTRICT,
            capability TEXT NOT NULL,
            logical_operation_id TEXT NOT NULL,
            operation_sha CHAR(64) NOT NULL,
            lease_expires_at TIMESTAMPTZ NOT NULL,
            state TEXT NOT NULL,
            execution_id UUID NULL,
            admitted_at TIMESTAMPTZ NOT NULL,
            started_at TIMESTAMPTZ NULL,
            finished_at TIMESTAMPTZ NULL,
            terminal_reason TEXT NULL,
            last_operation_id UUID NULL,
            CONSTRAINT uq_pay24a_admission_logical_operation
                UNIQUE(organization_id,capability,logical_operation_id),
            CONSTRAINT chk_pay24a_admission_generation
                CHECK(activation_generation > 0),
            CONSTRAINT chk_pay24a_admission_capability CHECK(capability IN (
                'checkout','webhooks','payment_application',
                'subscription_activation','refund_execution',
                'recurring_billing','dunning','platform_billing'
            )),
            CONSTRAINT chk_pay24a_admission_logical_id CHECK(
                char_length(logical_operation_id) BETWEEN 1 AND 128
                AND logical_operation_id
                    ~ '^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$'
            ),
            CONSTRAINT chk_pay24a_admission_operation_sha
                CHECK(operation_sha ~ '^[0-9a-f]{64}$'),
            CONSTRAINT chk_pay24a_admission_expiry
                CHECK(lease_expires_at > admitted_at),
            CONSTRAINT chk_pay24a_admission_state CHECK(state IN (
                'admitted','active','completed','expired','revoked','unknown'
            )),
            CONSTRAINT chk_pay24a_admission_terminal_reason CHECK(
                terminal_reason IS NULL
                OR (
                    char_length(terminal_reason) BETWEEN 1 AND 80
                    AND terminal_reason ~ '^[a-z][a-z0-9_.:-]{0,79}$'
                )
            ),
            CONSTRAINT chk_pay24a_admission_state_shape CHECK(
                (state='admitted'
                 AND execution_id IS NULL
                 AND started_at IS NULL
                 AND finished_at IS NULL
                 AND terminal_reason IS NULL)
                OR
                (state='active'
                 AND execution_id IS NOT NULL
                 AND started_at IS NOT NULL
                 AND finished_at IS NULL
                 AND terminal_reason IS NULL)
                OR
                (state='completed'
                 AND execution_id IS NOT NULL
                 AND started_at IS NOT NULL
                 AND finished_at IS NOT NULL
                 AND terminal_reason='provider.completed')
                OR
                (state='unknown'
                 AND execution_id IS NOT NULL
                 AND started_at IS NOT NULL
                 AND finished_at IS NOT NULL
                 AND terminal_reason IN (
                    'provider.outcome_unknown','active_lease.expired'
                 ))
                OR
                (state='expired'
                 AND execution_id IS NULL
                 AND started_at IS NULL
                 AND finished_at IS NOT NULL
                 AND terminal_reason='admission.expired')
                OR
                (state='revoked'
                 AND execution_id IS NULL
                 AND started_at IS NULL
                 AND finished_at IS NOT NULL
                 AND terminal_reason='activation.rollback')
            )
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_pay24a_admission_execution "
        "ON finance.provider_admission_leases(execution_id) "
        "WHERE execution_id IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX ix_pay24a_admission_drain "
        "ON finance.provider_admission_leases(state,lease_expires_at,activation_generation)"
    )

    op.execute(
        """
        INSERT INTO finance.payment_activation_authority(
            singleton,generation,stage,provider_egress_state,
            release_identity_id,certified_sha,deployed_sha,
            authorization_record_id,authorization_id,authorized_sha,
            authorized_stage,authorized_by,authorized_at,
            internal_organization_id,
            checkout,webhooks,payment_application,subscription_activation,
            refund_execution,recurring_billing,dunning,platform_billing,
            last_operation_id,updated_at,posture_digest
        )
        SELECT
            true,0,0,'blocked',
            NULL,NULL,NULL,
            NULL,NULL,NULL,NULL,NULL,NULL,
            NULL,
            false,false,false,false,false,false,false,false,
            NULL,pg_catalog.clock_timestamp(),
            pg_catalog.encode(
                pg_catalog.sha256(
                    pg_catalog.convert_to(
                        pg_catalog.jsonb_build_object(
                            'generation',0,
                            'stage',0,
                            'provider_egress','blocked',
                            'internal_organization_id',NULL,
                            'checkout',false,
                            'webhooks',false,
                            'payment_application',false,
                            'subscription_activation',false,
                            'refund_execution',false,
                            'recurring_billing',false,
                            'dunning',false,
                            'platform_billing',false,
                            'release_identity_id',NULL,
                            'authorization_record_id',NULL,
                            'authorization_id',NULL
                        )::text,
                        'utf8'
                    )
                ),
                'hex'
            )
        """
    )

    for table_name in _TABLES:
        op.execute(
            f"ALTER TABLE finance.{table_name} ENABLE ROW LEVEL SECURITY"
        )
        op.execute(
            f"ALTER TABLE finance.{table_name} FORCE ROW LEVEL SECURITY"
        )
        op.execute(f"REVOKE ALL ON TABLE finance.{table_name} FROM PUBLIC")


def _install_security_owner_table_boundary() -> None:
    grants = {
        "payment_activation_release_identities": "SELECT,INSERT",
        "payment_activation_authorizations": "SELECT,INSERT",
        "payment_activation_authority": "SELECT,UPDATE",
        "payment_activation_transition_events": "SELECT,INSERT",
        "provider_admission_leases": "SELECT,INSERT,UPDATE",
    }
    for table_name, privileges in grants.items():
        op.execute(
            f"GRANT {privileges} ON TABLE finance.{table_name} "
            f"TO {_SECURITY_OWNER}"
        )
        op.execute(
            f"CREATE POLICY pay24a_{table_name}_security_owner "
            f"ON finance.{table_name} FOR ALL TO {_SECURITY_OWNER} "
            "USING (true) WITH CHECK (true)"
        )


def _install_helpers_and_guards(bind, *, had_create: bool) -> None:
    had_migration_usage = bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.has_schema_privilege("
                "'migration_owner','app_secure','USAGE')"
            )
        ).scalar_one()
    )
    if not had_create:
        op.execute("GRANT CREATE ON SCHEMA app_secure TO app_security_owner")

    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        op.execute(
            r"""
            CREATE FUNCTION app_secure.pay24a_compute_posture_digest(
                p_generation bigint,
                p_stage smallint,
                p_provider_egress text,
                p_internal_organization_id uuid,
                p_checkout boolean,
                p_webhooks boolean,
                p_payment_application boolean,
                p_subscription_activation boolean,
                p_refund_execution boolean,
                p_recurring_billing boolean,
                p_dunning boolean,
                p_platform_billing boolean,
                p_release_identity_id uuid,
                p_authorization_record_id uuid,
                p_authorization_id text
            )
            RETURNS text
            LANGUAGE sql
            IMMUTABLE
            SECURITY DEFINER
            SET search_path=pg_catalog
            SET row_security=on
            AS $function$
                SELECT pg_catalog.encode(
                    pg_catalog.sha256(
                        pg_catalog.convert_to(
                            pg_catalog.jsonb_build_object(
                                'generation',p_generation,
                                'stage',p_stage,
                                'provider_egress',p_provider_egress,
                                'internal_organization_id',
                                    CASE
                                        WHEN p_internal_organization_id IS NULL
                                            THEN NULL
                                        ELSE p_internal_organization_id::text
                                    END,
                                'checkout',p_checkout,
                                'webhooks',p_webhooks,
                                'payment_application',p_payment_application,
                                'subscription_activation',
                                    p_subscription_activation,
                                'refund_execution',p_refund_execution,
                                'recurring_billing',p_recurring_billing,
                                'dunning',p_dunning,
                                'platform_billing',p_platform_billing,
                                'release_identity_id',
                                    CASE
                                        WHEN p_release_identity_id IS NULL
                                            THEN NULL
                                        ELSE p_release_identity_id::text
                                    END,
                                'authorization_record_id',
                                    CASE
                                        WHEN p_authorization_record_id IS NULL
                                            THEN NULL
                                        ELSE p_authorization_record_id::text
                                    END,
                                'authorization_id',p_authorization_id
                            )::text,
                            'utf8'
                        )
                    ),
                    'hex'
                )
            $function$
            """
        )
        op.execute(
            r"""
            CREATE FUNCTION app_secure.pay24a_append_transition_event(
                p_operation_id uuid,
                p_event_type text,
                p_request_digest text,
                p_prior_generation bigint,
                p_new_generation bigint,
                p_prior_stage smallint,
                p_new_stage smallint,
                p_prior_provider_egress text,
                p_new_provider_egress text,
                p_authorization_id text,
                p_authorized_stage smallint,
                p_certified_sha text,
                p_deployed_sha text,
                p_actor text,
                p_posture_digest text,
                p_subject_id uuid,
                p_event_details jsonb
            )
            RETURNS uuid
            LANGUAGE plpgsql
            VOLATILE
            SECURITY DEFINER
            SET search_path=pg_catalog
            SET row_security=on
            AS $function$
            DECLARE
                v_event_id uuid := pg_catalog.gen_random_uuid();
                v_event_sequence bigint;
                v_previous_hash text;
                v_event_hash text;
                v_occurred_at timestamptz := pg_catalog.clock_timestamp();
                v_principal text := session_user::text;
                v_payload jsonb;
            BEGIN
                IF p_operation_id IS NULL
                   OR p_event_type IS NULL
                   OR p_request_digest !~ '^[0-9a-f]{64}$'
                   OR p_actor IS NULL
                   OR char_length(pg_catalog.btrim(p_actor)) NOT BETWEEN 1 AND 128
                   OR p_actor ~ '[[:cntrl:]]'
                   OR p_posture_digest !~ '^[0-9a-f]{64}$'
                   OR p_event_details IS NULL
                   OR pg_catalog.jsonb_typeof(p_event_details)<>'object'
                THEN
                    RAISE EXCEPTION 'PAY-24-A transition evidence invalid'
                        USING ERRCODE='22023';
                END IF;

                PERFORM pg_catalog.pg_advisory_xact_lock(
                    pg_catalog.hashtextextended(
                        'pay24a.payment_activation_transition_events',
                        24001
                    )
                );
                SELECT e.event_sequence,e.event_hash
                  INTO v_event_sequence,v_previous_hash
                  FROM finance.payment_activation_transition_events AS e
                 ORDER BY e.event_sequence DESC
                 LIMIT 1;
                v_event_sequence := COALESCE(v_event_sequence,0)+1;

                v_payload := pg_catalog.jsonb_build_object(
                    'event_id',v_event_id::text,
                    'event_sequence',v_event_sequence,
                    'operation_id',p_operation_id::text,
                    'event_type',p_event_type,
                    'request_digest',p_request_digest,
                    'prior_generation',p_prior_generation,
                    'new_generation',p_new_generation,
                    'prior_stage',p_prior_stage,
                    'new_stage',p_new_stage,
                    'prior_provider_egress',p_prior_provider_egress,
                    'new_provider_egress',p_new_provider_egress,
                    'authorization_id',p_authorization_id,
                    'authorized_stage',p_authorized_stage,
                    'certified_sha',p_certified_sha,
                    'deployed_sha',p_deployed_sha,
                    'database_principal',v_principal,
                    'actor',pg_catalog.btrim(p_actor),
                    'occurred_at',v_occurred_at,
                    'posture_digest',p_posture_digest,
                    'subject_id',
                        CASE
                            WHEN p_subject_id IS NULL THEN NULL
                            ELSE p_subject_id::text
                        END,
                    'event_details',p_event_details,
                    'previous_event_hash',v_previous_hash
                );
                v_event_hash := pg_catalog.encode(
                    pg_catalog.sha256(
                        pg_catalog.convert_to(v_payload::text,'utf8')
                    ),
                    'hex'
                );

                INSERT INTO finance.payment_activation_transition_events(
                    event_id,event_sequence,operation_id,event_type,
                    request_digest,prior_generation,new_generation,
                    prior_stage,new_stage,
                    prior_provider_egress,new_provider_egress,
                    authorization_id,authorized_stage,
                    certified_sha,deployed_sha,
                    database_principal,actor,occurred_at,
                    posture_digest,subject_id,event_details,
                    previous_event_hash,event_hash
                ) VALUES (
                    v_event_id,v_event_sequence,p_operation_id,p_event_type,
                    p_request_digest,p_prior_generation,p_new_generation,
                    p_prior_stage,p_new_stage,
                    p_prior_provider_egress,p_new_provider_egress,
                    p_authorization_id,p_authorized_stage,
                    p_certified_sha,p_deployed_sha,
                    v_principal,pg_catalog.btrim(p_actor),v_occurred_at,
                    p_posture_digest,p_subject_id,p_event_details,
                    v_previous_hash,v_event_hash
                );
                RETURN v_event_id;
            END
            $function$
            """
        )
        op.execute(
            r"""
            CREATE FUNCTION app_secure.pay24a_reject_immutable_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            VOLATILE
            SECURITY DEFINER
            SET search_path=pg_catalog
            SET row_security=on
            AS $function$
            BEGIN
                RAISE EXCEPTION
                    'PAY-24-A immutable activation evidence cannot be changed'
                    USING ERRCODE='42501';
            END
            $function$
            """
        )
        op.execute(
            r"""
            CREATE FUNCTION app_secure.pay24a_guard_authority_update()
            RETURNS trigger
            LANGUAGE plpgsql
            VOLATILE
            SECURITY DEFINER
            SET search_path=pg_catalog
            SET row_security=on
            AS $function$
            DECLARE
                v_expected_digest text;
            BEGIN
                IF NEW.singleton IS DISTINCT FROM OLD.singleton
                   OR NEW.generation<>OLD.generation+1
                   OR NEW.last_operation_id IS NULL
                   OR NEW.last_operation_id IS NOT DISTINCT FROM OLD.last_operation_id
                   OR NEW.updated_at<OLD.updated_at
                THEN
                    RAISE EXCEPTION
                        'PAY-24-A authority update lost generation fencing'
                        USING ERRCODE='42501';
                END IF;
                v_expected_digest :=
                    app_secure.pay24a_compute_posture_digest(
                        NEW.generation,NEW.stage,NEW.provider_egress_state,
                        NEW.internal_organization_id,
                        NEW.checkout,NEW.webhooks,NEW.payment_application,
                        NEW.subscription_activation,NEW.refund_execution,
                        NEW.recurring_billing,NEW.dunning,NEW.platform_billing,
                        NEW.release_identity_id,NEW.authorization_record_id,
                        NEW.authorization_id
                    );
                IF NEW.posture_digest IS DISTINCT FROM v_expected_digest THEN
                    RAISE EXCEPTION
                        'PAY-24-A authority posture digest mismatch'
                        USING ERRCODE='42501';
                END IF;
                RETURN NEW;
            END
            $function$
            """
        )
        op.execute(
            r"""
            CREATE FUNCTION app_secure.pay24a_enforce_authority_evidence()
            RETURNS trigger
            LANGUAGE plpgsql
            VOLATILE
            SECURITY DEFINER
            SET search_path=pg_catalog
            SET row_security=on
            AS $function$
            BEGIN
                IF NOT EXISTS(
                    SELECT 1
                    FROM finance.payment_activation_transition_events AS e
                    WHERE e.operation_id=NEW.last_operation_id
                      AND e.prior_generation=OLD.generation
                      AND e.new_generation=NEW.generation
                      AND e.prior_stage=OLD.stage
                      AND e.new_stage=NEW.stage
                      AND e.posture_digest=NEW.posture_digest
                ) THEN
                    RAISE EXCEPTION
                        'PAY-24-A authority update lacks atomic transition evidence'
                        USING ERRCODE='23514';
                END IF;
                RETURN NULL;
            END
            $function$
            """
        )
        op.execute(
            r"""
            CREATE FUNCTION app_secure.pay24a_reject_admission_erasure()
            RETURNS trigger
            LANGUAGE plpgsql
            VOLATILE
            SECURITY DEFINER
            SET search_path=pg_catalog
            SET row_security=on
            AS $function$
            BEGIN
                RAISE EXCEPTION
                    'PAY-24-A provider admission history cannot be erased'
                    USING ERRCODE='42501';
            END
            $function$
            """
        )
        op.execute(
            r"""
            CREATE FUNCTION app_secure.pay24a_guard_admission_update()
            RETURNS trigger
            LANGUAGE plpgsql
            VOLATILE
            SECURITY DEFINER
            SET search_path=pg_catalog
            SET row_security=on
            AS $function$
            BEGIN
                IF NEW.admission_id IS DISTINCT FROM OLD.admission_id
                   OR NEW.activation_generation
                        IS DISTINCT FROM OLD.activation_generation
                   OR NEW.organization_id IS DISTINCT FROM OLD.organization_id
                   OR NEW.capability IS DISTINCT FROM OLD.capability
                   OR NEW.logical_operation_id
                        IS DISTINCT FROM OLD.logical_operation_id
                   OR NEW.operation_sha IS DISTINCT FROM OLD.operation_sha
                   OR NEW.lease_expires_at IS DISTINCT FROM OLD.lease_expires_at
                   OR NEW.admitted_at IS DISTINCT FROM OLD.admitted_at
                THEN
                    RAISE EXCEPTION
                        'PAY-24-A provider admission identity is immutable'
                        USING ERRCODE='42501';
                END IF;

                IF NOT (
                    (OLD.state='admitted'
                     AND NEW.state IN ('active','expired','revoked'))
                    OR
                    (OLD.state='active'
                     AND NEW.state IN ('completed','unknown'))
                ) THEN
                    RAISE EXCEPTION
                        'PAY-24-A provider admission state transition invalid'
                        USING ERRCODE='23514';
                END IF;
                IF OLD.state='active' AND (
                    NEW.execution_id IS DISTINCT FROM OLD.execution_id
                    OR NEW.started_at IS DISTINCT FROM OLD.started_at
                ) THEN
                    RAISE EXCEPTION
                        'PAY-24-A active execution fence is immutable'
                        USING ERRCODE='42501';
                END IF;
                RETURN NEW;
            END
            $function$
            """
        )

        for signature in _FUNCTION_SIGNATURES[:7]:
            op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")

        if not had_migration_usage:
            op.execute("GRANT USAGE ON SCHEMA app_secure TO migration_owner")
        for signature in _FUNCTION_SIGNATURES[2:7]:
            op.execute(
                f"GRANT EXECUTE ON FUNCTION {signature} TO migration_owner"
            )
    finally:
        op.execute("RESET ROLE")

    op.execute(
        """
        CREATE TRIGGER trg_pay24a_release_identity_immutable
        BEFORE UPDATE OR DELETE OR TRUNCATE
        ON finance.payment_activation_release_identities
        FOR EACH STATEMENT
        EXECUTE FUNCTION app_secure.pay24a_reject_immutable_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_pay24a_authorization_immutable
        BEFORE UPDATE OR DELETE OR TRUNCATE
        ON finance.payment_activation_authorizations
        FOR EACH STATEMENT
        EXECUTE FUNCTION app_secure.pay24a_reject_immutable_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_pay24a_transition_event_immutable
        BEFORE UPDATE OR DELETE OR TRUNCATE
        ON finance.payment_activation_transition_events
        FOR EACH STATEMENT
        EXECUTE FUNCTION app_secure.pay24a_reject_immutable_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_pay24a_authority_no_insert_or_erase
        BEFORE INSERT OR DELETE OR TRUNCATE
        ON finance.payment_activation_authority
        FOR EACH STATEMENT
        EXECUTE FUNCTION app_secure.pay24a_reject_immutable_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_pay24a_authority_update_guard
        BEFORE UPDATE ON finance.payment_activation_authority
        FOR EACH ROW
        EXECUTE FUNCTION app_secure.pay24a_guard_authority_update()
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_pay24a_authority_evidence
        AFTER UPDATE ON finance.payment_activation_authority
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW
        EXECUTE FUNCTION app_secure.pay24a_enforce_authority_evidence()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_pay24a_admission_no_erase
        BEFORE DELETE OR TRUNCATE
        ON finance.provider_admission_leases
        FOR EACH STATEMENT
        EXECUTE FUNCTION app_secure.pay24a_reject_admission_erasure()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_pay24a_admission_update_guard
        BEFORE UPDATE ON finance.provider_admission_leases
        FOR EACH ROW
        EXECUTE FUNCTION app_secure.pay24a_guard_admission_update()
        """
    )

    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        for signature in _FUNCTION_SIGNATURES[2:7]:
            op.execute(
                f"REVOKE EXECUTE ON FUNCTION {signature} FROM migration_owner"
            )
        if not had_migration_usage:
            op.execute("REVOKE USAGE ON SCHEMA app_secure FROM migration_owner")
    finally:
        op.execute("RESET ROLE")

def _install_release_and_authorization_functions() -> None:
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        op.execute(
            r"""
            CREATE FUNCTION app_secure.pay24a_bind_release_identity(
                p_operation_id uuid,
                p_expected_generation bigint,
                p_certified_sha text,
                p_deployed_sha text,
                p_measured_by text,
                p_measured_at timestamptz,
                p_actor text
            )
            RETURNS TABLE(
                generation bigint,
                release_identity_id uuid,
                certified_sha text,
                deployed_sha text
            )
            LANGUAGE plpgsql
            VOLATILE
            SECURITY DEFINER
            SET search_path=pg_catalog
            SET row_security=on
            AS $function$
            DECLARE
                v_before finance.payment_activation_authority%ROWTYPE;
                v_after finance.payment_activation_authority%ROWTYPE;
                v_existing finance.payment_activation_transition_events%ROWTYPE;
                v_release finance.payment_activation_release_identities%ROWTYPE;
                v_release_id uuid := pg_catalog.gen_random_uuid();
                v_now timestamptz := pg_catalog.clock_timestamp();
                v_request_digest text;
            BEGIN
                IF NOT pg_catalog.pg_has_role(
                    session_user,'finance_config_runtime','MEMBER'
                ) THEN
                    RAISE EXCEPTION
                        'PAY-24-A release binding requires finance_config_runtime'
                        USING ERRCODE='42501';
                END IF;
                IF p_operation_id IS NULL
                   OR p_expected_generation IS NULL
                   OR p_expected_generation<0
                   OR p_certified_sha IS NULL
                   OR p_certified_sha !~ '^[0-9a-f]{40}$'
                   OR p_deployed_sha IS NULL
                   OR p_deployed_sha !~ '^[0-9a-f]{40}$'
                   OR p_measured_by IS NULL
                   OR char_length(pg_catalog.btrim(p_measured_by))
                        NOT BETWEEN 3 AND 128
                   OR p_measured_by ~ '[[:cntrl:]]'
                   OR p_measured_at IS NULL
                   OR p_measured_at>v_now
                   OR p_actor IS NULL
                   OR char_length(pg_catalog.btrim(p_actor)) NOT BETWEEN 1 AND 128
                   OR p_actor ~ '[[:cntrl:]]'
                THEN
                    RAISE EXCEPTION 'PAY-24-A release identity invalid'
                        USING ERRCODE='22023';
                END IF;

                v_request_digest := pg_catalog.encode(
                    pg_catalog.sha256(
                        pg_catalog.convert_to(
                            pg_catalog.jsonb_build_object(
                                'operation_id',p_operation_id::text,
                                'expected_generation',p_expected_generation,
                                'certified_sha',p_certified_sha,
                                'deployed_sha',p_deployed_sha,
                                'measured_by',pg_catalog.btrim(p_measured_by),
                                'actor',pg_catalog.btrim(p_actor)
                            )::text,
                            'utf8'
                        )
                    ),
                    'hex'
                );

                SELECT a.* INTO STRICT v_before
                  FROM finance.payment_activation_authority AS a
                 WHERE a.singleton
                 FOR UPDATE;

                SELECT e.* INTO v_existing
                  FROM finance.payment_activation_transition_events AS e
                 WHERE e.operation_id=p_operation_id;
                IF FOUND THEN
                    IF v_existing.event_type<>'release_identity_bound'
                       OR v_existing.request_digest<>v_request_digest
                    THEN
                        RAISE EXCEPTION
                            'PAY-24-A operation id conflicts with prior request'
                            USING ERRCODE='23505';
                    END IF;
                    SELECT r.* INTO STRICT v_release
                      FROM finance.payment_activation_release_identities AS r
                     WHERE r.release_identity_id=v_existing.subject_id;
                    RETURN QUERY SELECT
                        v_existing.new_generation,
                        v_release.release_identity_id,
                        v_release.certified_sha,
                        v_release.deployed_sha;
                    RETURN;
                END IF;

                IF v_before.generation<>p_expected_generation THEN
                    RAISE EXCEPTION
                        'PAY-24-A stale activation generation'
                        USING ERRCODE='40001';
                END IF;
                IF v_before.provider_egress_state='closing' THEN
                    RAISE EXCEPTION
                        'PAY-24-A rollback is closing; release rebind denied'
                        USING ERRCODE='55000';
                END IF;
                IF v_before.stage=1 AND (
                    p_certified_sha<>p_deployed_sha
                    OR p_certified_sha IS DISTINCT FROM v_before.authorized_sha
                    OR v_before.authorized_stage<>1
                ) THEN
                    RAISE EXCEPTION
                        'PAY-24-A release rebind would invalidate Stage 1'
                        USING ERRCODE='23514';
                END IF;

                INSERT INTO finance.payment_activation_release_identities(
                    release_identity_id,operation_id,
                    certified_sha,deployed_sha,
                    measured_by,measured_at,bound_at
                ) VALUES (
                    v_release_id,p_operation_id,
                    p_certified_sha,p_deployed_sha,
                    pg_catalog.btrim(p_measured_by),p_measured_at,v_now
                );

                UPDATE finance.payment_activation_authority AS a
                   SET generation=v_before.generation+1,
                       release_identity_id=v_release_id,
                       certified_sha=p_certified_sha,
                       deployed_sha=p_deployed_sha,
                       last_operation_id=p_operation_id,
                       updated_at=v_now,
                       posture_digest=app_secure.pay24a_compute_posture_digest(
                           v_before.generation+1,
                           v_before.stage,
                           v_before.provider_egress_state,
                           v_before.internal_organization_id,
                           v_before.checkout,v_before.webhooks,
                           v_before.payment_application,
                           v_before.subscription_activation,
                           v_before.refund_execution,
                           v_before.recurring_billing,
                           v_before.dunning,v_before.platform_billing,
                           v_release_id,
                           v_before.authorization_record_id,
                           v_before.authorization_id
                       )
                 WHERE a.singleton
                 RETURNING a.* INTO STRICT v_after;

                PERFORM app_secure.pay24a_append_transition_event(
                    p_operation_id,'release_identity_bound',v_request_digest,
                    v_before.generation,v_after.generation,
                    v_before.stage,v_after.stage,
                    v_before.provider_egress_state,
                    v_after.provider_egress_state,
                    v_after.authorization_id,v_after.authorized_stage,
                    v_after.certified_sha,v_after.deployed_sha,
                    p_actor,v_after.posture_digest,v_release_id,
                    pg_catalog.jsonb_build_object(
                        'binding','trusted_release_identity'
                    )
                );

                RETURN QUERY SELECT
                    v_after.generation,v_release_id,
                    v_after.certified_sha,v_after.deployed_sha;
            END
            $function$
            """
        )
        op.execute(
            r"""
            CREATE FUNCTION app_secure.pay24a_bind_human_authorization(
                p_operation_id uuid,
                p_expected_generation bigint,
                p_authorization_id text,
                p_authorized_sha text,
                p_authorized_stage smallint,
                p_authorized_by text,
                p_authorized_at timestamptz,
                p_actor text
            )
            RETURNS TABLE(
                generation bigint,
                authorization_id text,
                authorized_stage smallint
            )
            LANGUAGE plpgsql
            VOLATILE
            SECURITY DEFINER
            SET search_path=pg_catalog
            SET row_security=on
            AS $function$
            DECLARE
                v_before finance.payment_activation_authority%ROWTYPE;
                v_after finance.payment_activation_authority%ROWTYPE;
                v_existing finance.payment_activation_transition_events%ROWTYPE;
                v_authorization
                    finance.payment_activation_authorizations%ROWTYPE;
                v_authorization_record_id uuid := pg_catalog.gen_random_uuid();
                v_now timestamptz := pg_catalog.clock_timestamp();
                v_request_digest text;
            BEGIN
                IF NOT pg_catalog.pg_has_role(
                    session_user,'finance_config_runtime','MEMBER'
                ) THEN
                    RAISE EXCEPTION
                        'PAY-24-A authorization binding requires finance_config_runtime'
                        USING ERRCODE='42501';
                END IF;
                IF p_operation_id IS NULL
                   OR p_expected_generation IS NULL
                   OR p_expected_generation<0
                   OR p_authorization_id IS NULL
                   OR char_length(p_authorization_id) NOT BETWEEN 3 AND 128
                   OR p_authorization_id
                        !~ '^[A-Za-z0-9][A-Za-z0-9_.:/-]{2,127}$'
                   OR p_authorized_sha IS NULL
                   OR p_authorized_sha !~ '^[0-9a-f]{40}$'
                   OR p_authorized_stage IS NULL
                   OR p_authorized_stage NOT IN (0,1)
                   OR p_authorized_by IS NULL
                   OR char_length(pg_catalog.btrim(p_authorized_by))
                        NOT BETWEEN 3 AND 128
                   OR p_authorized_by ~ '[[:cntrl:]]'
                   OR p_authorized_at IS NULL
                   OR p_authorized_at>v_now
                   OR p_actor IS NULL
                   OR char_length(pg_catalog.btrim(p_actor)) NOT BETWEEN 1 AND 128
                   OR p_actor ~ '[[:cntrl:]]'
                THEN
                    RAISE EXCEPTION 'PAY-24-A human authorization invalid'
                        USING ERRCODE='22023';
                END IF;

                v_request_digest := pg_catalog.encode(
                    pg_catalog.sha256(
                        pg_catalog.convert_to(
                            pg_catalog.jsonb_build_object(
                                'operation_id',p_operation_id::text,
                                'expected_generation',p_expected_generation,
                                'authorization_id',p_authorization_id,
                                'authorized_sha',p_authorized_sha,
                                'authorized_stage',p_authorized_stage,
                                'authorized_by',pg_catalog.btrim(p_authorized_by),
                                'authorized_at',p_authorized_at,
                                'actor',pg_catalog.btrim(p_actor)
                            )::text,
                            'utf8'
                        )
                    ),
                    'hex'
                );

                SELECT a.* INTO STRICT v_before
                  FROM finance.payment_activation_authority AS a
                 WHERE a.singleton
                 FOR UPDATE;

                SELECT e.* INTO v_existing
                  FROM finance.payment_activation_transition_events AS e
                 WHERE e.operation_id=p_operation_id;
                IF FOUND THEN
                    IF v_existing.event_type<>'human_authorization_bound'
                       OR v_existing.request_digest<>v_request_digest
                    THEN
                        RAISE EXCEPTION
                            'PAY-24-A operation id conflicts with prior request'
                            USING ERRCODE='23505';
                    END IF;
                    SELECT h.* INTO STRICT v_authorization
                      FROM finance.payment_activation_authorizations AS h
                     WHERE h.authorization_record_id=v_existing.subject_id;
                    RETURN QUERY SELECT
                        v_existing.new_generation,
                        v_authorization.authorization_id,
                        v_authorization.authorized_stage;
                    RETURN;
                END IF;

                IF v_before.generation<>p_expected_generation THEN
                    RAISE EXCEPTION
                        'PAY-24-A stale activation generation'
                        USING ERRCODE='40001';
                END IF;
                IF v_before.provider_egress_state='closing' THEN
                    RAISE EXCEPTION
                        'PAY-24-A rollback is closing; authorization rebind denied'
                        USING ERRCODE='55000';
                END IF;
                IF v_before.stage=1 AND (
                    p_authorized_stage<>1
                    OR p_authorized_sha IS DISTINCT FROM v_before.certified_sha
                    OR v_before.certified_sha
                        IS DISTINCT FROM v_before.deployed_sha
                ) THEN
                    RAISE EXCEPTION
                        'PAY-24-A authorization rebind would invalidate Stage 1'
                        USING ERRCODE='23514';
                END IF;
                IF EXISTS(
                    SELECT 1
                    FROM finance.payment_activation_authorizations AS h
                    WHERE h.authorization_id=p_authorization_id
                ) THEN
                    RAISE EXCEPTION
                        'PAY-24-A authorization id is already bound'
                        USING ERRCODE='23505';
                END IF;

                INSERT INTO finance.payment_activation_authorizations(
                    authorization_record_id,operation_id,authorization_id,
                    authorized_sha,authorized_stage,authorized_by,
                    authorized_at,bound_at
                ) VALUES (
                    v_authorization_record_id,p_operation_id,p_authorization_id,
                    p_authorized_sha,p_authorized_stage,
                    pg_catalog.btrim(p_authorized_by),p_authorized_at,v_now
                );

                UPDATE finance.payment_activation_authority AS a
                   SET generation=v_before.generation+1,
                       authorization_record_id=v_authorization_record_id,
                       authorization_id=p_authorization_id,
                       authorized_sha=p_authorized_sha,
                       authorized_stage=p_authorized_stage,
                       authorized_by=pg_catalog.btrim(p_authorized_by),
                       authorized_at=p_authorized_at,
                       last_operation_id=p_operation_id,
                       updated_at=v_now,
                       posture_digest=app_secure.pay24a_compute_posture_digest(
                           v_before.generation+1,
                           v_before.stage,
                           v_before.provider_egress_state,
                           v_before.internal_organization_id,
                           v_before.checkout,v_before.webhooks,
                           v_before.payment_application,
                           v_before.subscription_activation,
                           v_before.refund_execution,
                           v_before.recurring_billing,
                           v_before.dunning,v_before.platform_billing,
                           v_before.release_identity_id,
                           v_authorization_record_id,
                           p_authorization_id
                       )
                 WHERE a.singleton
                 RETURNING a.* INTO STRICT v_after;

                PERFORM app_secure.pay24a_append_transition_event(
                    p_operation_id,'human_authorization_bound',
                    v_request_digest,
                    v_before.generation,v_after.generation,
                    v_before.stage,v_after.stage,
                    v_before.provider_egress_state,
                    v_after.provider_egress_state,
                    v_after.authorization_id,v_after.authorized_stage,
                    v_after.certified_sha,v_after.deployed_sha,
                    p_actor,v_after.posture_digest,
                    v_authorization_record_id,
                    pg_catalog.jsonb_build_object(
                        'binding','stage_bound_human_authorization'
                    )
                );

                RETURN QUERY SELECT
                    v_after.generation,v_after.authorization_id,
                    v_after.authorized_stage;
            END
            $function$
            """
        )
        for signature in _CONFIG_FUNCTIONS[:2]:
            op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
            op.execute(
                f"GRANT EXECUTE ON FUNCTION {signature} TO {_CONFIG}"
            )
    finally:
        op.execute("RESET ROLE")


def _install_transition_and_rollback_functions() -> None:
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        op.execute(
            r"""
            CREATE FUNCTION app_secure.pay24a_transition_activation(
                p_operation_id uuid,
                p_expected_generation bigint,
                p_target_stage smallint,
                p_provider_egress text,
                p_internal_organization_id uuid,
                p_checkout boolean,
                p_webhooks boolean,
                p_payment_application boolean,
                p_subscription_activation boolean,
                p_refund_execution boolean,
                p_recurring_billing boolean,
                p_dunning boolean,
                p_platform_billing boolean,
                p_actor text
            )
            RETURNS TABLE(
                generation bigint,
                stage smallint,
                provider_egress text,
                posture_digest text
            )
            LANGUAGE plpgsql
            VOLATILE
            SECURITY DEFINER
            SET search_path=pg_catalog
            SET row_security=on
            AS $function$
            DECLARE
                v_before finance.payment_activation_authority%ROWTYPE;
                v_after finance.payment_activation_authority%ROWTYPE;
                v_existing finance.payment_activation_transition_events%ROWTYPE;
                v_now timestamptz := pg_catalog.clock_timestamp();
                v_request_digest text;
            BEGIN
                IF NOT pg_catalog.pg_has_role(
                    session_user,'finance_config_runtime','MEMBER'
                ) THEN
                    RAISE EXCEPTION
                        'PAY-24-A activation transition requires finance_config_runtime'
                        USING ERRCODE='42501';
                END IF;
                IF p_operation_id IS NULL
                   OR p_expected_generation IS NULL
                   OR p_expected_generation<0
                   OR p_target_stage IS NULL
                   OR p_target_stage NOT IN (0,1)
                   OR p_provider_egress IS NULL
                   OR p_provider_egress NOT IN ('blocked','open')
                   OR p_checkout IS NULL
                   OR p_webhooks IS NULL
                   OR p_payment_application IS NULL
                   OR p_subscription_activation IS NULL
                   OR p_refund_execution IS NULL
                   OR p_recurring_billing IS NULL
                   OR p_dunning IS NULL
                   OR p_platform_billing IS NULL
                   OR p_actor IS NULL
                   OR char_length(pg_catalog.btrim(p_actor)) NOT BETWEEN 1 AND 128
                   OR p_actor ~ '[[:cntrl:]]'
                THEN
                    RAISE EXCEPTION 'PAY-24-A activation transition invalid'
                        USING ERRCODE='22023';
                END IF;

                v_request_digest := pg_catalog.encode(
                    pg_catalog.sha256(
                        pg_catalog.convert_to(
                            pg_catalog.jsonb_build_object(
                                'operation_id',p_operation_id::text,
                                'expected_generation',p_expected_generation,
                                'target_stage',p_target_stage,
                                'provider_egress',p_provider_egress,
                                'internal_organization_id',
                                    CASE
                                        WHEN p_internal_organization_id IS NULL
                                            THEN NULL
                                        ELSE p_internal_organization_id::text
                                    END,
                                'checkout',p_checkout,
                                'webhooks',p_webhooks,
                                'payment_application',p_payment_application,
                                'subscription_activation',
                                    p_subscription_activation,
                                'refund_execution',p_refund_execution,
                                'recurring_billing',p_recurring_billing,
                                'dunning',p_dunning,
                                'platform_billing',p_platform_billing,
                                'actor',pg_catalog.btrim(p_actor)
                            )::text,
                            'utf8'
                        )
                    ),
                    'hex'
                );

                SELECT a.* INTO STRICT v_before
                  FROM finance.payment_activation_authority AS a
                 WHERE a.singleton
                 FOR UPDATE;

                SELECT e.* INTO v_existing
                  FROM finance.payment_activation_transition_events AS e
                 WHERE e.operation_id=p_operation_id;
                IF FOUND THEN
                    IF v_existing.event_type<>'activation_transitioned'
                       OR v_existing.request_digest<>v_request_digest
                    THEN
                        RAISE EXCEPTION
                            'PAY-24-A operation id conflicts with prior request'
                            USING ERRCODE='23505';
                    END IF;
                    RETURN QUERY SELECT
                        v_existing.new_generation,
                        v_existing.new_stage,
                        v_existing.new_provider_egress,
                        v_existing.posture_digest::text;
                    RETURN;
                END IF;

                IF v_before.generation<>p_expected_generation THEN
                    RAISE EXCEPTION
                        'PAY-24-A stale activation generation'
                        USING ERRCODE='40001';
                END IF;
                IF v_before.provider_egress_state='closing' THEN
                    RAISE EXCEPTION
                        'PAY-24-A rollback is closing; ordinary transition denied'
                        USING ERRCODE='55000';
                END IF;
                IF p_target_stage<v_before.stage
                   OR p_target_stage>v_before.stage+1
                THEN
                    RAISE EXCEPTION
                        'PAY-24-A progressive stage transition violated'
                        USING ERRCODE='23514';
                END IF;
                IF p_target_stage=0 AND (
                    p_provider_egress<>'blocked'
                    OR p_internal_organization_id IS NOT NULL
                    OR p_checkout OR p_webhooks OR p_payment_application
                    OR p_subscription_activation OR p_refund_execution
                    OR p_recurring_billing OR p_dunning OR p_platform_billing
                ) THEN
                    RAISE EXCEPTION
                        'PAY-24-A Stage 0 posture is fail-closed'
                        USING ERRCODE='23514';
                END IF;
                IF p_target_stage=1 AND (
                    p_internal_organization_id IS NULL
                    OR v_before.release_identity_id IS NULL
                    OR v_before.authorization_record_id IS NULL
                    OR v_before.certified_sha
                        IS DISTINCT FROM v_before.deployed_sha
                    OR v_before.certified_sha
                        IS DISTINCT FROM v_before.authorized_sha
                    OR v_before.authorized_stage<>1
                    OR p_refund_execution
                    OR p_recurring_billing
                    OR p_dunning
                    OR p_platform_billing
                ) THEN
                    RAISE EXCEPTION
                        'PAY-24-A Stage 1 exact authority is incomplete'
                        USING ERRCODE='23514';
                END IF;

                IF p_target_stage=v_before.stage
                   AND p_provider_egress=v_before.provider_egress_state
                   AND p_internal_organization_id
                        IS NOT DISTINCT FROM v_before.internal_organization_id
                   AND p_checkout=v_before.checkout
                   AND p_webhooks=v_before.webhooks
                   AND p_payment_application=v_before.payment_application
                   AND p_subscription_activation
                        =v_before.subscription_activation
                   AND p_refund_execution=v_before.refund_execution
                   AND p_recurring_billing=v_before.recurring_billing
                   AND p_dunning=v_before.dunning
                   AND p_platform_billing=v_before.platform_billing
                THEN
                    RAISE EXCEPTION
                        'PAY-24-A transition contains no material posture change'
                        USING ERRCODE='22023';
                END IF;

                UPDATE finance.payment_activation_authority AS a
                   SET generation=v_before.generation+1,
                       stage=p_target_stage,
                       provider_egress_state=p_provider_egress,
                       internal_organization_id=p_internal_organization_id,
                       checkout=p_checkout,
                       webhooks=p_webhooks,
                       payment_application=p_payment_application,
                       subscription_activation=p_subscription_activation,
                       refund_execution=p_refund_execution,
                       recurring_billing=p_recurring_billing,
                       dunning=p_dunning,
                       platform_billing=p_platform_billing,
                       last_operation_id=p_operation_id,
                       updated_at=v_now,
                       posture_digest=app_secure.pay24a_compute_posture_digest(
                           v_before.generation+1,
                           p_target_stage,p_provider_egress,
                           p_internal_organization_id,
                           p_checkout,p_webhooks,p_payment_application,
                           p_subscription_activation,p_refund_execution,
                           p_recurring_billing,p_dunning,p_platform_billing,
                           v_before.release_identity_id,
                           v_before.authorization_record_id,
                           v_before.authorization_id
                       )
                 WHERE a.singleton
                 RETURNING a.* INTO STRICT v_after;

                PERFORM app_secure.pay24a_append_transition_event(
                    p_operation_id,'activation_transitioned',
                    v_request_digest,
                    v_before.generation,v_after.generation,
                    v_before.stage,v_after.stage,
                    v_before.provider_egress_state,
                    v_after.provider_egress_state,
                    v_after.authorization_id,v_after.authorized_stage,
                    v_after.certified_sha,v_after.deployed_sha,
                    p_actor,v_after.posture_digest,NULL,
                    pg_catalog.jsonb_build_object(
                        'transition','validated_full_target_posture',
                        'internal_organization_bound',
                            v_after.internal_organization_id IS NOT NULL
                    )
                );

                RETURN QUERY SELECT
                    v_after.generation,v_after.stage,
                    v_after.provider_egress_state,v_after.posture_digest::text;
            END
            $function$
            """
        )
        op.execute(
            r"""
            CREATE FUNCTION app_secure.pay24a_begin_emergency_rollback(
                p_operation_id uuid,
                p_expected_generation bigint,
                p_actor text
            )
            RETURNS TABLE(
                generation bigint,
                revoked_admission_count bigint,
                active_admission_count bigint
            )
            LANGUAGE plpgsql
            VOLATILE
            SECURITY DEFINER
            SET search_path=pg_catalog
            SET row_security=on
            AS $function$
            DECLARE
                v_before finance.payment_activation_authority%ROWTYPE;
                v_after finance.payment_activation_authority%ROWTYPE;
                v_existing finance.payment_activation_transition_events%ROWTYPE;
                v_now timestamptz := pg_catalog.clock_timestamp();
                v_request_digest text;
                v_revoked bigint := 0;
                v_active bigint := 0;
            BEGIN
                IF NOT pg_catalog.pg_has_role(
                    session_user,'finance_config_runtime','MEMBER'
                ) THEN
                    RAISE EXCEPTION
                        'PAY-24-A rollback requires finance_config_runtime'
                        USING ERRCODE='42501';
                END IF;
                IF p_operation_id IS NULL
                   OR p_expected_generation IS NULL
                   OR p_expected_generation<0
                   OR p_actor IS NULL
                   OR char_length(pg_catalog.btrim(p_actor)) NOT BETWEEN 1 AND 128
                   OR p_actor ~ '[[:cntrl:]]'
                THEN
                    RAISE EXCEPTION 'PAY-24-A rollback arguments invalid'
                        USING ERRCODE='22023';
                END IF;
                v_request_digest := pg_catalog.encode(
                    pg_catalog.sha256(
                        pg_catalog.convert_to(
                            pg_catalog.jsonb_build_object(
                                'operation_id',p_operation_id::text,
                                'expected_generation',p_expected_generation,
                                'actor',pg_catalog.btrim(p_actor)
                            )::text,
                            'utf8'
                        )
                    ),
                    'hex'
                );

                SELECT a.* INTO STRICT v_before
                  FROM finance.payment_activation_authority AS a
                 WHERE a.singleton
                 FOR UPDATE;
                SELECT e.* INTO v_existing
                  FROM finance.payment_activation_transition_events AS e
                 WHERE e.operation_id=p_operation_id;
                IF FOUND THEN
                    IF v_existing.event_type<>'emergency_rollback_begun'
                       OR v_existing.request_digest<>v_request_digest
                    THEN
                        RAISE EXCEPTION
                            'PAY-24-A operation id conflicts with prior request'
                            USING ERRCODE='23505';
                    END IF;
                    RETURN QUERY SELECT
                        v_existing.new_generation,
                        (v_existing.event_details
                            ->>'revoked_admission_count')::bigint,
                        (v_existing.event_details
                            ->>'active_admission_count')::bigint;
                    RETURN;
                END IF;
                IF v_before.generation<>p_expected_generation THEN
                    RAISE EXCEPTION
                        'PAY-24-A stale activation generation'
                        USING ERRCODE='40001';
                END IF;
                IF v_before.stage<>1
                   OR v_before.provider_egress_state='closing'
                THEN
                    RAISE EXCEPTION
                        'PAY-24-A rollback begin requires non-closing Stage 1'
                        USING ERRCODE='23514';
                END IF;

                UPDATE finance.payment_activation_authority AS a
                   SET generation=v_before.generation+1,
                       provider_egress_state='closing',
                       last_operation_id=p_operation_id,
                       updated_at=v_now,
                       posture_digest=app_secure.pay24a_compute_posture_digest(
                           v_before.generation+1,
                           v_before.stage,'closing',
                           v_before.internal_organization_id,
                           v_before.checkout,v_before.webhooks,
                           v_before.payment_application,
                           v_before.subscription_activation,
                           v_before.refund_execution,
                           v_before.recurring_billing,
                           v_before.dunning,v_before.platform_billing,
                           v_before.release_identity_id,
                           v_before.authorization_record_id,
                           v_before.authorization_id
                       )
                 WHERE a.singleton
                 RETURNING a.* INTO STRICT v_after;

                UPDATE finance.provider_admission_leases AS l
                   SET state='revoked',
                       finished_at=v_now,
                       terminal_reason='activation.rollback',
                       last_operation_id=p_operation_id
                 WHERE l.state='admitted';
                GET DIAGNOSTICS v_revoked = ROW_COUNT;

                SELECT count(*)::bigint INTO v_active
                  FROM finance.provider_admission_leases AS l
                 WHERE l.state='active';

                PERFORM app_secure.pay24a_append_transition_event(
                    p_operation_id,'emergency_rollback_begun',
                    v_request_digest,
                    v_before.generation,v_after.generation,
                    v_before.stage,v_after.stage,
                    v_before.provider_egress_state,
                    v_after.provider_egress_state,
                    v_after.authorization_id,v_after.authorized_stage,
                    v_after.certified_sha,v_after.deployed_sha,
                    p_actor,v_after.posture_digest,NULL,
                    pg_catalog.jsonb_build_object(
                        'revoked_admission_count',v_revoked,
                        'active_admission_count',v_active,
                        'new_admissions','denied'
                    )
                );
                RETURN QUERY SELECT v_after.generation,v_revoked,v_active;
            END
            $function$
            """
        )
        op.execute(
            r"""
            CREATE FUNCTION app_secure.pay24a_expire_provider_admissions(
                p_operation_id uuid,
                p_actor text
            )
            RETURNS TABLE(expired_count bigint,unknown_count bigint)
            LANGUAGE plpgsql
            VOLATILE
            SECURITY DEFINER
            SET search_path=pg_catalog
            SET row_security=on
            AS $function$
            DECLARE
                v_authority finance.payment_activation_authority%ROWTYPE;
                v_existing finance.payment_activation_transition_events%ROWTYPE;
                v_now timestamptz := pg_catalog.clock_timestamp();
                v_request_digest text;
                v_expired bigint := 0;
                v_unknown bigint := 0;
            BEGIN
                IF NOT (
                    pg_catalog.pg_has_role(
                        session_user,'finance_config_runtime','MEMBER'
                    )
                    OR pg_catalog.pg_has_role(
                        session_user,'finance_maintenance_runtime','MEMBER'
                    )
                    OR pg_catalog.pg_has_role(
                        session_user,'lifecycle_maintenance_runtime','MEMBER'
                    )
                ) THEN
                    RAISE EXCEPTION
                        'PAY-24-A admission expiry requires maintenance capability'
                        USING ERRCODE='42501';
                END IF;
                IF p_operation_id IS NULL
                   OR p_actor IS NULL
                   OR char_length(pg_catalog.btrim(p_actor)) NOT BETWEEN 1 AND 128
                   OR p_actor ~ '[[:cntrl:]]'
                THEN
                    RAISE EXCEPTION
                        'PAY-24-A admission expiry arguments invalid'
                        USING ERRCODE='22023';
                END IF;
                v_request_digest := pg_catalog.encode(
                    pg_catalog.sha256(
                        pg_catalog.convert_to(
                            pg_catalog.jsonb_build_object(
                                'operation_id',p_operation_id::text,
                                'actor',pg_catalog.btrim(p_actor)
                            )::text,
                            'utf8'
                        )
                    ),
                    'hex'
                );

                SELECT a.* INTO STRICT v_authority
                  FROM finance.payment_activation_authority AS a
                 WHERE a.singleton
                 FOR UPDATE;
                SELECT e.* INTO v_existing
                  FROM finance.payment_activation_transition_events AS e
                 WHERE e.operation_id=p_operation_id;
                IF FOUND THEN
                    IF v_existing.event_type<>'provider_admissions_expired'
                       OR v_existing.request_digest<>v_request_digest
                    THEN
                        RAISE EXCEPTION
                            'PAY-24-A operation id conflicts with prior request'
                            USING ERRCODE='23505';
                    END IF;
                    RETURN QUERY SELECT
                        (v_existing.event_details->>'expired_count')::bigint,
                        (v_existing.event_details->>'unknown_count')::bigint;
                    RETURN;
                END IF;

                UPDATE finance.provider_admission_leases AS l
                   SET state='expired',
                       finished_at=v_now,
                       terminal_reason='admission.expired',
                       last_operation_id=p_operation_id
                 WHERE l.state='admitted'
                   AND l.lease_expires_at<=v_now;
                GET DIAGNOSTICS v_expired = ROW_COUNT;

                UPDATE finance.provider_admission_leases AS l
                   SET state='unknown',
                       finished_at=v_now,
                       terminal_reason='active_lease.expired',
                       last_operation_id=p_operation_id
                 WHERE l.state='active'
                   AND l.lease_expires_at<=v_now;
                GET DIAGNOSTICS v_unknown = ROW_COUNT;

                PERFORM app_secure.pay24a_append_transition_event(
                    p_operation_id,'provider_admissions_expired',
                    v_request_digest,
                    v_authority.generation,v_authority.generation,
                    v_authority.stage,v_authority.stage,
                    v_authority.provider_egress_state,
                    v_authority.provider_egress_state,
                    v_authority.authorization_id,
                    v_authority.authorized_stage,
                    v_authority.certified_sha,v_authority.deployed_sha,
                    p_actor,v_authority.posture_digest,NULL,
                    pg_catalog.jsonb_build_object(
                        'expired_count',v_expired,
                        'unknown_count',v_unknown
                    )
                );
                RETURN QUERY SELECT v_expired,v_unknown;
            END
            $function$
            """
        )
        op.execute(
            r"""
            CREATE FUNCTION app_secure.pay24a_finalize_emergency_rollback(
                p_operation_id uuid,
                p_expected_generation bigint,
                p_actor text
            )
            RETURNS TABLE(
                generation bigint,
                stage smallint,
                provider_egress text
            )
            LANGUAGE plpgsql
            VOLATILE
            SECURITY DEFINER
            SET search_path=pg_catalog
            SET row_security=on
            AS $function$
            DECLARE
                v_before finance.payment_activation_authority%ROWTYPE;
                v_after finance.payment_activation_authority%ROWTYPE;
                v_existing finance.payment_activation_transition_events%ROWTYPE;
                v_now timestamptz := pg_catalog.clock_timestamp();
                v_request_digest text;
                v_undrained bigint;
                v_unknown bigint;
            BEGIN
                IF NOT pg_catalog.pg_has_role(
                    session_user,'finance_config_runtime','MEMBER'
                ) THEN
                    RAISE EXCEPTION
                        'PAY-24-A rollback finalization requires finance_config_runtime'
                        USING ERRCODE='42501';
                END IF;
                IF p_operation_id IS NULL
                   OR p_expected_generation IS NULL
                   OR p_expected_generation<0
                   OR p_actor IS NULL
                   OR char_length(pg_catalog.btrim(p_actor)) NOT BETWEEN 1 AND 128
                   OR p_actor ~ '[[:cntrl:]]'
                THEN
                    RAISE EXCEPTION
                        'PAY-24-A rollback finalization arguments invalid'
                        USING ERRCODE='22023';
                END IF;
                v_request_digest := pg_catalog.encode(
                    pg_catalog.sha256(
                        pg_catalog.convert_to(
                            pg_catalog.jsonb_build_object(
                                'operation_id',p_operation_id::text,
                                'expected_generation',p_expected_generation,
                                'actor',pg_catalog.btrim(p_actor)
                            )::text,
                            'utf8'
                        )
                    ),
                    'hex'
                );

                SELECT a.* INTO STRICT v_before
                  FROM finance.payment_activation_authority AS a
                 WHERE a.singleton
                 FOR UPDATE;
                SELECT e.* INTO v_existing
                  FROM finance.payment_activation_transition_events AS e
                 WHERE e.operation_id=p_operation_id;
                IF FOUND THEN
                    IF v_existing.event_type<>'emergency_rollback_finalized'
                       OR v_existing.request_digest<>v_request_digest
                    THEN
                        RAISE EXCEPTION
                            'PAY-24-A operation id conflicts with prior request'
                            USING ERRCODE='23505';
                    END IF;
                    RETURN QUERY SELECT
                        v_existing.new_generation,
                        v_existing.new_stage,
                        v_existing.new_provider_egress;
                    RETURN;
                END IF;
                IF v_before.generation<>p_expected_generation THEN
                    RAISE EXCEPTION
                        'PAY-24-A stale activation generation'
                        USING ERRCODE='40001';
                END IF;
                IF v_before.stage<>1
                   OR v_before.provider_egress_state<>'closing'
                THEN
                    RAISE EXCEPTION
                        'PAY-24-A rollback finalization requires closing Stage 1'
                        USING ERRCODE='23514';
                END IF;

                SELECT count(*)::bigint INTO v_undrained
                  FROM finance.provider_admission_leases AS l
                 WHERE l.state IN ('admitted','active');
                IF v_undrained<>0 THEN
                    RAISE EXCEPTION
                        'PAY-24-A rollback drain is incomplete'
                        USING ERRCODE='55000';
                END IF;
                SELECT count(*)::bigint INTO v_unknown
                  FROM finance.provider_admission_leases AS l
                 WHERE l.state='unknown';

                UPDATE finance.payment_activation_authority AS a
                   SET generation=v_before.generation+1,
                       stage=0,
                       provider_egress_state='blocked',
                       authorization_record_id=NULL,
                       authorization_id=NULL,
                       authorized_sha=NULL,
                       authorized_stage=NULL,
                       authorized_by=NULL,
                       authorized_at=NULL,
                       internal_organization_id=NULL,
                       checkout=false,
                       webhooks=false,
                       payment_application=false,
                       subscription_activation=false,
                       refund_execution=false,
                       recurring_billing=false,
                       dunning=false,
                       platform_billing=false,
                       last_operation_id=p_operation_id,
                       updated_at=v_now,
                       posture_digest=app_secure.pay24a_compute_posture_digest(
                           v_before.generation+1,
                           0::smallint,'blocked',NULL,
                           false,false,false,false,false,false,false,false,
                           v_before.release_identity_id,NULL,NULL
                       )
                 WHERE a.singleton
                 RETURNING a.* INTO STRICT v_after;

                PERFORM app_secure.pay24a_append_transition_event(
                    p_operation_id,'emergency_rollback_finalized',
                    v_request_digest,
                    v_before.generation,v_after.generation,
                    v_before.stage,v_after.stage,
                    v_before.provider_egress_state,
                    v_after.provider_egress_state,
                    v_before.authorization_id,v_before.authorized_stage,
                    v_after.certified_sha,v_after.deployed_sha,
                    p_actor,v_after.posture_digest,NULL,
                    pg_catalog.jsonb_build_object(
                        'drain','complete',
                        'unknown_outcome_count',v_unknown,
                        'authorization_cleared',true
                    )
                );
                RETURN QUERY SELECT
                    v_after.generation,v_after.stage,
                    v_after.provider_egress_state;
            END
            $function$
            """
        )

        for signature in _CONFIG_FUNCTIONS[2:]:
            op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
            op.execute(
                f"GRANT EXECUTE ON FUNCTION {signature} TO {_CONFIG}"
            )
        op.execute(f"REVOKE ALL ON FUNCTION {_EXPIRY_FUNCTION} FROM PUBLIC")
        op.execute(
            f"GRANT EXECUTE ON FUNCTION {_EXPIRY_FUNCTION} "
            f"TO {_CONFIG},{_FINANCE_MAINTENANCE},{_LIFECYCLE_MAINTENANCE}"
        )
    finally:
        op.execute("RESET ROLE")


def _install_admission_functions() -> None:
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        op.execute(
            r"""
            CREATE FUNCTION app_secure.pay24a_request_provider_admission(
                p_expected_generation bigint,
                p_capability text,
                p_logical_operation_id text,
                p_operation_sha text,
                p_lease_seconds integer
            )
            RETURNS TABLE(
                admission_id uuid,
                activation_generation bigint,
                organization_id uuid,
                capability text,
                logical_operation_id text,
                lease_expires_at timestamptz,
                state text
            )
            LANGUAGE plpgsql
            VOLATILE
            SECURITY DEFINER
            SET search_path=pg_catalog
            SET row_security=on
            AS $function$
            DECLARE
                v_authority finance.payment_activation_authority%ROWTYPE;
                v_existing finance.provider_admission_leases%ROWTYPE;
                v_inserted finance.provider_admission_leases%ROWTYPE;
                v_org_setting text;
                v_org uuid;
                v_now timestamptz := pg_catalog.clock_timestamp();
                v_admission_id uuid := pg_catalog.gen_random_uuid();
                v_capability_enabled boolean := false;
                v_outstanding_count bigint := 0;
            BEGIN
                IF p_expected_generation IS NULL
                   OR p_expected_generation<0
                   OR p_capability IS NULL
                   OR p_capability NOT IN (
                        'checkout','webhooks','payment_application',
                        'subscription_activation','refund_execution',
                        'recurring_billing','dunning','platform_billing'
                   )
                   OR p_logical_operation_id IS NULL
                   OR char_length(p_logical_operation_id) NOT BETWEEN 1 AND 128
                   OR p_logical_operation_id
                        !~ '^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$'
                   OR p_operation_sha IS NULL
                   OR p_operation_sha !~ '^[0-9a-f]{64}$'
                   OR p_lease_seconds IS NULL
                   OR p_lease_seconds NOT BETWEEN 5 AND 300
                THEN
                    RAISE EXCEPTION
                        'PAY-24-A provider admission arguments invalid'
                        USING ERRCODE='22023';
                END IF;

                IF p_capability='checkout' THEN
                    IF NOT pg_catalog.pg_has_role(
                        session_user,'finance_payment_runtime','MEMBER'
                    ) THEN
                        RAISE EXCEPTION
                            'PAY-24-A checkout admission requires finance_payment_runtime'
                            USING ERRCODE='42501';
                    END IF;
                ELSIF p_capability='refund_execution' THEN
                    IF NOT pg_catalog.pg_has_role(
                        session_user,'finance_refund_runtime','MEMBER'
                    ) THEN
                        RAISE EXCEPTION
                            'PAY-24-A refund admission requires finance_refund_runtime'
                            USING ERRCODE='42501';
                    END IF;
                ELSE
                    RAISE EXCEPTION
                        'PAY-24-A capability has no provider-admission runtime'
                        USING ERRCODE='42501';
                END IF;

                v_org_setting := pg_catalog.current_setting(
                    'app.current_org_id',true
                );
                IF v_org_setting IS NULL OR v_org_setting='' THEN
                    RAISE EXCEPTION
                        'PAY-24-A provider admission requires organization context'
                        USING ERRCODE='42501';
                END IF;
                BEGIN
                    v_org := v_org_setting::uuid;
                EXCEPTION WHEN invalid_text_representation THEN
                    RAISE EXCEPTION
                        'PAY-24-A provider admission organization context invalid'
                        USING ERRCODE='42501';
                END;

                SELECT a.* INTO STRICT v_authority
                  FROM finance.payment_activation_authority AS a
                 WHERE a.singleton
                 FOR UPDATE;
                IF v_authority.generation<>p_expected_generation THEN
                    RAISE EXCEPTION
                        'PAY-24-A stale activation generation'
                        USING ERRCODE='40001';
                END IF;
                IF v_authority.stage<>1
                   OR v_authority.provider_egress_state<>'open'
                   OR v_authority.internal_organization_id IS DISTINCT FROM v_org
                   OR v_authority.release_identity_id IS NULL
                   OR v_authority.authorization_record_id IS NULL
                   OR v_authority.certified_sha
                        IS DISTINCT FROM v_authority.deployed_sha
                   OR v_authority.certified_sha
                        IS DISTINCT FROM v_authority.authorized_sha
                   OR v_authority.authorized_stage<>1
                THEN
                    RAISE EXCEPTION
                        'PAY-24-A provider admission authority denied'
                        USING ERRCODE='42501';
                END IF;

                v_capability_enabled := CASE p_capability
                    WHEN 'checkout' THEN v_authority.checkout
                    WHEN 'webhooks' THEN v_authority.webhooks
                    WHEN 'payment_application'
                        THEN v_authority.payment_application
                    WHEN 'subscription_activation'
                        THEN v_authority.subscription_activation
                    WHEN 'refund_execution' THEN v_authority.refund_execution
                    WHEN 'recurring_billing'
                        THEN v_authority.recurring_billing
                    WHEN 'dunning' THEN v_authority.dunning
                    WHEN 'platform_billing'
                        THEN v_authority.platform_billing
                    ELSE false
                END;
                IF NOT v_capability_enabled THEN
                    RAISE EXCEPTION
                        'PAY-24-A provider admission capability disabled'
                        USING ERRCODE='42501';
                END IF;

                SELECT l.* INTO v_existing
                  FROM finance.provider_admission_leases AS l
                 WHERE l.organization_id=v_org
                   AND l.capability=p_capability
                   AND l.logical_operation_id=p_logical_operation_id
                 FOR UPDATE;
                IF FOUND THEN
                    IF v_existing.operation_sha<>p_operation_sha THEN
                        RAISE EXCEPTION
                            'PAY-24-A logical provider operation conflicts'
                            USING ERRCODE='23505';
                    END IF;
                    RETURN QUERY SELECT
                        v_existing.admission_id,
                        v_existing.activation_generation,
                        v_existing.organization_id,
                        v_existing.capability,
                        v_existing.logical_operation_id,
                        v_existing.lease_expires_at,
                        v_existing.state;
                    RETURN;
                END IF;

                -- The singleton lock held above serializes this capacity
                -- check with every admission request and rollback begin.
                SELECT count(*)::bigint INTO v_outstanding_count
                  FROM finance.provider_admission_leases AS l
                 WHERE l.organization_id=v_org
                   AND l.capability=p_capability
                   AND l.state IN ('admitted','active');
                IF v_outstanding_count>=__PAY24A_MAX_OUTSTANDING__ THEN
                    RAISE EXCEPTION
                        'PAY-24-A provider admission capacity reached'
                        USING ERRCODE='54000';
                END IF;

                INSERT INTO finance.provider_admission_leases(
                    admission_id,activation_generation,organization_id,
                    capability,logical_operation_id,operation_sha,
                    lease_expires_at,state,execution_id,
                    admitted_at,started_at,finished_at,
                    terminal_reason,last_operation_id
                ) VALUES (
                    v_admission_id,v_authority.generation,v_org,
                    p_capability,p_logical_operation_id,p_operation_sha,
                    v_now+pg_catalog.make_interval(secs=>p_lease_seconds),
                    'admitted',NULL,
                    v_now,NULL,NULL,NULL,NULL
                )
                RETURNING * INTO STRICT v_inserted;

                RETURN QUERY SELECT
                    v_inserted.admission_id,
                    v_inserted.activation_generation,
                    v_inserted.organization_id,
                    v_inserted.capability,
                    v_inserted.logical_operation_id,
                    v_inserted.lease_expires_at,
                    v_inserted.state;
            END
            $function$
            """.replace(
                "__PAY24A_MAX_OUTSTANDING__",
                str(_MAX_OUTSTANDING_ADMISSIONS),
            )
        )
        op.execute(
            r"""
            CREATE FUNCTION app_secure.pay24a_start_provider_admission(
                p_admission_id uuid,
                p_execution_id uuid
            )
            RETURNS TABLE(
                admission_id uuid,
                activation_generation bigint,
                organization_id uuid,
                capability text,
                logical_operation_id text,
                lease_expires_at timestamptz,
                execution_id uuid,
                state text
            )
            LANGUAGE plpgsql
            VOLATILE
            SECURITY DEFINER
            SET search_path=pg_catalog
            SET row_security=on
            AS $function$
            DECLARE
                v_authority finance.payment_activation_authority%ROWTYPE;
                v_lease record;
                v_org_setting text;
                v_org uuid;
                v_now timestamptz := pg_catalog.clock_timestamp();
                v_capability_enabled boolean := false;
            BEGIN
                IF p_admission_id IS NULL OR p_execution_id IS NULL THEN
                    RAISE EXCEPTION
                        'PAY-24-A provider admission start arguments invalid'
                        USING ERRCODE='22023';
                END IF;
                v_org_setting := pg_catalog.current_setting(
                    'app.current_org_id',true
                );
                IF v_org_setting IS NULL OR v_org_setting='' THEN
                    RAISE EXCEPTION
                        'PAY-24-A provider admission start requires organization context'
                        USING ERRCODE='42501';
                END IF;
                BEGIN
                    v_org := v_org_setting::uuid;
                EXCEPTION WHEN invalid_text_representation THEN
                    RAISE EXCEPTION
                        'PAY-24-A provider admission organization context invalid'
                        USING ERRCODE='42501';
                END;

                SELECT a.* INTO STRICT v_authority
                  FROM finance.payment_activation_authority AS a
                 WHERE a.singleton
                 FOR UPDATE;
                SELECT l.*,l.xmin::text AS insertion_xid INTO v_lease
                  FROM finance.provider_admission_leases AS l
                 WHERE l.admission_id=p_admission_id
                 FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'PAY-24-A provider admission unavailable'
                        USING ERRCODE='42501';
                END IF;

                IF v_lease.capability='checkout' THEN
                    IF NOT pg_catalog.pg_has_role(
                        session_user,'finance_payment_runtime','MEMBER'
                    ) THEN
                        RAISE EXCEPTION
                            'PAY-24-A checkout start requires finance_payment_runtime'
                            USING ERRCODE='42501';
                    END IF;
                ELSIF v_lease.capability='refund_execution' THEN
                    IF NOT pg_catalog.pg_has_role(
                        session_user,'finance_refund_runtime','MEMBER'
                    ) THEN
                        RAISE EXCEPTION
                            'PAY-24-A refund start requires finance_refund_runtime'
                            USING ERRCODE='42501';
                    END IF;
                ELSE
                    RAISE EXCEPTION
                        'PAY-24-A admission capability has no start runtime'
                        USING ERRCODE='42501';
                END IF;
                IF v_lease.organization_id IS DISTINCT FROM v_org THEN
                    RAISE EXCEPTION
                        'PAY-24-A cross-tenant admission start denied'
                        USING ERRCODE='42501';
                END IF;

                -- The provider adapter must never turn an admission into an
                -- outbound side effect until the request transaction has
                -- committed.  xmin is the inserting top-level transaction;
                -- compare its unsigned 32-bit value with the low 32 bits of
                -- PostgreSQL 16's epoch-aware xid8 for the current transaction.
                IF v_lease.insertion_xid::bigint =
                   pg_catalog.mod(
                       pg_catalog.pg_current_xact_id()::text::numeric,
                       4294967296::numeric
                   )::bigint
                THEN
                    RAISE EXCEPTION
                        'PAY-24-A provider admission must commit before start'
                        USING ERRCODE='55000';
                END IF;

                IF v_lease.execution_id=p_execution_id THEN
                    IF v_lease.state='active' THEN
                        RAISE EXCEPTION
                            'PAY-24-A provider execution already started; reconcile before retry'
                            USING ERRCODE='55000';
                    END IF;
                    RETURN QUERY SELECT
                        v_lease.admission_id,
                        v_lease.activation_generation,
                        v_lease.organization_id,
                        v_lease.capability,
                        v_lease.logical_operation_id,
                        v_lease.lease_expires_at,
                        v_lease.execution_id,
                        v_lease.state;
                    RETURN;
                END IF;
                IF v_lease.state<>'admitted' THEN
                    RAISE EXCEPTION
                        'PAY-24-A provider admission is not startable'
                        USING ERRCODE='55000';
                END IF;
                IF v_authority.generation<>v_lease.activation_generation
                   OR v_authority.stage<>1
                   OR v_authority.provider_egress_state<>'open'
                   OR v_authority.internal_organization_id
                        IS DISTINCT FROM v_org
                   OR v_authority.certified_sha
                        IS DISTINCT FROM v_authority.deployed_sha
                   OR v_authority.certified_sha
                        IS DISTINCT FROM v_authority.authorized_sha
                   OR v_authority.authorized_stage<>1
                THEN
                    RAISE EXCEPTION
                        'PAY-24-A provider admission generation is stale'
                        USING ERRCODE='42501';
                END IF;
                v_capability_enabled := CASE v_lease.capability
                    WHEN 'checkout' THEN v_authority.checkout
                    WHEN 'refund_execution' THEN v_authority.refund_execution
                    ELSE false
                END;
                IF NOT v_capability_enabled THEN
                    RAISE EXCEPTION
                        'PAY-24-A provider admission capability disabled'
                        USING ERRCODE='42501';
                END IF;

                IF v_lease.lease_expires_at<=v_now THEN
                    UPDATE finance.provider_admission_leases AS l
                       SET state='expired',
                           finished_at=v_now,
                           terminal_reason='admission.expired',
                           last_operation_id=p_execution_id
                     WHERE l.admission_id=p_admission_id
                     RETURNING l.* INTO STRICT v_lease;
                    RETURN QUERY SELECT
                        v_lease.admission_id,
                        v_lease.activation_generation,
                        v_lease.organization_id,
                        v_lease.capability,
                        v_lease.logical_operation_id,
                        v_lease.lease_expires_at,
                        v_lease.execution_id,
                        v_lease.state;
                    RETURN;
                END IF;
                IF EXISTS(
                    SELECT 1
                    FROM finance.provider_admission_leases AS other_lease
                    WHERE other_lease.execution_id=p_execution_id
                      AND other_lease.admission_id<>p_admission_id
                ) THEN
                    RAISE EXCEPTION
                        'PAY-24-A execution id is already bound'
                        USING ERRCODE='23505';
                END IF;

                UPDATE finance.provider_admission_leases AS l
                   SET state='active',
                       execution_id=p_execution_id,
                       started_at=v_now,
                       last_operation_id=p_execution_id
                 WHERE l.admission_id=p_admission_id
                 RETURNING l.* INTO STRICT v_lease;
                RETURN QUERY SELECT
                    v_lease.admission_id,
                    v_lease.activation_generation,
                    v_lease.organization_id,
                    v_lease.capability,
                    v_lease.logical_operation_id,
                    v_lease.lease_expires_at,
                    v_lease.execution_id,
                    v_lease.state;
            END
            $function$
            """
        )
        op.execute(
            r"""
            CREATE FUNCTION app_secure.pay24a_finish_provider_admission(
                p_admission_id uuid,
                p_execution_id uuid,
                p_outcome text
            )
            RETURNS TABLE(
                admission_id uuid,
                state text,
                finished_at timestamptz
            )
            LANGUAGE plpgsql
            VOLATILE
            SECURITY DEFINER
            SET search_path=pg_catalog
            SET row_security=on
            AS $function$
            DECLARE
                v_authority finance.payment_activation_authority%ROWTYPE;
                v_lease finance.provider_admission_leases%ROWTYPE;
                v_org_setting text;
                v_org uuid;
                v_now timestamptz := pg_catalog.clock_timestamp();
                v_reason text;
            BEGIN
                IF p_admission_id IS NULL
                   OR p_execution_id IS NULL
                   OR p_outcome IS NULL
                   OR p_outcome NOT IN ('completed','unknown')
                THEN
                    RAISE EXCEPTION
                        'PAY-24-A provider admission finish arguments invalid'
                        USING ERRCODE='22023';
                END IF;
                v_org_setting := pg_catalog.current_setting(
                    'app.current_org_id',true
                );
                IF v_org_setting IS NULL OR v_org_setting='' THEN
                    RAISE EXCEPTION
                        'PAY-24-A provider admission finish requires organization context'
                        USING ERRCODE='42501';
                END IF;
                BEGIN
                    v_org := v_org_setting::uuid;
                EXCEPTION WHEN invalid_text_representation THEN
                    RAISE EXCEPTION
                        'PAY-24-A provider admission organization context invalid'
                        USING ERRCODE='42501';
                END;

                SELECT a.* INTO STRICT v_authority
                  FROM finance.payment_activation_authority AS a
                 WHERE a.singleton
                 FOR UPDATE;
                SELECT l.* INTO v_lease
                  FROM finance.provider_admission_leases AS l
                 WHERE l.admission_id=p_admission_id
                 FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'PAY-24-A provider admission unavailable'
                        USING ERRCODE='42501';
                END IF;

                IF v_lease.capability='checkout' THEN
                    IF NOT pg_catalog.pg_has_role(
                        session_user,'finance_payment_runtime','MEMBER'
                    ) THEN
                        RAISE EXCEPTION
                            'PAY-24-A checkout finish requires finance_payment_runtime'
                            USING ERRCODE='42501';
                    END IF;
                ELSIF v_lease.capability='refund_execution' THEN
                    IF NOT pg_catalog.pg_has_role(
                        session_user,'finance_refund_runtime','MEMBER'
                    ) THEN
                        RAISE EXCEPTION
                            'PAY-24-A refund finish requires finance_refund_runtime'
                            USING ERRCODE='42501';
                    END IF;
                ELSE
                    RAISE EXCEPTION
                        'PAY-24-A admission capability has no finish runtime'
                        USING ERRCODE='42501';
                END IF;
                IF v_lease.organization_id IS DISTINCT FROM v_org THEN
                    RAISE EXCEPTION
                        'PAY-24-A cross-tenant admission finish denied'
                        USING ERRCODE='42501';
                END IF;
                IF v_lease.execution_id IS DISTINCT FROM p_execution_id THEN
                    RAISE EXCEPTION
                        'PAY-24-A provider admission execution fence mismatch'
                        USING ERRCODE='42501';
                END IF;

                IF v_lease.state=p_outcome THEN
                    RETURN QUERY SELECT
                        v_lease.admission_id,v_lease.state,v_lease.finished_at;
                    RETURN;
                END IF;
                IF v_lease.state<>'active' THEN
                    RAISE EXCEPTION
                        'PAY-24-A provider admission outcome conflicts'
                        USING ERRCODE='55000';
                END IF;
                v_reason := CASE p_outcome
                    WHEN 'completed' THEN 'provider.completed'
                    ELSE 'provider.outcome_unknown'
                END;
                UPDATE finance.provider_admission_leases AS l
                   SET state=p_outcome,
                       finished_at=v_now,
                       terminal_reason=v_reason,
                       last_operation_id=p_execution_id
                 WHERE l.admission_id=p_admission_id
                 RETURNING l.* INTO STRICT v_lease;
                RETURN QUERY SELECT
                    v_lease.admission_id,v_lease.state,v_lease.finished_at;
            END
            $function$
            """
        )

        for signature in _ADMISSION_FUNCTIONS:
            op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
            op.execute(
                f"GRANT EXECUTE ON FUNCTION {signature} "
                f"TO {_PAYMENT},{_REFUND}"
            )
    finally:
        op.execute("RESET ROLE")


def _install_read_functions() -> None:
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        op.execute(
            r"""
            CREATE FUNCTION app_secure.pay24a_activation_snapshot()
            RETURNS TABLE(
                stage smallint,
                generation bigint,
                provider_egress text,
                enabled_capabilities text[],
                internal_organization_id uuid,
                authorization_id text,
                authorized_stage smallint,
                certified_sha text,
                deployed_sha text,
                updated_at timestamptz,
                posture_digest text
            )
            LANGUAGE plpgsql
            STABLE
            SECURITY DEFINER
            SET search_path=pg_catalog
            SET row_security=on
            AS $function$
            BEGIN
                IF NOT (
                    pg_catalog.pg_has_role(
                        session_user,'finance_config_runtime','MEMBER'
                    )
                    OR pg_catalog.pg_has_role(
                        session_user,'finance_read_runtime','MEMBER'
                    )
                    OR pg_catalog.pg_has_role(
                        session_user,'finance_maintenance_runtime','MEMBER'
                    )
                    OR pg_catalog.pg_has_role(
                        session_user,'lifecycle_maintenance_runtime','MEMBER'
                    )
                ) THEN
                    RAISE EXCEPTION
                        'PAY-24-A activation snapshot requires operational capability'
                        USING ERRCODE='42501';
                END IF;
                RETURN QUERY
                SELECT
                    a.stage,a.generation,a.provider_egress_state,
                    pg_catalog.array_remove(
                        ARRAY[
                            CASE WHEN a.checkout THEN 'checkout' END,
                            CASE WHEN a.webhooks THEN 'webhooks' END,
                            CASE WHEN a.payment_application
                                THEN 'payment_application' END,
                            CASE WHEN a.subscription_activation
                                THEN 'subscription_activation' END,
                            CASE WHEN a.refund_execution
                                THEN 'refund_execution' END,
                            CASE WHEN a.recurring_billing
                                THEN 'recurring_billing' END,
                            CASE WHEN a.dunning THEN 'dunning' END,
                            CASE WHEN a.platform_billing
                                THEN 'platform_billing' END
                        ]::text[],
                        NULL
                    ),
                    a.internal_organization_id,
                    a.authorization_id,a.authorized_stage,
                    a.certified_sha,a.deployed_sha,
                    a.updated_at,a.posture_digest::text
                FROM finance.payment_activation_authority AS a
                WHERE a.singleton;
            END
            $function$
            """
        )
        op.execute(
            r"""
            CREATE FUNCTION app_secure.pay24a_transition_evidence(
                p_after_event_sequence bigint,
                p_limit integer
            )
            RETURNS TABLE(
                event_sequence bigint,
                event_id uuid,
                operation_id uuid,
                event_type text,
                prior_generation bigint,
                new_generation bigint,
                prior_stage smallint,
                new_stage smallint,
                authorization_id text,
                authorized_stage smallint,
                certified_sha text,
                deployed_sha text,
                database_principal text,
                actor text,
                occurred_at timestamptz,
                posture_digest text,
                previous_event_hash text,
                event_hash text,
                event_details jsonb
            )
            LANGUAGE plpgsql
            STABLE
            SECURITY DEFINER
            SET search_path=pg_catalog
            SET row_security=on
            AS $function$
            BEGIN
                IF NOT (
                    pg_catalog.pg_has_role(
                        session_user,'finance_config_runtime','MEMBER'
                    )
                    OR pg_catalog.pg_has_role(
                        session_user,'finance_read_runtime','MEMBER'
                    )
                    OR pg_catalog.pg_has_role(
                        session_user,'finance_maintenance_runtime','MEMBER'
                    )
                    OR pg_catalog.pg_has_role(
                        session_user,'lifecycle_maintenance_runtime','MEMBER'
                    )
                ) THEN
                    RAISE EXCEPTION
                        'PAY-24-A transition evidence requires operational capability'
                        USING ERRCODE='42501';
                END IF;
                IF p_after_event_sequence IS NULL
                   OR p_after_event_sequence<0
                   OR p_limit IS NULL
                   OR p_limit NOT BETWEEN 1 AND 100
                THEN
                    RAISE EXCEPTION
                        'PAY-24-A transition evidence bounds invalid'
                        USING ERRCODE='22023';
                END IF;
                RETURN QUERY
                SELECT
                    e.event_sequence,e.event_id,e.operation_id,e.event_type,
                    e.prior_generation,e.new_generation,
                    e.prior_stage,e.new_stage,
                    e.authorization_id,e.authorized_stage,
                    e.certified_sha,e.deployed_sha,
                    e.database_principal,e.actor,e.occurred_at,
                    e.posture_digest::text,
                    e.previous_event_hash::text,e.event_hash::text,
                    e.event_details
                FROM finance.payment_activation_transition_events AS e
                WHERE e.event_sequence>p_after_event_sequence
                ORDER BY e.event_sequence
                LIMIT p_limit;
            END
            $function$
            """
        )
        op.execute(
            r"""
            CREATE FUNCTION app_secure.pay24a_admission_drain_snapshot()
            RETURNS TABLE(
                activation_generation bigint,
                state text,
                lease_count bigint,
                oldest_lease_expiry timestamptz,
                newest_lease_expiry timestamptz
            )
            LANGUAGE plpgsql
            STABLE
            SECURITY DEFINER
            SET search_path=pg_catalog
            SET row_security=on
            AS $function$
            BEGIN
                IF NOT (
                    pg_catalog.pg_has_role(
                        session_user,'finance_config_runtime','MEMBER'
                    )
                    OR pg_catalog.pg_has_role(
                        session_user,'finance_read_runtime','MEMBER'
                    )
                    OR pg_catalog.pg_has_role(
                        session_user,'finance_maintenance_runtime','MEMBER'
                    )
                    OR pg_catalog.pg_has_role(
                        session_user,'lifecycle_maintenance_runtime','MEMBER'
                    )
                ) THEN
                    RAISE EXCEPTION
                        'PAY-24-A drain snapshot requires operational capability'
                        USING ERRCODE='42501';
                END IF;
                RETURN QUERY
                SELECT
                    grouped.activation_generation,
                    grouped.state,
                    grouped.lease_count,
                    grouped.oldest_lease_expiry,
                    grouped.newest_lease_expiry
                FROM (
                    SELECT
                        l.activation_generation,
                        l.state,
                        count(*)::bigint AS lease_count,
                        min(l.lease_expires_at) AS oldest_lease_expiry,
                        max(l.lease_expires_at) AS newest_lease_expiry
                    FROM finance.provider_admission_leases AS l
                    WHERE l.state IN ('admitted','active','unknown')
                    GROUP BY l.activation_generation,l.state
                    ORDER BY l.activation_generation DESC,l.state
                    LIMIT 100
                ) AS grouped
                ORDER BY grouped.activation_generation,grouped.state;
            END
            $function$
            """
        )

        for signature in _READ_FUNCTIONS:
            op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
            op.execute(
                f"GRANT EXECUTE ON FUNCTION {signature} "
                f"TO {_CONFIG},{_READ},{_FINANCE_MAINTENANCE},"
                f"{_LIFECYCLE_MAINTENANCE}"
            )
        op.execute(
            f"GRANT USAGE ON SCHEMA app_secure "
            f"TO {_READ},{_FINANCE_MAINTENANCE}"
        )
    finally:
        op.execute("RESET ROLE")


def _expected_execute_matrix() -> dict[str, set[str]]:
    matrix = {signature: set() for signature in _FUNCTION_SIGNATURES}
    for signature in _CONFIG_FUNCTIONS:
        matrix[signature].add(_CONFIG)
    for signature in _ADMISSION_FUNCTIONS:
        matrix[signature].update((_PAYMENT, _REFUND))
    matrix[_EXPIRY_FUNCTION].update(
        (_CONFIG, _FINANCE_MAINTENANCE, _LIFECYCLE_MAINTENANCE)
    )
    for signature in _READ_FUNCTIONS:
        matrix[signature].update(
            (_CONFIG, _READ, _FINANCE_MAINTENANCE, _LIFECYCLE_MAINTENANCE)
        )
    return matrix


def _postflight_relations_and_acls(bind) -> None:
    for table_name in _TABLES:
        row = bind.execute(
            sa.text(
                """
                SELECT owner.rolname,c.relrowsecurity,c.relforcerowsecurity
                FROM pg_catalog.pg_class AS c
                JOIN pg_catalog.pg_namespace AS n ON n.oid=c.relnamespace
                JOIN pg_catalog.pg_roles AS owner ON owner.oid=c.relowner
                WHERE n.nspname='finance' AND c.relname=:table_name
                """
            ),
            {"table_name": table_name},
        ).one_or_none()
        if row is None:
            raise RuntimeError(
                f"PAY-24-A relation missing after install: finance.{table_name}"
            )
        if row[0] != _MIGRATION_OWNER or row[1] is not True or row[2] is not True:
            raise RuntimeError(
                f"PAY-24-A owner/RLS drift: finance.{table_name}"
            )

    runtime_roles = (
        _CONFIG,
        _PAYMENT,
        _REFUND,
        _READ,
        _FINANCE_MAINTENANCE,
        _LIFECYCLE_MAINTENANCE,
        "app_runtime",
        "worker_runtime",
    )
    for role in runtime_roles:
        for table_name in _TABLES:
            for privilege in (
                "SELECT",
                "INSERT",
                "UPDATE",
                "DELETE",
                "TRUNCATE",
                "REFERENCES",
                "TRIGGER",
            ):
                if bind.execute(
                    sa.text(
                        "SELECT pg_catalog.has_table_privilege("
                        ":role,:relation,:privilege)"
                    ),
                    {
                        "role": role,
                        "relation": f"finance.{table_name}",
                        "privilege": privilege,
                    },
                ).scalar_one():
                    raise RuntimeError(
                        "PAY-24-A leaked direct table authority: "
                        f"{role} -> finance.{table_name}.{privilege}"
                    )
    public_relation = bind.execute(
        sa.text(
            """
            SELECT n.nspname||'.'||c.relname
            FROM pg_catalog.pg_class AS c
            JOIN pg_catalog.pg_namespace AS n ON n.oid=c.relnamespace
            CROSS JOIN LATERAL pg_catalog.aclexplode(
                COALESCE(
                    c.relacl,
                    pg_catalog.acldefault('r',c.relowner)
                )
            ) AS acl
            WHERE n.nspname='finance'
              AND c.relname = ANY(CAST(:tables AS text[]))
              AND acl.grantee=0
            LIMIT 1
            """
        ),
        {"tables": list(_TABLES)},
    ).scalar_one_or_none()
    if public_relation is not None:
        raise RuntimeError(
            "PAY-24-A PUBLIC retained table authority: " + public_relation
        )


def _postflight_functions(bind, *, had_create: bool) -> None:
    runtime_roles = (
        _CONFIG,
        _PAYMENT,
        _REFUND,
        _READ,
        _FINANCE_MAINTENANCE,
        _LIFECYCLE_MAINTENANCE,
        "app_runtime",
        "worker_runtime",
    )
    expected_matrix = _expected_execute_matrix()
    for signature, expected_roles in expected_matrix.items():
        proc = bind.execute(
            sa.text(
                """
                SELECT p.oid,owner.rolname,p.prosecdef,p.proconfig
                FROM pg_catalog.pg_proc AS p
                JOIN pg_catalog.pg_roles AS owner ON owner.oid=p.proowner
                WHERE p.oid=pg_catalog.to_regprocedure(:signature)
                """
            ),
            {"signature": signature},
        ).one_or_none()
        if proc is None:
            raise RuntimeError(
                "PAY-24-A function missing after install: " + signature
            )
        function_oid, owner, security_definer, config = proc
        config_values = set(config or ())
        if owner != _SECURITY_OWNER or security_definer is not True:
            raise RuntimeError(
                "PAY-24-A function owner/security drift: " + signature
            )
        if "search_path=pg_catalog" not in config_values:
            raise RuntimeError(
                "PAY-24-A function lost fixed search_path: " + signature
            )
        if "row_security=on" not in config_values:
            raise RuntimeError(
                "PAY-24-A function lost row_security=on: " + signature
            )
        if bind.execute(
            sa.text(
                """
                SELECT EXISTS(
                    SELECT 1
                    FROM pg_catalog.pg_proc AS public_proc
                    CROSS JOIN LATERAL pg_catalog.aclexplode(
                        COALESCE(
                            public_proc.proacl,
                            pg_catalog.acldefault('f',public_proc.proowner)
                        )
                    ) AS acl
                    WHERE public_proc.oid=CAST(:oid AS oid)
                      AND acl.grantee=0
                      AND acl.privilege_type='EXECUTE'
                )
                """
            ),
            {"oid": function_oid},
        ).scalar_one():
            raise RuntimeError(
                "PAY-24-A PUBLIC retained function EXECUTE: " + signature
            )

        for role in runtime_roles:
            actual = bool(
                bind.execute(
                    sa.text(
                        "SELECT pg_catalog.has_function_privilege("
                        ":role,CAST(:oid AS oid),'EXECUTE')"
                    ),
                    {"role": role, "oid": function_oid},
                ).scalar_one()
            )
            if actual is not (role in expected_roles):
                raise RuntimeError(
                    "PAY-24-A function ACL drift: "
                    f"{role} -> {signature}"
                )

    for role in (_READ, _FINANCE_MAINTENANCE):
        if not bind.execute(
            sa.text(
                "SELECT pg_catalog.has_schema_privilege("
                ":role,'app_secure','USAGE')"
            ),
            {"role": role},
        ).scalar_one():
            raise RuntimeError(
                "PAY-24-A bounded function role lacks app_secure USAGE: "
                + role
            )

    if not had_create and bind.execute(
        sa.text(
            "SELECT pg_catalog.has_schema_privilege("
            "'app_security_owner','app_secure','CREATE')"
        )
    ).scalar_one():
        raise RuntimeError(
            "PAY-24-A temporary app_secure CREATE authority leaked"
        )


def _postflight_triggers_and_initial_state(bind) -> None:
    expected_triggers = (
        (
            "payment_activation_release_identities",
            "trg_pay24a_release_identity_immutable",
        ),
        (
            "payment_activation_authorizations",
            "trg_pay24a_authorization_immutable",
        ),
        (
            "payment_activation_transition_events",
            "trg_pay24a_transition_event_immutable",
        ),
        (
            "payment_activation_authority",
            "trg_pay24a_authority_no_insert_or_erase",
        ),
        (
            "payment_activation_authority",
            "trg_pay24a_authority_update_guard",
        ),
        (
            "payment_activation_authority",
            "trg_pay24a_authority_evidence",
        ),
        (
            "provider_admission_leases",
            "trg_pay24a_admission_no_erase",
        ),
        (
            "provider_admission_leases",
            "trg_pay24a_admission_update_guard",
        ),
    )
    for table_name, trigger_name in expected_triggers:
        count = bind.execute(
            sa.text(
                """
                SELECT count(*)
                FROM pg_catalog.pg_trigger AS t
                JOIN pg_catalog.pg_class AS c ON c.oid=t.tgrelid
                JOIN pg_catalog.pg_namespace AS n ON n.oid=c.relnamespace
                WHERE n.nspname='finance'
                  AND c.relname=:table_name
                  AND t.tgname=:trigger_name
                  AND NOT t.tgisinternal
                  AND t.tgenabled='O'
                """
            ),
            {"table_name": table_name, "trigger_name": trigger_name},
        ).scalar_one()
        if int(count) != 1:
            raise RuntimeError(
                "PAY-24-A immutable/fencing trigger missing: " + trigger_name
            )

    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        authority = bind.execute(
            sa.text(
                """
                SELECT a.*,
                       app_secure.pay24a_compute_posture_digest(
                           a.generation,a.stage,a.provider_egress_state,
                           a.internal_organization_id,
                           a.checkout,a.webhooks,a.payment_application,
                           a.subscription_activation,a.refund_execution,
                           a.recurring_billing,a.dunning,a.platform_billing,
                           a.release_identity_id,a.authorization_record_id,
                           a.authorization_id
                       ) AS expected_digest
                FROM finance.payment_activation_authority AS a
                """
            )
        ).mappings().all()
    finally:
        op.execute("RESET ROLE")
    if len(authority) != 1:
        raise RuntimeError("PAY-24-A deterministic singleton is missing")
    initial = authority[0]
    if (
        int(initial["generation"]) != 0
        or int(initial["stage"]) != 0
        or initial["provider_egress_state"] != "blocked"
        or initial["internal_organization_id"] is not None
        or initial["authorization_record_id"] is not None
        or initial["release_identity_id"] is not None
        or any(
            bool(initial[name])
            for name in (
                "checkout",
                "webhooks",
                "payment_application",
                "subscription_activation",
                "refund_execution",
                "recurring_billing",
                "dunning",
                "platform_billing",
            )
        )
        or initial["posture_digest"] != initial["expected_digest"]
    ):
        raise RuntimeError("PAY-24-A initial Stage-0 authority is not pristine")


def _postflight(bind, *, had_create: bool) -> None:
    _postflight_relations_and_acls(bind)
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        _postflight_functions(bind, had_create=had_create)
    finally:
        op.execute("RESET ROLE")
    _postflight_triggers_and_initial_state(bind)


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='60s'")
    _require_identity(bind)
    _preflight(bind)

    had_create = bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.has_schema_privilege("
                "'app_security_owner','app_secure','CREATE')"
            )
        ).scalar_one()
    )
    _install_tables()
    _install_security_owner_table_boundary()
    _install_helpers_and_guards(bind, had_create=had_create)
    _install_release_and_authorization_functions()
    _install_transition_and_rollback_functions()
    _install_admission_functions()
    _install_read_functions()

    if not had_create:
        op.execute(
            "REVOKE CREATE ON SCHEMA app_secure FROM app_security_owner"
        )
    _postflight(bind, had_create=had_create)


def _require_pristine_downgrade(bind) -> None:
    unforced: list[str] = []
    try:
        for table_name in _TABLES:
            op.execute(
                f"ALTER TABLE finance.{table_name} "
                "NO FORCE ROW LEVEL SECURITY"
            )
            unforced.append(table_name)

        counts = {
            table_name: int(
                bind.execute(
                    sa.text(
                        f"SELECT count(*) FROM finance.{table_name}"
                    )
                ).scalar_one()
            )
            for table_name in (
                "payment_activation_release_identities",
                "payment_activation_authorizations",
                "payment_activation_transition_events",
                "provider_admission_leases",
            )
        }
        authority_rows = bind.execute(
            sa.text("SELECT * FROM finance.payment_activation_authority")
        ).mappings().all()
    finally:
        # Restore FORCE before evaluating/refusing the downgrade.  A failed
        # decision must never leave the global authority owner-visible.
        for table_name in reversed(unforced):
            op.execute(
                f"ALTER TABLE finance.{table_name} FORCE ROW LEVEL SECURITY"
            )

    pristine = len(authority_rows) == 1
    if pristine:
        authority = authority_rows[0]
        pristine = (
            int(authority["generation"]) == 0
            and int(authority["stage"]) == 0
            and authority["provider_egress_state"] == "blocked"
            and authority["release_identity_id"] is None
            and authority["certified_sha"] is None
            and authority["deployed_sha"] is None
            and authority["authorization_record_id"] is None
            and authority["authorization_id"] is None
            and authority["authorized_sha"] is None
            and authority["authorized_stage"] is None
            and authority["authorized_by"] is None
            and authority["authorized_at"] is None
            and authority["internal_organization_id"] is None
            and authority["last_operation_id"] is None
            and not any(
                bool(authority[name])
                for name in (
                    "checkout",
                    "webhooks",
                    "payment_application",
                    "subscription_activation",
                    "refund_execution",
                    "recurring_billing",
                    "dunning",
                    "platform_billing",
                )
            )
        )
    if not pristine or any(counts.values()):
        raise RuntimeError(
            "PAY-24-A downgrade refuses to destroy durable activation, "
            "authorization, transition, generation, or provider-admission evidence"
        )


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='60s'")
    _require_identity(bind)
    _require_pristine_downgrade(bind)

    triggers = (
        (
            "payment_activation_release_identities",
            "trg_pay24a_release_identity_immutable",
        ),
        (
            "payment_activation_authorizations",
            "trg_pay24a_authorization_immutable",
        ),
        (
            "payment_activation_transition_events",
            "trg_pay24a_transition_event_immutable",
        ),
        (
            "payment_activation_authority",
            "trg_pay24a_authority_no_insert_or_erase",
        ),
        (
            "payment_activation_authority",
            "trg_pay24a_authority_update_guard",
        ),
        (
            "payment_activation_authority",
            "trg_pay24a_authority_evidence",
        ),
        (
            "provider_admission_leases",
            "trg_pay24a_admission_no_erase",
        ),
        (
            "provider_admission_leases",
            "trg_pay24a_admission_update_guard",
        ),
    )
    for table_name, trigger_name in triggers:
        op.execute(
            f"DROP TRIGGER {trigger_name} ON finance.{table_name} RESTRICT"
        )

    for table_name in _TABLES:
        op.execute(
            f"DROP POLICY pay24a_{table_name}_security_owner "
            f"ON finance.{table_name}"
        )

    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        for signature in reversed(_FUNCTION_SIGNATURES):
            op.execute(f"DROP FUNCTION {signature} RESTRICT")
        op.execute(
            f"REVOKE USAGE ON SCHEMA app_secure "
            f"FROM {_READ},{_FINANCE_MAINTENANCE}"
        )
    finally:
        op.execute("RESET ROLE")

    grants = {
        "payment_activation_release_identities": "SELECT,INSERT",
        "payment_activation_authorizations": "SELECT,INSERT",
        "payment_activation_authority": "SELECT,UPDATE",
        "payment_activation_transition_events": "SELECT,INSERT",
        "provider_admission_leases": "SELECT,INSERT,UPDATE",
    }
    for table_name, privileges in grants.items():
        op.execute(
            f"REVOKE {privileges} ON TABLE finance.{table_name} "
            f"FROM {_SECURITY_OWNER}"
        )

    op.execute("DROP TABLE finance.payment_activation_authority RESTRICT")
    op.execute("DROP TABLE finance.provider_admission_leases RESTRICT")
    op.execute(
        "DROP TABLE finance.payment_activation_transition_events RESTRICT"
    )
    op.execute(
        "DROP TABLE finance.payment_activation_authorizations RESTRICT"
    )
    op.execute(
        "DROP TABLE finance.payment_activation_release_identities RESTRICT"
    )
