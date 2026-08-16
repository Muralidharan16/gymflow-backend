"""Add fenced provider-state drift repair for P4B search reconciliation.

Revision ID: v07d8e9f0a36
Revises: u07d8e9f0a35
Create Date: 2026-08-16

The P4B worker may discover that OpenSearch contains a document at the same or a
higher external version than PostgreSQL's current desired projection. Strict
external versioning correctly refuses to overwrite that state. This revision
adds one narrowly-scoped SECURITY DEFINER repair capability: only a worker that
owns a live leased search event can record the provider evidence, advance the
PostgreSQL search clock above the observed provider clock, supersede the stale
attempt, and enqueue a fresh authoritative search command.

The predecessor outbox routines already use ``superseded`` for obsolete work,
but the legacy outbox status CHECK did not admit that explicit non-success
terminal state. This revision closes that schema-contract gap without widening
any role privilege. Downgrade restores the predecessor CHECK exactly and refuses
to discard live/provider-backed P4B state.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "v07d8e9f0a36"
down_revision = "u07d8e9f0a35"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_WORKER = "worker_runtime"
_FUNCTION = (
    "app_secure.repair_branch_search_provider_drift("
    "uuid,uuid,bigint,text,text,text,text,text,bigint,text,text,text)"
)
_OUTBOX_STATUS_CONSTRAINT = "branch_outbox_events_status_check"
_PREDECESSOR_OUTBOX_STATUSES = (
    "pending",
    "processing",
    "delivered",
    "dead_lettered",
    "quarantined",
    "compatibility_queue",
)


def _require_identity_contract(bind) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT session_user::text AS session_name,
                   current_user::text AS current_name,
                   rolsuper, rolinherit, rolcreatedb, rolcreaterole,
                   rolreplication, rolbypassrls
            FROM pg_catalog.pg_roles
            WHERE rolname = current_user
            """
        )
    ).mappings().one()
    if row["session_name"] != _MIGRATION_OWNER or row["current_name"] != _MIGRATION_OWNER:
        raise RuntimeError("v07 P4B drift repair migration requires migration_owner")
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
        raise RuntimeError("v07 migration_owner violates reduced role contract")

    for role_name in (_SECURITY_OWNER, _WORKER, "app_runtime", "auth_runtime", "lifecycle_maintenance_runtime"):
        role = bind.execute(
            sa.text(
                """
                SELECT rolsuper, rolinherit, rolcreatedb, rolcreaterole,
                       rolreplication, rolbypassrls
                FROM pg_catalog.pg_roles WHERE rolname = :role_name
                """
            ),
            {"role_name": role_name},
        ).mappings().one_or_none()
        if role is None:
            raise RuntimeError(f"v07 requires externally provisioned role {role_name}")
        if any(bool(role[key]) for key in role):
            raise RuntimeError(f"v07 reduced role contract drift: {role_name}")

    for runtime in ("app_runtime", "auth_runtime", _WORKER, "lifecycle_maintenance_runtime"):
        if bind.execute(
            sa.text("SELECT pg_catalog.pg_has_role(:member,:target,'SET')"),
            {"member": runtime, "target": _SECURITY_OWNER},
        ).scalar_one():
            raise RuntimeError(f"v07 runtime may SET ROLE app_security_owner: {runtime}")


def _require_predecessor(bind) -> None:
    if bind.execute(
        sa.text("SELECT pg_catalog.to_regclass('public.branch_search_effect_attempts') IS NULL")
    ).scalar_one():
        raise RuntimeError("v07 requires P4B search evidence predecessor")
    if bind.execute(
        sa.text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_proc AS p
                JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace
                WHERE n.nspname='app_secure'
                  AND p.proname='repair_branch_search_provider_drift'
            )
            """
        )
    ).scalar_one():
        raise RuntimeError("v07 drift repair function collision")

    constraint_def = bind.execute(
        sa.text(
            """
            SELECT pg_catalog.pg_get_constraintdef(c.oid, true)
            FROM pg_catalog.pg_constraint AS c
            WHERE c.conrelid = 'public.branch_outbox_events'::regclass
              AND c.conname = :constraint_name
              AND c.contype = 'c'
            """
        ),
        {"constraint_name": _OUTBOX_STATUS_CONSTRAINT},
    ).scalar_one_or_none()
    if constraint_def is None:
        raise RuntimeError("v07 predecessor outbox status CHECK is missing")
    missing = [
        status
        for status in _PREDECESSOR_OUTBOX_STATUSES
        if f"'{status}'" not in constraint_def
    ]
    if missing or "'superseded'" in constraint_def:
        raise RuntimeError(
            "v07 predecessor outbox status CHECK drift: "
            f"missing={missing!r}, definition={constraint_def!r}"
        )


def _install_superseded_status_contract() -> None:
    statuses = ", ".join(
        f"'{status}'" for status in (*_PREDECESSOR_OUTBOX_STATUSES, "superseded")
    )
    op.execute(
        f"""
        ALTER TABLE public.branch_outbox_events
            DROP CONSTRAINT {_OUTBOX_STATUS_CONSTRAINT},
            ADD CONSTRAINT {_OUTBOX_STATUS_CONSTRAINT}
                CHECK (status IN ({statuses}))
        """
    )


def _restore_predecessor_status_contract() -> None:
    statuses = ", ".join(f"'{status}'" for status in _PREDECESSOR_OUTBOX_STATUSES)
    op.execute(
        f"""
        ALTER TABLE public.branch_outbox_events
            DROP CONSTRAINT {_OUTBOX_STATUS_CONSTRAINT},
            ADD CONSTRAINT {_OUTBOX_STATUS_CONSTRAINT}
                CHECK (status IN ({statuses}))
        """
    )


def upgrade() -> None:
    bind = op.get_bind()
    _require_identity_contract(bind)
    _require_predecessor(bind)

    # The table remains owned by the migration authority. This only admits the
    # explicit obsolete-work terminal state already used by bounded P4B routines;
    # it grants no new DML capability to any runtime identity.
    _install_superseded_status_contract()

    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute(
        """
        CREATE FUNCTION app_secure.repair_branch_search_provider_drift(
            p_outbox_id uuid,
            p_worker_id uuid,
            p_desired_version bigint,
            p_operation text,
            p_provider_code text,
            p_provider_index text,
            p_provider_document_id text,
            p_request_sha256 text,
            p_provider_version bigint,
            p_provider_evidence_sha256 text,
            p_document_sha256 text,
            p_error_code text
        )
        RETURNS bigint
        LANGUAGE plpgsql
        VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        SET row_security = on
        AS $function$
        DECLARE
            v_tenant uuid;
            v_branch uuid;
            v_attempt integer;
            v_correlation uuid;
            v_current_version bigint;
            v_current_operation text;
            v_next_version bigint;
        BEGIN
            IF p_outbox_id IS NULL OR p_worker_id IS NULL
               OR p_desired_version IS NULL OR p_desired_version < 1
               OR p_operation NOT IN ('index','delete')
               OR p_provider_code IS NULL OR btrim(p_provider_code) = ''
               OR p_provider_index IS NULL OR btrim(p_provider_index) = ''
               OR p_provider_document_id IS NULL OR btrim(p_provider_document_id) = ''
               OR p_request_sha256 !~ '^[0-9a-f]{64}$'
               OR p_provider_version IS NULL OR p_provider_version < p_desired_version
               OR p_provider_version >= 9223372036854775807
               OR p_provider_evidence_sha256 !~ '^[0-9a-f]{64}$'
               OR (p_document_sha256 IS NOT NULL AND p_document_sha256 !~ '^[0-9a-f]{64}$')
               OR (p_operation = 'index' AND p_document_sha256 IS NULL)
               OR (p_operation = 'delete' AND p_document_sha256 IS NOT NULL)
               OR p_error_code NOT IN (
                    'provider_document_mismatch',
                    'provider_version_ahead',
                    'delete_not_proven'
               )
            THEN
                RAISE EXCEPTION 'invalid search provider drift repair arguments'
                    USING ERRCODE = '22023';
            END IF;

            SELECT o.tenant_id, o.branch_id, o.attempt_count, o.correlation_id
            INTO v_tenant, v_branch, v_attempt, v_correlation
            FROM public.branch_outbox_events AS o
            WHERE o.outbox_id = p_outbox_id
              AND o.event_type IN ('branch.search_index','branch.search_deindex')
              AND o.status = 'processing'
              AND o.leased_by = p_worker_id
              AND o.leased_until > pg_catalog.clock_timestamp();
            IF NOT FOUND THEN
                RAISE EXCEPTION 'search drift repair requires a live owned search lease'
                    USING ERRCODE = '42501';
            END IF;
            IF p_provider_document_id IS DISTINCT FROM v_branch::text THEN
                RAISE EXCEPTION 'search drift repair provider document does not match leased branch'
                    USING ERRCODE = '22023';
            END IF;

            SELECT s.search_visibility_version,
                   CASE
                       WHEN s.is_operational AND s.is_public AND s.deleted_at IS NULL
                       THEN 'index'::text ELSE 'delete'::text
                   END
            INTO v_current_version, v_current_operation
            FROM public.org_branch_state AS s
            WHERE s.branch_id = v_branch AND s.org_id = v_tenant
            FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'search drift repair branch state missing'
                    USING ERRCODE = 'P0002';
            END IF;

            INSERT INTO public.branch_search_effect_attempts (
                attempt_id, outbox_id, tenant_id, branch_id, desired_version,
                operation, attempt_number, outcome, provider_code, provider_index,
                provider_document_id, request_sha256, provider_version,
                provider_evidence_sha256, document_sha256, error_code, created_at
            ) VALUES (
                pg_catalog.gen_random_uuid(), p_outbox_id, v_tenant, v_branch,
                p_desired_version, p_operation, v_attempt, 'permanent_rejection',
                p_provider_code, p_provider_index, p_provider_document_id,
                p_request_sha256, p_provider_version, p_provider_evidence_sha256,
                p_document_sha256, left(p_error_code,160), pg_catalog.clock_timestamp()
            ) ON CONFLICT (outbox_id, attempt_number) DO NOTHING;

            IF v_current_version IS DISTINCT FROM p_desired_version
               OR v_current_operation IS DISTINCT FROM p_operation
            THEN
                UPDATE public.branch_outbox_events
                SET status='superseded', leased_by=NULL, leased_until=NULL,
                    last_error='search_projection_superseded_before_drift_repair'
                WHERE outbox_id=p_outbox_id
                  AND status='processing'
                  AND leased_by=p_worker_id;
                RETURN NULL;
            END IF;

            v_next_version := GREATEST(v_current_version, p_provider_version) + 1;

            UPDATE public.org_branch_state
            SET search_visibility_version = v_next_version,
                search_last_synced_at = NULL,
                search_sync_failed_at = pg_catalog.clock_timestamp()
            WHERE branch_id = v_branch AND org_id = v_tenant;

            UPDATE public.branch_outbox_events
            SET status='superseded', leased_by=NULL, leased_until=NULL,
                last_error='search_provider_drift_repair_requeued'
            WHERE outbox_id=p_outbox_id
              AND status='processing'
              AND leased_by=p_worker_id
              AND leased_until > pg_catalog.clock_timestamp();
            IF NOT FOUND THEN
                RAISE EXCEPTION 'search drift repair lost its lease fence'
                    USING ERRCODE = '40001';
            END IF;

            INSERT INTO public.branch_outbox_events (
                outbox_id, tenant_id, branch_id, event_type, payload,
                created_at, process_after, status, attempt_count, max_attempts,
                correlation_id, leased_by, leased_until
            ) VALUES (
                pg_catalog.gen_random_uuid(), v_tenant, v_branch,
                CASE WHEN v_current_operation='index'
                     THEN 'branch.search_index' ELSE 'branch.search_deindex' END,
                pg_catalog.jsonb_build_object(
                    'source','search_provider_drift_repair',
                    'desired_version',v_next_version,
                    'observed_provider_version',p_provider_version,
                    'provider_error_code',p_error_code
                ),
                pg_catalog.clock_timestamp(), pg_catalog.clock_timestamp(),
                'pending', 0, 5, v_correlation, NULL, NULL
            );

            RETURN v_next_version;
        END;
        $function$;
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_FUNCTION} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_FUNCTION} TO worker_runtime")
    op.execute("RESET ROLE")

    # Post-install proof: only the ordinary worker gets this exact capability.
    oid = bind.execute(
        sa.text(
            """
            SELECT p.oid::bigint
            FROM pg_catalog.pg_proc AS p
            JOIN pg_catalog.pg_namespace AS n ON n.oid=p.pronamespace
            WHERE n.nspname='app_secure'
              AND p.proname='repair_branch_search_provider_drift'
              AND pg_catalog.oidvectortypes(p.proargtypes)=
                  'uuid, uuid, bigint, text, text, text, text, text, bigint, text, text, text'
            """
        )
    ).scalar_one()
    for role_name in ("app_runtime", "auth_runtime", "lifecycle_maintenance_runtime"):
        if bind.execute(
            sa.text("SELECT pg_catalog.has_function_privilege(:role,CAST(:oid AS oid),'EXECUTE')"),
            {"role": role_name, "oid": oid},
        ).scalar_one():
            raise RuntimeError(f"v07 leaked drift repair capability to {role_name}")
    if not bind.execute(
        sa.text("SELECT pg_catalog.has_function_privilege('worker_runtime',CAST(:oid AS oid),'EXECUTE')"),
        {"oid": oid},
    ).scalar_one():
        raise RuntimeError("v07 worker lacks drift repair capability")

    constraint_def = bind.execute(
        sa.text(
            """
            SELECT pg_catalog.pg_get_constraintdef(c.oid, true)
            FROM pg_catalog.pg_constraint AS c
            WHERE c.conrelid = 'public.branch_outbox_events'::regclass
              AND c.conname = :constraint_name
            """
        ),
        {"constraint_name": _OUTBOX_STATUS_CONSTRAINT},
    ).scalar_one()
    if "'superseded'" not in constraint_def:
        raise RuntimeError("v07 failed to install superseded outbox status contract")


def downgrade() -> None:
    bind = op.get_bind()
    _require_identity_contract(bind)

    live = bind.execute(
        sa.text(
            """
            SELECT count(*) FROM public.branch_outbox_events
            WHERE event_type IN ('branch.search_index','branch.search_deindex')
              AND status IN ('pending','processing')
            """
        )
    ).scalar_one()
    evidence = bind.execute(
        sa.text("SELECT count(*) FROM public.branch_search_effect_attempts")
    ).scalar_one()
    acknowledged = bind.execute(
        sa.text(
            "SELECT count(*) FROM public.org_branch_state "
            "WHERE search_provider_ack_version IS NOT NULL"
        )
    ).scalar_one()
    superseded = bind.execute(
        sa.text(
            "SELECT count(*) FROM public.branch_outbox_events "
            "WHERE status = 'superseded'"
        )
    ).scalar_one()
    if live or evidence or acknowledged or superseded:
        raise RuntimeError(
            "v07 downgrade refuses loss of live/provider-backed search state: "
            f"live={live}, evidence={evidence}, acknowledged={acknowledged}, "
            f"superseded={superseded}"
        )

    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {_FUNCTION} FROM worker_runtime")
    op.execute(f"DROP FUNCTION {_FUNCTION}")
    op.execute("RESET ROLE")
    _restore_predecessor_status_contract()
