"""P5-E: fence every search and notification worker capability.

Revision ID: zi07d8e9f0a43
Revises: zh07d8e9f0a42
Create Date: 2026-09-13

P5-W added a monotonic claim generation to both durable outboxes.  The search
and notification SECURITY DEFINER functions predated that generation and only
checked worker UUID plus lease time.  Add fence-bearing overloads which lock
and validate the exact outbox generation before delegating to the certified P4
function bodies.  The legacy overloads remain for reversible downgrade but are
no longer executable by worker_runtime while this migration is active.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "zi07d8e9f0a43"
down_revision = "zh07d8e9f0a42"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_WORKER = "worker_runtime"
_HELPER = "app_secure.require_p5_provider_fence(uuid,uuid,bigint,text[])"

_SIGNATURES = (
    (
        "app_secure.claim_branch_search_projection(uuid,uuid)",
        "app_secure.claim_branch_search_projection(uuid,uuid,bigint)",
    ),
    (
        "app_secure.acknowledge_branch_search_effect(uuid,uuid,bigint,text,text,text,text,text,bigint,text,text)",
        "app_secure.acknowledge_branch_search_effect(uuid,uuid,bigint,bigint,text,text,text,text,text,bigint,text,text)",
    ),
    (
        "app_secure.record_branch_search_failure(uuid,uuid,bigint,text,text,text,text,text)",
        "app_secure.record_branch_search_failure(uuid,uuid,bigint,bigint,text,text,text,text,text)",
    ),
    (
        "app_secure.repair_branch_search_provider_drift(uuid,uuid,bigint,text,text,text,text,text,bigint,text,text,text)",
        "app_secure.repair_branch_search_provider_drift(uuid,uuid,bigint,bigint,text,text,text,text,text,bigint,text,text,text)",
    ),
    (
        "app_secure.materialize_branch_member_notifications(uuid,uuid)",
        "app_secure.materialize_branch_member_notifications(uuid,uuid,bigint)",
    ),
    (
        "app_secure.claim_notification_delivery_v2(uuid,uuid)",
        "app_secure.claim_notification_delivery_v2(uuid,uuid,bigint)",
    ),
    (
        "app_secure.acknowledge_notification_provider_acceptance(uuid,uuid,text,text,text)",
        "app_secure.acknowledge_notification_provider_acceptance(uuid,uuid,bigint,text,text,text)",
    ),
    (
        "app_secure.record_notification_delivery_failure(uuid,uuid,text,text,text)",
        "app_secure.record_notification_delivery_failure(uuid,uuid,bigint,text,text,text)",
    ),
    (
        "app_secure.claim_notification_reconciliation(uuid,uuid)",
        "app_secure.claim_notification_reconciliation(uuid,uuid,bigint)",
    ),
    (
        "app_secure.complete_notification_reconciliation(uuid,uuid,text,text)",
        "app_secure.complete_notification_reconciliation(uuid,uuid,bigint,text,text)",
    ),
    (
        "app_secure.record_notification_reconciliation_failure(uuid,uuid,text,boolean)",
        "app_secure.record_notification_reconciliation_failure(uuid,uuid,bigint,text,boolean)",
    ),
)


def _require_identity(bind) -> None:
    identity = bind.execute(
        sa.text("SELECT session_user::text,current_user::text")
    ).one()
    if tuple(identity) != (_MIGRATION_OWNER, _MIGRATION_OWNER):
        raise RuntimeError("zi07 P5-E migration requires migration_owner")


def _to_regprocedure(bind, signature: str) -> int | None:
    return bind.execute(
        sa.text("SELECT pg_catalog.to_regprocedure(:signature)::oid"),
        {"signature": signature},
    ).scalar_one()


def _has_execute(bind, role_name: str, signature: str) -> bool:
    return bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.has_function_privilege("
                ":role_name,:signature,'EXECUTE')"
            ),
            {"role_name": role_name, "signature": signature},
        ).scalar_one()
    )


def _require_predecessor(bind) -> None:
    if bind.execute(
        sa.text(
            """
            SELECT NOT (
                pg_catalog.has_column_privilege(
                    :worker,'public.branch_outbox_events','lease_fence','UPDATE'
                )
                AND EXISTS (
                    SELECT 1 FROM pg_catalog.pg_attribute
                    WHERE attrelid='public.branch_outbox_events'::regclass
                      AND attname='lease_fence' AND attnum>0 AND NOT attisdropped
                )
            )
            """
        ),
        {"worker": _WORKER},
    ).scalar_one():
        raise RuntimeError("zi07 requires the P5-W lifecycle claim fence")

    if _to_regprocedure(bind, _HELPER) is not None:
        raise RuntimeError("zi07 provider-fence helper collision")
    for old_signature, new_signature in _SIGNATURES:
        if _to_regprocedure(bind, old_signature) is None:
            raise RuntimeError(f"zi07 missing predecessor function: {old_signature}")
        if not _has_execute(bind, _WORKER, old_signature):
            raise RuntimeError(
                f"zi07 predecessor worker execute is absent: {old_signature}"
            )
        if _has_execute(bind, "PUBLIC", old_signature):
            raise RuntimeError(
                f"zi07 predecessor function leaked to PUBLIC: {old_signature}"
            )
        if _to_regprocedure(bind, new_signature) is not None:
            raise RuntimeError(f"zi07 fenced overload collision: {new_signature}")


def _create_helper() -> None:
    op.execute(
        """
        CREATE FUNCTION app_secure.require_p5_provider_fence(
            p_outbox_id uuid,
            p_worker_id uuid,
            p_lease_fence bigint,
            p_event_types text[]
        )
        RETURNS void
        LANGUAGE plpgsql
        VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        SET row_security = on
        AS $function$
        BEGIN
            IF p_outbox_id IS NULL OR p_worker_id IS NULL
               OR p_lease_fence IS NULL OR p_lease_fence < 1
               OR p_event_types IS NULL
               OR pg_catalog.cardinality(p_event_types) < 1
            THEN
                RAISE EXCEPTION 'provider capability requires complete fence identity'
                    USING ERRCODE='22023';
            END IF;

            PERFORM 1
            FROM public.branch_outbox_events AS outbox_data
            WHERE outbox_data.outbox_id=p_outbox_id
              AND outbox_data.event_type=ANY(p_event_types)
              AND outbox_data.status='processing'
              AND outbox_data.leased_by=p_worker_id
              AND outbox_data.lease_fence=p_lease_fence
              AND outbox_data.leased_until>pg_catalog.clock_timestamp()
            FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'provider capability requires current live claim fence'
                    USING ERRCODE='42501';
            END IF;
        END;
        $function$;
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_HELPER} FROM PUBLIC")
    op.execute(f"REVOKE ALL ON FUNCTION {_HELPER} FROM {_WORKER}")


def _create_wrappers() -> None:
    wrappers = (
        """
        CREATE FUNCTION app_secure.claim_branch_search_projection(
            p_outbox_id uuid,p_worker_id uuid,p_lease_fence bigint
        ) RETURNS TABLE(
            tenant_id uuid,branch_id uuid,operation text,desired_version bigint,
            document jsonb,previous_ack_version bigint
        ) LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path=pg_catalog,public SET row_security=on
        AS $function$
        BEGIN
            PERFORM app_secure.require_p5_provider_fence(
                p_outbox_id,p_worker_id,p_lease_fence,
                ARRAY['branch.search_index','branch.search_deindex']::text[]
            );
            RETURN QUERY SELECT * FROM app_secure.claim_branch_search_projection(
                p_outbox_id,p_worker_id
            );
        END;
        $function$;
        """,
        """
        CREATE FUNCTION app_secure.acknowledge_branch_search_effect(
            p_outbox_id uuid,p_worker_id uuid,p_lease_fence bigint,
            p_desired_version bigint,p_operation text,p_provider_code text,
            p_provider_index text,p_provider_document_id text,p_request_sha256 text,
            p_provider_version bigint,p_provider_evidence_sha256 text,
            p_document_sha256 text
        ) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path=pg_catalog,public SET row_security=on
        AS $function$
        BEGIN
            PERFORM app_secure.require_p5_provider_fence(
                p_outbox_id,p_worker_id,p_lease_fence,
                ARRAY['branch.search_index','branch.search_deindex']::text[]
            );
            RETURN app_secure.acknowledge_branch_search_effect(
                p_outbox_id,p_worker_id,p_desired_version,p_operation,
                p_provider_code,p_provider_index,p_provider_document_id,
                p_request_sha256,p_provider_version,p_provider_evidence_sha256,
                p_document_sha256
            );
        END;
        $function$;
        """,
        """
        CREATE FUNCTION app_secure.record_branch_search_failure(
            p_outbox_id uuid,p_worker_id uuid,p_lease_fence bigint,
            p_desired_version bigint,p_operation text,p_outcome text,
            p_provider_code text,p_request_sha256 text,p_error_code text
        ) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path=pg_catalog,public SET row_security=on
        AS $function$
        BEGIN
            PERFORM app_secure.require_p5_provider_fence(
                p_outbox_id,p_worker_id,p_lease_fence,
                ARRAY['branch.search_index','branch.search_deindex']::text[]
            );
            RETURN app_secure.record_branch_search_failure(
                p_outbox_id,p_worker_id,p_desired_version,p_operation,p_outcome,
                p_provider_code,p_request_sha256,p_error_code
            );
        END;
        $function$;
        """,
        """
        CREATE FUNCTION app_secure.repair_branch_search_provider_drift(
            p_outbox_id uuid,p_worker_id uuid,p_lease_fence bigint,
            p_desired_version bigint,p_operation text,p_provider_code text,
            p_provider_index text,p_provider_document_id text,p_request_sha256 text,
            p_provider_version bigint,p_provider_evidence_sha256 text,
            p_document_sha256 text,p_error_code text
        ) RETURNS bigint LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path=pg_catalog,public SET row_security=on
        AS $function$
        BEGIN
            PERFORM app_secure.require_p5_provider_fence(
                p_outbox_id,p_worker_id,p_lease_fence,
                ARRAY['branch.search_index','branch.search_deindex']::text[]
            );
            RETURN app_secure.repair_branch_search_provider_drift(
                p_outbox_id,p_worker_id,p_desired_version,p_operation,
                p_provider_code,p_provider_index,p_provider_document_id,
                p_request_sha256,p_provider_version,p_provider_evidence_sha256,
                p_document_sha256,p_error_code
            );
        END;
        $function$;
        """,
        """
        CREATE FUNCTION app_secure.materialize_branch_member_notifications(
            p_outbox_id uuid,p_worker_id uuid,p_lease_fence bigint
        ) RETURNS integer LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path=pg_catalog,public SET row_security=on
        AS $function$
        BEGIN
            PERFORM app_secure.require_p5_provider_fence(
                p_outbox_id,p_worker_id,p_lease_fence,
                ARRAY['branch.member_notification']::text[]
            );
            RETURN app_secure.materialize_branch_member_notifications(
                p_outbox_id,p_worker_id
            );
        END;
        $function$;
        """,
        """
        CREATE FUNCTION app_secure.claim_notification_delivery_v2(
            p_outbox_id uuid,p_worker_id uuid,p_lease_fence bigint
        ) RETURNS TABLE(
            eligible boolean,command_id uuid,tenant_id uuid,branch_id uuid,
            member_id uuid,channel text,destination text,member_name text,
            template_key text,template_data jsonb,idempotency_key text,
            attempt_number integer,provider_code text
        ) LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path=pg_catalog,public SET row_security=on
        AS $function$
        BEGIN
            PERFORM app_secure.require_p5_provider_fence(
                p_outbox_id,p_worker_id,p_lease_fence,
                ARRAY['notification.delivery']::text[]
            );
            RETURN QUERY SELECT * FROM app_secure.claim_notification_delivery_v2(
                p_outbox_id,p_worker_id
            );
        END;
        $function$;
        """,
        """
        CREATE FUNCTION app_secure.acknowledge_notification_provider_acceptance(
            p_outbox_id uuid,p_worker_id uuid,p_lease_fence bigint,
            p_provider_reference_id text,p_request_sha256 text,p_evidence_sha256 text
        ) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path=pg_catalog,public SET row_security=on
        AS $function$
        BEGIN
            PERFORM app_secure.require_p5_provider_fence(
                p_outbox_id,p_worker_id,p_lease_fence,
                ARRAY['notification.delivery']::text[]
            );
            RETURN app_secure.acknowledge_notification_provider_acceptance(
                p_outbox_id,p_worker_id,p_provider_reference_id,
                p_request_sha256,p_evidence_sha256
            );
        END;
        $function$;
        """,
        """
        CREATE FUNCTION app_secure.record_notification_delivery_failure(
            p_outbox_id uuid,p_worker_id uuid,p_lease_fence bigint,
            p_outcome text,p_request_sha256 text,p_error_code text
        ) RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path=pg_catalog,public SET row_security=on
        AS $function$
        BEGIN
            PERFORM app_secure.require_p5_provider_fence(
                p_outbox_id,p_worker_id,p_lease_fence,
                ARRAY['notification.delivery']::text[]
            );
            RETURN app_secure.record_notification_delivery_failure(
                p_outbox_id,p_worker_id,p_outcome,p_request_sha256,p_error_code
            );
        END;
        $function$;
        """,
        """
        CREATE FUNCTION app_secure.claim_notification_reconciliation(
            p_outbox_id uuid,p_worker_id uuid,p_lease_fence bigint
        ) RETURNS TABLE(
            command_id uuid,tenant_id uuid,provider_reference_id text
        ) LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path=pg_catalog,public SET row_security=on
        AS $function$
        BEGIN
            PERFORM app_secure.require_p5_provider_fence(
                p_outbox_id,p_worker_id,p_lease_fence,
                ARRAY['notification.reconcile']::text[]
            );
            RETURN QUERY SELECT * FROM app_secure.claim_notification_reconciliation(
                p_outbox_id,p_worker_id
            );
        END;
        $function$;
        """,
        """
        CREATE FUNCTION app_secure.complete_notification_reconciliation(
            p_outbox_id uuid,p_worker_id uuid,p_lease_fence bigint,
            p_last_event text,p_evidence_sha256 text
        ) RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path=pg_catalog,public SET row_security=on
        AS $function$
        BEGIN
            PERFORM app_secure.require_p5_provider_fence(
                p_outbox_id,p_worker_id,p_lease_fence,
                ARRAY['notification.reconcile']::text[]
            );
            RETURN app_secure.complete_notification_reconciliation(
                p_outbox_id,p_worker_id,p_last_event,p_evidence_sha256
            );
        END;
        $function$;
        """,
        """
        CREATE FUNCTION app_secure.record_notification_reconciliation_failure(
            p_outbox_id uuid,p_worker_id uuid,p_lease_fence bigint,
            p_error_code text,p_permanent boolean
        ) RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path=pg_catalog,public SET row_security=on
        AS $function$
        BEGIN
            PERFORM app_secure.require_p5_provider_fence(
                p_outbox_id,p_worker_id,p_lease_fence,
                ARRAY['notification.reconcile']::text[]
            );
            RETURN app_secure.record_notification_reconciliation_failure(
                p_outbox_id,p_worker_id,p_error_code,p_permanent
            );
        END;
        $function$;
        """,
    )
    for statement in wrappers:
        op.execute(statement)


def _verify_upgrade(bind) -> None:
    if _to_regprocedure(bind, _HELPER) is None:
        raise RuntimeError("zi07 provider-fence helper was not installed")
    if _has_execute(bind, _WORKER, _HELPER) or _has_execute(bind, "PUBLIC", _HELPER):
        raise RuntimeError("zi07 private provider-fence helper leaked")
    for old_signature, new_signature in _SIGNATURES:
        if _has_execute(bind, _WORKER, old_signature):
            raise RuntimeError(
                f"zi07 worker retained unfenced provider capability: {old_signature}"
            )
        if _to_regprocedure(bind, new_signature) is None:
            raise RuntimeError(f"zi07 fenced provider capability missing: {new_signature}")
        if not _has_execute(bind, _WORKER, new_signature):
            raise RuntimeError(
                f"zi07 worker lacks fenced provider capability: {new_signature}"
            )
        if _has_execute(bind, "PUBLIC", new_signature):
            raise RuntimeError(
                f"zi07 fenced provider capability leaked to PUBLIC: {new_signature}"
            )


def upgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    _require_predecessor(bind)
    op.execute(f"SET LOCAL ROLE {_SECURITY_OWNER}")
    try:
        for old_signature, _new_signature in _SIGNATURES:
            op.execute(f"REVOKE EXECUTE ON FUNCTION {old_signature} FROM {_WORKER}")
        _create_helper()
        _create_wrappers()
        for _old_signature, new_signature in _SIGNATURES:
            op.execute(f"REVOKE ALL ON FUNCTION {new_signature} FROM PUBLIC")
            op.execute(f"GRANT EXECUTE ON FUNCTION {new_signature} TO {_WORKER}")
    finally:
        op.execute("RESET ROLE")
    _verify_upgrade(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    _verify_upgrade(bind)
    op.execute(f"SET LOCAL ROLE {_SECURITY_OWNER}")
    try:
        for old_signature, new_signature in reversed(_SIGNATURES):
            op.execute(f"REVOKE EXECUTE ON FUNCTION {new_signature} FROM {_WORKER}")
            op.execute(f"DROP FUNCTION {new_signature}")
            op.execute(f"GRANT EXECUTE ON FUNCTION {old_signature} TO {_WORKER}")
        op.execute(f"DROP FUNCTION {_HELPER}")
    finally:
        op.execute("RESET ROLE")

    for old_signature, new_signature in _SIGNATURES:
        if _to_regprocedure(bind, new_signature) is not None:
            raise RuntimeError(f"zi07 downgrade left fenced overload: {new_signature}")
        if not _has_execute(bind, _WORKER, old_signature):
            raise RuntimeError(
                f"zi07 downgrade failed to restore predecessor ACL: {old_signature}"
            )
    if _to_regprocedure(bind, _HELPER) is not None:
        raise RuntimeError("zi07 downgrade left provider-fence helper")
