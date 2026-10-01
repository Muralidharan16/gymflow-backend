# PAY-24-B RI1B1 expand PAY8 provider-effect authority.
# Zero-downtime expand only; legacy app authority is temporarily retained.

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "zz77d8e9f0a67"
down_revision = "zz67d8e9f0a66"
branch_labels = None
depends_on = None

_CLAIM_SIGNATURE = "app_secure.claim_finance_provider_operation(uuid,uuid)"
_FINISH_SIGNATURE = "app_secure.finish_finance_provider_operation(uuid,uuid,bigint,text,text,text,text)"
_FUNCTION_IDENTITIES = {
    _CLAIM_SIGNATURE: ("claim_finance_provider_operation", "uuid, uuid"),
    _FINISH_SIGNATURE: (
        "finish_finance_provider_operation",
        "uuid, uuid, bigint, text, text, text, text",
    ),
}
_HISTORICAL_CLAIM_SHA256 = "d367a6132bf01356ae388da31488d05b67274d0d9f17466fb4929c3084fff1bc"
_HISTORICAL_FINISH_SHA256 = "a5a9c184a179fc5000e6e0a9ca2a4e1abadca889d2cfe51df1d14fde26f6360c"
_EXPANDED_CLAIM_SHA256 = "07ccd0a3c9892243c6d6e3cd745b84738fb2103f17219efd8e948c88ea2c8af0"
_EXPANDED_FINISH_SHA256 = "63bb0041b96e8ae84a14ea0d21492caeee57263bdf0345c51a2e687bacbbdcf1"
_CLAIM_EXPANDED_SQL = "\n            CREATE OR REPLACE FUNCTION app_secure.claim_finance_provider_operation(\n                p_operation_id uuid,\n                p_lease_owner uuid\n            )\n            RETURNS TABLE(\n                operation_id uuid,\n                status text,\n                lease_fence bigint,\n                provider_object_id text,\n                attempt_count integer,\n                claimed boolean\n            )\n            LANGUAGE plpgsql\n            SECURITY DEFINER\n            SET search_path=pg_catalog,public,finance\n            SET row_security=on\n            AS $function$\n            DECLARE\n                v_org uuid;\n                v_operation finance.provider_operations%ROWTYPE;\n            BEGIN\n                IF NOT (\n                    pg_catalog.pg_has_role(\n                        session_user,'app_runtime','MEMBER'\n                    )\n                    OR pg_catalog.pg_has_role(\n                        session_user,'finance_payment_runtime','MEMBER'\n                    )\n                ) THEN\n                    RAISE EXCEPTION\n                        'PAY-8 provider claim requires app_runtime or finance_payment_runtime'\n                        USING ERRCODE='42501';\n                END IF;\n                BEGIN\n                    v_org:=NULLIF(\n                        pg_catalog.current_setting(\n                            'app.current_org_id',true\n                        ),''\n                    )::uuid;\n                EXCEPTION WHEN invalid_text_representation THEN\n                    RAISE EXCEPTION\n                        'PAY-8 provider claim tenant invalid'\n                        USING ERRCODE='42501';\n                END;\n                IF v_org IS NULL\n                   OR p_operation_id IS NULL\n                   OR p_lease_owner IS NULL\n                THEN\n                    RAISE EXCEPTION\n                        'PAY-8 provider claim identity invalid'\n                        USING ERRCODE='22023';\n                END IF;\n\n                SELECT o.* INTO v_operation\n                FROM finance.provider_operations o\n                WHERE o.id=p_operation_id\n                  AND o.organization_id=v_org\n                FOR UPDATE;\n                IF NOT FOUND THEN\n                    RAISE EXCEPTION\n                        'PAY-8 provider operation unavailable'\n                        USING ERRCODE='P0002';\n                END IF;\n\n                IF v_operation.status='in_flight'\n                   AND v_operation.lease_until<=\n                       pg_catalog.clock_timestamp()\n                THEN\n                    UPDATE finance.provider_operations\n                    SET status='unknown',\n                        lease_owner=NULL,\n                        lease_until=NULL,\n                        last_error_code='lease_expired_unknown',\n                        updated_at=pg_catalog.clock_timestamp()\n                    WHERE id=v_operation.id\n                    RETURNING * INTO v_operation;\n                    RETURN QUERY SELECT\n                        v_operation.id,v_operation.status::text,\n                        v_operation.lease_fence,\n                        v_operation.provider_object_id::text,\n                        v_operation.attempt_count,false;\n                    RETURN;\n                END IF;\n\n                IF v_operation.status='failed_retryable'\n                   AND v_operation.attempt_count>=v_operation.max_attempts\n                THEN\n                    UPDATE finance.provider_operations\n                    SET status='failed_final',\n                        last_error_code='retry_exhausted',\n                        completed_at=pg_catalog.clock_timestamp(),\n                        updated_at=pg_catalog.clock_timestamp()\n                    WHERE id=v_operation.id\n                    RETURNING * INTO v_operation;\n                    RETURN QUERY SELECT\n                        v_operation.id,v_operation.status::text,\n                        v_operation.lease_fence,\n                        v_operation.provider_object_id::text,\n                        v_operation.attempt_count,false;\n                    RETURN;\n                END IF;\n\n                IF v_operation.status NOT IN (\n                    'reserved','failed_retryable'\n                ) THEN\n                    RETURN QUERY SELECT\n                        v_operation.id,v_operation.status::text,\n                        v_operation.lease_fence,\n                        v_operation.provider_object_id::text,\n                        v_operation.attempt_count,false;\n                    RETURN;\n                END IF;\n\n                UPDATE finance.provider_operations AS po\n                SET status='in_flight',\n                    attempt_count=po.attempt_count+1,\n                    lease_owner=p_lease_owner,\n                    lease_until=\n                        pg_catalog.clock_timestamp()+interval '30 seconds',\n                    lease_fence=po.lease_fence+1,\n                    last_error_code=NULL,\n                    last_started_at=pg_catalog.clock_timestamp(),\n                    updated_at=pg_catalog.clock_timestamp()\n                WHERE po.id=v_operation.id\n                RETURNING po.* INTO v_operation;\n\n                RETURN QUERY SELECT\n                    v_operation.id,v_operation.status::text,\n                    v_operation.lease_fence,\n                    v_operation.provider_object_id::text,\n                    v_operation.attempt_count,true;\n            END\n            $function$\n            "
_FINISH_EXPANDED_SQL = "\n            CREATE OR REPLACE FUNCTION app_secure.finish_finance_provider_operation(\n                p_operation_id uuid,\n                p_lease_owner uuid,\n                p_lease_fence bigint,\n                p_outcome text,\n                p_provider_object_id text,\n                p_error_code text,\n                p_evidence_sha256 text\n            )\n            RETURNS TABLE(\n                operation_id uuid,\n                status text,\n                provider_object_id text,\n                attempt_count integer\n            )\n            LANGUAGE plpgsql\n            SECURITY DEFINER\n            SET search_path=pg_catalog,public,finance\n            SET row_security=on\n            AS $function$\n            DECLARE\n                v_org uuid;\n                v_operation finance.provider_operations%ROWTYPE;\n                v_payment finance.payments%ROWTYPE;\n            BEGIN\n                IF NOT (\n                    pg_catalog.pg_has_role(\n                        session_user,'app_runtime','MEMBER'\n                    )\n                    OR pg_catalog.pg_has_role(\n                        session_user,'finance_payment_runtime','MEMBER'\n                    )\n                ) THEN\n                    RAISE EXCEPTION\n                        'PAY-8 provider completion requires app_runtime or finance_payment_runtime'\n                        USING ERRCODE='42501';\n                END IF;\n                BEGIN\n                    v_org:=NULLIF(\n                        pg_catalog.current_setting(\n                            'app.current_org_id',true\n                        ),''\n                    )::uuid;\n                EXCEPTION WHEN invalid_text_representation THEN\n                    RAISE EXCEPTION\n                        'PAY-8 provider completion tenant invalid'\n                        USING ERRCODE='42501';\n                END;\n                IF v_org IS NULL\n                   OR p_operation_id IS NULL\n                   OR p_lease_owner IS NULL\n                   OR p_lease_fence IS NULL\n                   OR p_outcome NOT IN (\n                       'succeeded','failed_retryable',\n                       'failed_final','unknown'\n                   )\n                   OR (\n                       p_error_code IS NOT NULL\n                       AND p_error_code !~\n                           '^[a-z][a-z0-9_]{0,79}$'\n                   )\n                   OR (\n                       p_evidence_sha256 IS NOT NULL\n                       AND p_evidence_sha256 !~ '^[0-9a-f]{64}$'\n                   )\n                THEN\n                    RAISE EXCEPTION\n                        'PAY-8 provider completion invalid'\n                        USING ERRCODE='22023';\n                END IF;\n\n                SELECT o.* INTO v_operation\n                FROM finance.provider_operations o\n                WHERE o.id=p_operation_id\n                  AND o.organization_id=v_org\n                FOR UPDATE;\n                IF NOT FOUND THEN\n                    RAISE EXCEPTION\n                        'PAY-8 provider operation unavailable'\n                        USING ERRCODE='P0002';\n                END IF;\n                IF v_operation.status<>'in_flight'\n                   OR v_operation.lease_owner\n                      IS DISTINCT FROM p_lease_owner\n                   OR v_operation.lease_fence\n                      IS DISTINCT FROM p_lease_fence\n                THEN\n                    RAISE EXCEPTION\n                        'PAY-8 provider operation fence conflict'\n                        USING ERRCODE='40001';\n                END IF;\n\n                IF p_outcome='succeeded' THEN\n                    IF p_provider_object_id IS NULL\n                       OR p_provider_object_id !~\n                          '^[A-Za-z0-9_:-]{1,200}$'\n                       OR p_evidence_sha256 IS NULL\n                    THEN\n                        RAISE EXCEPTION\n                            'PAY-8 provider success evidence invalid'\n                            USING ERRCODE='22023';\n                    END IF;\n                    SELECT p.* INTO v_payment\n                    FROM finance.payments p\n                    WHERE p.id=v_operation.payment_id\n                      AND p.organization_id=v_org\n                    FOR UPDATE;\n                    IF NOT FOUND\n                       OR v_payment.provider_code\n                          IS DISTINCT FROM v_operation.provider_code\n                    THEN\n                        RAISE EXCEPTION\n                            'PAY-8 provider payment unavailable'\n                            USING ERRCODE='23514';\n                    END IF;\n                    IF v_payment.provider_order_ref IS NOT NULL\n                       AND v_payment.provider_order_ref NOT LIKE 'intent_%'\n                       AND v_payment.provider_order_ref\n                          IS DISTINCT FROM p_provider_object_id\n                    THEN\n                        RAISE EXCEPTION\n                            'PAY-8 provider object replay conflict'\n                            USING ERRCODE='23505';\n                    END IF;\n                    BEGIN\n                        UPDATE finance.payments\n                        SET provider_order_ref=p_provider_object_id,\n                            updated_at=pg_catalog.clock_timestamp()\n                        WHERE id=v_payment.id;\n                    EXCEPTION WHEN unique_violation THEN\n                        RAISE EXCEPTION\n                            'PAY-8 provider object already bound'\n                            USING ERRCODE='23505';\n                    END;\n                ELSIF p_outcome IN (\n                    'failed_retryable','failed_final','unknown'\n                ) THEN\n                    IF p_error_code IS NULL THEN\n                        RAISE EXCEPTION\n                            'PAY-8 provider failure code required'\n                            USING ERRCODE='22023';\n                    END IF;\n                END IF;\n\n                UPDATE finance.provider_operations AS po\n                SET status=p_outcome,\n                    provider_object_id=CASE\n                        WHEN p_provider_object_id IS NOT NULL\n                        THEN p_provider_object_id\n                        ELSE po.provider_object_id\n                    END,\n                    provider_evidence_sha256=CASE\n                        WHEN p_evidence_sha256 IS NOT NULL\n                        THEN p_evidence_sha256\n                        ELSE po.provider_evidence_sha256\n                    END,\n                    lease_owner=NULL,\n                    lease_until=NULL,\n                    last_error_code=CASE\n                        WHEN p_outcome='succeeded' THEN NULL\n                        ELSE p_error_code\n                    END,\n                    completed_at=CASE\n                        WHEN p_outcome IN (\n                            'succeeded','failed_final'\n                        )\n                        THEN pg_catalog.clock_timestamp()\n                        ELSE NULL\n                    END,\n                    updated_at=pg_catalog.clock_timestamp()\n                WHERE po.id=v_operation.id\n                RETURNING po.* INTO v_operation;\n\n                RETURN QUERY SELECT\n                    v_operation.id,v_operation.status::text,\n                    v_operation.provider_object_id::text,\n                    v_operation.attempt_count;\n            END\n            $function$\n            "
_CLAIM_ORIGINAL_SQL = "\n            CREATE OR REPLACE FUNCTION app_secure.claim_finance_provider_operation(\n                p_operation_id uuid,\n                p_lease_owner uuid\n            )\n            RETURNS TABLE(\n                operation_id uuid,\n                status text,\n                lease_fence bigint,\n                provider_object_id text,\n                attempt_count integer,\n                claimed boolean\n            )\n            LANGUAGE plpgsql\n            SECURITY DEFINER\n            SET search_path=pg_catalog,public,finance\n            SET row_security=on\n            AS $function$\n            DECLARE\n                v_org uuid;\n                v_operation finance.provider_operations%ROWTYPE;\n            BEGIN\n                IF NOT pg_catalog.pg_has_role(\n                    session_user,'app_runtime','MEMBER'\n                ) THEN\n                    RAISE EXCEPTION\n                        'PAY-8 provider claim requires app_runtime'\n                        USING ERRCODE='42501';\n                END IF;\n                BEGIN\n                    v_org:=NULLIF(\n                        pg_catalog.current_setting(\n                            'app.current_org_id',true\n                        ),''\n                    )::uuid;\n                EXCEPTION WHEN invalid_text_representation THEN\n                    RAISE EXCEPTION\n                        'PAY-8 provider claim tenant invalid'\n                        USING ERRCODE='42501';\n                END;\n                IF v_org IS NULL\n                   OR p_operation_id IS NULL\n                   OR p_lease_owner IS NULL\n                THEN\n                    RAISE EXCEPTION\n                        'PAY-8 provider claim identity invalid'\n                        USING ERRCODE='22023';\n                END IF;\n\n                SELECT o.* INTO v_operation\n                FROM finance.provider_operations o\n                WHERE o.id=p_operation_id\n                  AND o.organization_id=v_org\n                FOR UPDATE;\n                IF NOT FOUND THEN\n                    RAISE EXCEPTION\n                        'PAY-8 provider operation unavailable'\n                        USING ERRCODE='P0002';\n                END IF;\n\n                IF v_operation.status='in_flight'\n                   AND v_operation.lease_until<=\n                       pg_catalog.clock_timestamp()\n                THEN\n                    UPDATE finance.provider_operations\n                    SET status='unknown',\n                        lease_owner=NULL,\n                        lease_until=NULL,\n                        last_error_code='lease_expired_unknown',\n                        updated_at=pg_catalog.clock_timestamp()\n                    WHERE id=v_operation.id\n                    RETURNING * INTO v_operation;\n                    RETURN QUERY SELECT\n                        v_operation.id,v_operation.status::text,\n                        v_operation.lease_fence,\n                        v_operation.provider_object_id::text,\n                        v_operation.attempt_count,false;\n                    RETURN;\n                END IF;\n\n                IF v_operation.status='failed_retryable'\n                   AND v_operation.attempt_count>=v_operation.max_attempts\n                THEN\n                    UPDATE finance.provider_operations\n                    SET status='failed_final',\n                        last_error_code='retry_exhausted',\n                        completed_at=pg_catalog.clock_timestamp(),\n                        updated_at=pg_catalog.clock_timestamp()\n                    WHERE id=v_operation.id\n                    RETURNING * INTO v_operation;\n                    RETURN QUERY SELECT\n                        v_operation.id,v_operation.status::text,\n                        v_operation.lease_fence,\n                        v_operation.provider_object_id::text,\n                        v_operation.attempt_count,false;\n                    RETURN;\n                END IF;\n\n                IF v_operation.status NOT IN (\n                    'reserved','failed_retryable'\n                ) THEN\n                    RETURN QUERY SELECT\n                        v_operation.id,v_operation.status::text,\n                        v_operation.lease_fence,\n                        v_operation.provider_object_id::text,\n                        v_operation.attempt_count,false;\n                    RETURN;\n                END IF;\n\n                UPDATE finance.provider_operations AS po\n                SET status='in_flight',\n                    attempt_count=po.attempt_count+1,\n                    lease_owner=p_lease_owner,\n                    lease_until=\n                        pg_catalog.clock_timestamp()+interval '30 seconds',\n                    lease_fence=po.lease_fence+1,\n                    last_error_code=NULL,\n                    last_started_at=pg_catalog.clock_timestamp(),\n                    updated_at=pg_catalog.clock_timestamp()\n                WHERE po.id=v_operation.id\n                RETURNING po.* INTO v_operation;\n\n                RETURN QUERY SELECT\n                    v_operation.id,v_operation.status::text,\n                    v_operation.lease_fence,\n                    v_operation.provider_object_id::text,\n                    v_operation.attempt_count,true;\n            END\n            $function$\n            "
_FINISH_ORIGINAL_SQL = "\n            CREATE OR REPLACE FUNCTION app_secure.finish_finance_provider_operation(\n                p_operation_id uuid,\n                p_lease_owner uuid,\n                p_lease_fence bigint,\n                p_outcome text,\n                p_provider_object_id text,\n                p_error_code text,\n                p_evidence_sha256 text\n            )\n            RETURNS TABLE(\n                operation_id uuid,\n                status text,\n                provider_object_id text,\n                attempt_count integer\n            )\n            LANGUAGE plpgsql\n            SECURITY DEFINER\n            SET search_path=pg_catalog,public,finance\n            SET row_security=on\n            AS $function$\n            DECLARE\n                v_org uuid;\n                v_operation finance.provider_operations%ROWTYPE;\n                v_payment finance.payments%ROWTYPE;\n            BEGIN\n                IF NOT pg_catalog.pg_has_role(\n                    session_user,'app_runtime','MEMBER'\n                ) THEN\n                    RAISE EXCEPTION\n                        'PAY-8 provider completion requires app_runtime'\n                        USING ERRCODE='42501';\n                END IF;\n                BEGIN\n                    v_org:=NULLIF(\n                        pg_catalog.current_setting(\n                            'app.current_org_id',true\n                        ),''\n                    )::uuid;\n                EXCEPTION WHEN invalid_text_representation THEN\n                    RAISE EXCEPTION\n                        'PAY-8 provider completion tenant invalid'\n                        USING ERRCODE='42501';\n                END;\n                IF v_org IS NULL\n                   OR p_operation_id IS NULL\n                   OR p_lease_owner IS NULL\n                   OR p_lease_fence IS NULL\n                   OR p_outcome NOT IN (\n                       'succeeded','failed_retryable',\n                       'failed_final','unknown'\n                   )\n                   OR (\n                       p_error_code IS NOT NULL\n                       AND p_error_code !~\n                           '^[a-z][a-z0-9_]{0,79}$'\n                   )\n                   OR (\n                       p_evidence_sha256 IS NOT NULL\n                       AND p_evidence_sha256 !~ '^[0-9a-f]{64}$'\n                   )\n                THEN\n                    RAISE EXCEPTION\n                        'PAY-8 provider completion invalid'\n                        USING ERRCODE='22023';\n                END IF;\n\n                SELECT o.* INTO v_operation\n                FROM finance.provider_operations o\n                WHERE o.id=p_operation_id\n                  AND o.organization_id=v_org\n                FOR UPDATE;\n                IF NOT FOUND THEN\n                    RAISE EXCEPTION\n                        'PAY-8 provider operation unavailable'\n                        USING ERRCODE='P0002';\n                END IF;\n                IF v_operation.status<>'in_flight'\n                   OR v_operation.lease_owner\n                      IS DISTINCT FROM p_lease_owner\n                   OR v_operation.lease_fence\n                      IS DISTINCT FROM p_lease_fence\n                THEN\n                    RAISE EXCEPTION\n                        'PAY-8 provider operation fence conflict'\n                        USING ERRCODE='40001';\n                END IF;\n\n                IF p_outcome='succeeded' THEN\n                    IF p_provider_object_id IS NULL\n                       OR p_provider_object_id !~\n                          '^[A-Za-z0-9_:-]{1,200}$'\n                       OR p_evidence_sha256 IS NULL\n                    THEN\n                        RAISE EXCEPTION\n                            'PAY-8 provider success evidence invalid'\n                            USING ERRCODE='22023';\n                    END IF;\n                    SELECT p.* INTO v_payment\n                    FROM finance.payments p\n                    WHERE p.id=v_operation.payment_id\n                      AND p.organization_id=v_org\n                    FOR UPDATE;\n                    IF NOT FOUND\n                       OR v_payment.provider_code\n                          IS DISTINCT FROM v_operation.provider_code\n                    THEN\n                        RAISE EXCEPTION\n                            'PAY-8 provider payment unavailable'\n                            USING ERRCODE='23514';\n                    END IF;\n                    IF v_payment.provider_order_ref IS NOT NULL\n                       AND v_payment.provider_order_ref NOT LIKE 'intent_%'\n                       AND v_payment.provider_order_ref\n                          IS DISTINCT FROM p_provider_object_id\n                    THEN\n                        RAISE EXCEPTION\n                            'PAY-8 provider object replay conflict'\n                            USING ERRCODE='23505';\n                    END IF;\n                    BEGIN\n                        UPDATE finance.payments\n                        SET provider_order_ref=p_provider_object_id,\n                            updated_at=pg_catalog.clock_timestamp()\n                        WHERE id=v_payment.id;\n                    EXCEPTION WHEN unique_violation THEN\n                        RAISE EXCEPTION\n                            'PAY-8 provider object already bound'\n                            USING ERRCODE='23505';\n                    END;\n                ELSIF p_outcome IN (\n                    'failed_retryable','failed_final','unknown'\n                ) THEN\n                    IF p_error_code IS NULL THEN\n                        RAISE EXCEPTION\n                            'PAY-8 provider failure code required'\n                            USING ERRCODE='22023';\n                    END IF;\n                END IF;\n\n                UPDATE finance.provider_operations AS po\n                SET status=p_outcome,\n                    provider_object_id=CASE\n                        WHEN p_provider_object_id IS NOT NULL\n                        THEN p_provider_object_id\n                        ELSE po.provider_object_id\n                    END,\n                    provider_evidence_sha256=CASE\n                        WHEN p_evidence_sha256 IS NOT NULL\n                        THEN p_evidence_sha256\n                        ELSE po.provider_evidence_sha256\n                    END,\n                    lease_owner=NULL,\n                    lease_until=NULL,\n                    last_error_code=CASE\n                        WHEN p_outcome='succeeded' THEN NULL\n                        ELSE p_error_code\n                    END,\n                    completed_at=CASE\n                        WHEN p_outcome IN (\n                            'succeeded','failed_final'\n                        )\n                        THEN pg_catalog.clock_timestamp()\n                        ELSE NULL\n                    END,\n                    updated_at=pg_catalog.clock_timestamp()\n                WHERE po.id=v_operation.id\n                RETURNING po.* INTO v_operation;\n\n                RETURN QUERY SELECT\n                    v_operation.id,v_operation.status::text,\n                    v_operation.provider_object_id::text,\n                    v_operation.attempt_count;\n            END\n            $function$\n            "

def _role_exists(bind, role_name: str) -> bool:
    return bool(bind.execute(
        sa.text("SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_roles WHERE rolname=:role)"),
        {"role": role_name},
    ).scalar_one())


def _identity(bind) -> tuple[str, str]:
    row = bind.execute(
        sa.text("SELECT session_user::text, current_user::text")
    ).one()
    return str(row[0]), str(row[1])


def _require_migration_owner(bind) -> None:
    session_name, current_name = _identity(bind)
    if session_name != current_name or session_name != "migration_owner":
        raise RuntimeError(
            "PAY24B RI1B1 requires session_user=current_user=migration_owner"
        )


def _can_set_security_owner(bind) -> bool:
    return bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.pg_has_role("
                "session_user, 'app_security_owner', 'SET')"
            )
        ).scalar_one()
    )


def _enter_security_owner(bind) -> None:
    _require_migration_owner(bind)
    if not _can_set_security_owner(bind):
        raise RuntimeError(
            "PAY24B RI1B1 migration_owner cannot SET ROLE app_security_owner"
        )
    bind.execute(sa.text("SET LOCAL ROLE app_security_owner"))
    session_name, current_name = _identity(bind)
    if (
        session_name != "migration_owner"
        or current_name != "app_security_owner"
    ):
        raise RuntimeError(
            "PAY24B RI1B1 failed to enter bounded app_security_owner context"
        )


def _reset_role(bind) -> None:
    bind.execute(sa.text("RESET ROLE"))
    _require_migration_owner(bind)


def _function_observation(bind, signature: str) -> dict:
    function_name, expected_argument_types = _FUNCTION_IDENTITIES[signature]
    rows = bind.execute(
        sa.text(
            """
            SELECT owner_role.rolname AS owner,
                   p.prosecdef AS security_definer,
                   pg_catalog.oidvectortypes(p.proargtypes) AS argument_types,
                   pg_catalog.has_function_privilege(
                       'app_runtime',p.oid,'EXECUTE'
                   ) AS app_execute,
                   pg_catalog.has_function_privilege(
                       'finance_payment_runtime',p.oid,'EXECUTE'
                   ) AS payment_execute,
                   pg_catalog.pg_get_functiondef(p.oid) AS definition
            FROM pg_catalog.pg_proc AS p
            JOIN pg_catalog.pg_namespace AS n ON n.oid=p.pronamespace
            JOIN pg_catalog.pg_roles AS owner_role ON owner_role.oid=p.proowner
            WHERE n.nspname='app_secure'
              AND p.proname=:function_name
              AND p.prokind='f'
            """
        ),
        {"function_name": function_name},
    ).mappings().all()
    if len(rows) != 1:
        raise RuntimeError(
            "PAY24B RI1B1 required exactly one app_secure function "
            f"for {function_name}; observed {len(rows)}"
        )
    row = rows[0]
    if row["argument_types"] != expected_argument_types:
        raise RuntimeError(f"PAY24B RI1B1 function signature drift: {signature}")
    return dict(row)


def _require_common_prerequisites(bind) -> None:
    _require_migration_owner(bind)
    for role_name in ('app_runtime','finance_payment_runtime','app_security_owner'):
        if not _role_exists(bind, role_name):
            raise RuntimeError(f"PAY24B RI1B1 required role missing: {role_name}")
    if not _can_set_security_owner(bind):
        raise RuntimeError(
            "PAY24B RI1B1 migration_owner lacks bounded SET authority "
            "for app_security_owner"
        )
    if not bind.execute(sa.text("SELECT pg_catalog.has_schema_privilege('finance_payment_runtime','app_secure','USAGE')")).scalar_one():
        raise RuntimeError('PAY24B RI1B1 payment runtime lacks app_secure USAGE')


def _require_expand_baseline(bind, signature: str) -> None:
    observed = _function_observation(bind, signature)
    if observed['owner'] != 'app_security_owner':
        raise RuntimeError(f"PAY24B RI1B1 unexpected function owner: {signature}")
    if not observed['security_definer']:
        raise RuntimeError(f"PAY24B RI1B1 function is not SECURITY DEFINER: {signature}")
    if not observed['app_execute']:
        raise RuntimeError(f"PAY24B RI1B1 legacy app authority missing: {signature}")
    if observed['payment_execute']:
        raise RuntimeError(f"PAY24B RI1B1 payment authority already present: {signature}")
    if "'app_runtime'" not in observed['definition']:
        raise RuntimeError(f"PAY24B RI1B1 app guard missing: {signature}")
    if "'finance_payment_runtime'" in observed['definition']:
        raise RuntimeError(f"PAY24B RI1B1 payment guard already present: {signature}")

def _require_expanded(bind, signature: str) -> None:
    observed = _function_observation(bind, signature)
    if observed['owner'] != 'app_security_owner' or not observed['security_definer']:
        raise RuntimeError(f"PAY24B RI1B1 expanded function identity drift: {signature}")
    if not observed['app_execute'] or not observed['payment_execute']:
        raise RuntimeError(f"PAY24B RI1B1 dual EXECUTE authority missing: {signature}")
    if "'app_runtime'" not in observed['definition'] or "'finance_payment_runtime'" not in observed['definition']:
        raise RuntimeError(f"PAY24B RI1B1 expanded guard missing: {signature}")

def upgrade() -> None:
    bind = op.get_bind()
    _require_common_prerequisites(bind)
    _require_expand_baseline(bind, _CLAIM_SIGNATURE)
    _require_expand_baseline(bind, _FINISH_SIGNATURE)

    _enter_security_owner(bind)
    op.execute(_CLAIM_EXPANDED_SQL)
    op.execute(_FINISH_EXPANDED_SQL)
    op.execute('REVOKE ALL ON FUNCTION app_secure.claim_finance_provider_operation(uuid,uuid) FROM PUBLIC')
    op.execute('REVOKE ALL ON FUNCTION app_secure.finish_finance_provider_operation(uuid,uuid,bigint,text,text,text,text) FROM PUBLIC')
    op.execute('GRANT EXECUTE ON FUNCTION app_secure.claim_finance_provider_operation(uuid,uuid) TO app_runtime')
    op.execute('GRANT EXECUTE ON FUNCTION app_secure.finish_finance_provider_operation(uuid,uuid,bigint,text,text,text,text) TO app_runtime')
    op.execute('GRANT EXECUTE ON FUNCTION app_secure.claim_finance_provider_operation(uuid,uuid) TO finance_payment_runtime')
    op.execute('GRANT EXECUTE ON FUNCTION app_secure.finish_finance_provider_operation(uuid,uuid,bigint,text,text,text,text) TO finance_payment_runtime')
    # RESET is success-only: transaction rollback clears the LOCAL role after
    # failed SQL without masking the original database error.
    _reset_role(bind)

    _require_expanded(bind, _CLAIM_SIGNATURE)
    _require_expanded(bind, _FINISH_SIGNATURE)


def downgrade() -> None:
    bind = op.get_bind()
    _require_common_prerequisites(bind)
    _require_expanded(bind, _CLAIM_SIGNATURE)
    _require_expanded(bind, _FINISH_SIGNATURE)

    _enter_security_owner(bind)
    op.execute(_CLAIM_ORIGINAL_SQL)
    op.execute(_FINISH_ORIGINAL_SQL)
    op.execute('REVOKE EXECUTE ON FUNCTION app_secure.claim_finance_provider_operation(uuid,uuid) FROM finance_payment_runtime')
    op.execute('REVOKE EXECUTE ON FUNCTION app_secure.finish_finance_provider_operation(uuid,uuid,bigint,text,text,text,text) FROM finance_payment_runtime')
    op.execute('GRANT EXECUTE ON FUNCTION app_secure.claim_finance_provider_operation(uuid,uuid) TO app_runtime')
    op.execute('GRANT EXECUTE ON FUNCTION app_secure.finish_finance_provider_operation(uuid,uuid,bigint,text,text,text,text) TO app_runtime')
    op.execute('REVOKE ALL ON FUNCTION app_secure.claim_finance_provider_operation(uuid,uuid) FROM PUBLIC')
    op.execute('REVOKE ALL ON FUNCTION app_secure.finish_finance_provider_operation(uuid,uuid,bigint,text,text,text,text) FROM PUBLIC')
    _reset_role(bind)

    _require_expand_baseline(bind, _CLAIM_SIGNATURE)
    _require_expand_baseline(bind, _FINISH_SIGNATURE)
