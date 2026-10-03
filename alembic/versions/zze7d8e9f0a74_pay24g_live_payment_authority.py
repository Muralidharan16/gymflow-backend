"""PAY-24-G terminal-only live captured-payment authority.

Revision ID: zze7d8e9f0a74
Revises: zzd7d8e9f0a73
Create Date: 2026-10-03

Live provider evidence is isolated to finance_reconciliation_runtime. Ordinary
app_runtime remains razorpay_sandbox-only. Both live provider evidence and
subsequent payment application recheck the durable PAY-24-A Stage-1 internal
organization posture so rollback/closing fails closed on either side.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "zze7d8e9f0a74"
down_revision = "zzd7d8e9f0a73"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_RECONCILIATION = "finance_reconciliation_runtime"
_CONFIRM_NAME = "confirm_finance_provider_evidence"
_APPLY_NAME = "apply_finance_confirmed_payment"
_RESERVE_NAME = "reserve_finance_idempotency"
_COMPLETE_NAME = "complete_finance_idempotency"

_OLD_RESERVE_GATE = """            IF NOT pg_catalog.pg_has_role(session_user, 'app_runtime', 'MEMBER') THEN
                RAISE EXCEPTION 'P4D finance idempotency requires app_runtime' USING ERRCODE='42501';
            END IF;
"""

_NEW_RESERVE_GATE = """            IF NOT pg_catalog.pg_has_role(session_user, 'app_runtime', 'MEMBER') THEN
                IF NOT (
                    p_scope = 'finance.provider.capture.confirm'
                    AND pg_catalog.pg_has_role(
                        session_user,
                        'finance_reconciliation_runtime',
                        'MEMBER'
                    )
                ) THEN
                    RAISE EXCEPTION 'P4D finance idempotency requires app_runtime' USING ERRCODE='42501';
                END IF;
            END IF;
"""

_OLD_COMPLETE_GATE = """            IF NOT pg_catalog.pg_has_role(session_user, 'app_runtime', 'MEMBER') THEN
                RAISE EXCEPTION 'P4D finance idempotency completion requires app_runtime' USING ERRCODE='42501';
            END IF;
"""

_NEW_COMPLETE_GATE = """            IF NOT pg_catalog.pg_has_role(session_user, 'app_runtime', 'MEMBER') THEN
                IF NOT pg_catalog.pg_has_role(
                    session_user,
                    'finance_reconciliation_runtime',
                    'MEMBER'
                ) THEN
                    RAISE EXCEPTION 'P4D finance idempotency completion requires app_runtime' USING ERRCODE='42501';
                END IF;
            END IF;
"""

_COMPLETE_VALIDATION_MARKER = """            IF p_id IS NULL OR p_response_ref IS NULL OR pg_catalog.btrim(p_response_ref) = '' THEN
                RAISE EXCEPTION 'P4D finance idempotency completion invalid' USING ERRCODE='22023';
            END IF;
"""

_COMPLETE_RECON_SCOPE_FENCE = """
            IF NOT pg_catalog.pg_has_role(
                session_user,
                'app_runtime',
                'MEMBER'
            ) THEN
                PERFORM 1
                FROM finance.idempotency_keys key_data
                WHERE key_data.id = p_id
                  AND key_data.organization_id = v_current_org_id
                  AND key_data.scope = 'finance.provider.capture.confirm';
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'PAY-24-G finance idempotency completion scope unavailable'
                        USING ERRCODE='42501';
                END IF;
            END IF;
"""

_OLD_PROVIDER_GATE = """                IF NOT pg_catalog.pg_has_role(
                    session_user,
                    'app_runtime',
                    'MEMBER'
                ) THEN
                    RAISE EXCEPTION
                        'P4D provider evidence requires app_runtime'
                        USING ERRCODE='42501';
                END IF;

                IF p_provider_code IS DISTINCT FROM
                    'razorpay_sandbox'
                THEN
                    RAISE EXCEPTION
                        'P4D provider evidence provider unavailable'
                        USING ERRCODE='42501';
                END IF;
"""

_NEW_PROVIDER_GATE = """                IF p_provider_code = 'razorpay_sandbox' THEN
                    IF NOT pg_catalog.pg_has_role(
                        session_user,
                        'app_runtime',
                        'MEMBER'
                    ) THEN
                        RAISE EXCEPTION
                            'P4D provider evidence requires app_runtime'
                            USING ERRCODE='42501';
                    END IF;
                ELSIF p_provider_code = 'razorpay' THEN
                    IF NOT pg_catalog.pg_has_role(
                        session_user,
                        'finance_reconciliation_runtime',
                        'MEMBER'
                    ) THEN
                        RAISE EXCEPTION
                            'PAY-24-G live evidence requires finance_reconciliation_runtime'
                            USING ERRCODE='42501';
                    END IF;
                ELSE
                    RAISE EXCEPTION
                        'P4D provider evidence provider unavailable'
                        USING ERRCODE='42501';
                END IF;
"""

_PROVIDER_ORG_MARKER = """                IF v_payment.organization_id IS NULL THEN
                    RAISE EXCEPTION
                        'P4D provider evidence payment unavailable'
                        USING ERRCODE='P0002';
                END IF;
"""

_APPLY_PAYMENT_MARKER = """                IF NOT FOUND
                   OR v_payment.organization_id IS NULL
                THEN
                    RAISE EXCEPTION
                        'P4D finance payment application payment unavailable'
                        USING ERRCODE='42501';
                END IF;
"""

_LIVE_STAGE_FENCE = """
                IF p_provider_code = 'razorpay' THEN
                    PERFORM 1
                    FROM finance.payment_activation_authority a
                    WHERE a.singleton
                      AND a.stage=1
                      AND a.provider_egress_state='open'
                      AND a.internal_organization_id=v_payment.organization_id
                      AND a.checkout IS TRUE
                      AND a.webhooks IS TRUE
                      AND a.payment_application IS TRUE
                      AND a.subscription_activation IS TRUE
                      AND a.refund_execution IS FALSE
                      AND a.recurring_billing IS FALSE
                      AND a.dunning IS FALSE
                      AND a.platform_billing IS FALSE
                      AND a.release_identity_id IS NOT NULL
                      AND a.authorization_record_id IS NOT NULL
                      AND a.authorization_id IS NOT NULL
                      AND a.authorized_stage=1
                      AND a.certified_sha IS NOT NULL
                      AND a.deployed_sha=a.certified_sha
                      AND a.authorized_sha=a.certified_sha;
                    IF NOT FOUND THEN
                        RAISE EXCEPTION
                            'PAY-24-G live provider evidence denied by Stage-1 authority'
                            USING ERRCODE='42501';
                    END IF;
                END IF;
"""

_LIVE_APPLY_FENCE = """
                IF v_payment.provider_code = 'razorpay' THEN
                    PERFORM 1
                    FROM finance.payment_activation_authority a
                    WHERE a.singleton
                      AND a.stage=1
                      AND a.provider_egress_state='open'
                      AND a.internal_organization_id=v_payment.organization_id
                      AND a.checkout IS TRUE
                      AND a.webhooks IS TRUE
                      AND a.payment_application IS TRUE
                      AND a.subscription_activation IS TRUE
                      AND a.refund_execution IS FALSE
                      AND a.recurring_billing IS FALSE
                      AND a.dunning IS FALSE
                      AND a.platform_billing IS FALSE
                      AND a.release_identity_id IS NOT NULL
                      AND a.authorization_record_id IS NOT NULL
                      AND a.authorization_id IS NOT NULL
                      AND a.authorized_stage=1
                      AND a.certified_sha IS NOT NULL
                      AND a.deployed_sha=a.certified_sha
                      AND a.authorized_sha=a.certified_sha;
                    IF NOT FOUND THEN
                        RAISE EXCEPTION
                            'PAY-24-G live payment application denied by Stage-1 authority'
                            USING ERRCODE='42501';
                    END IF;
                END IF;
"""


def _definition(bind, name: str, pronargs: int) -> str:
    row = bind.execute(
        sa.text(
            """
            SELECT pg_catalog.pg_get_functiondef(p.oid)
            FROM pg_catalog.pg_proc p
            JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname='app_secure'
              AND p.proname=:name
              AND p.pronargs=:pronargs
            """
        ),
        {"name": name, "pronargs": pronargs},
    ).one_or_none()
    if row is None:
        raise RuntimeError(f"PAY-24-G required function missing: {name}")
    return str(row[0])


def _replace_once(source: str, old: str, new: str, label: str) -> str:
    if source.count(old) != 1:
        raise RuntimeError(f"PAY-24-G {label} predecessor drift")
    return source.replace(old, new, 1)


def _preflight(bind) -> tuple[str, str, str, str]:
    identity = bind.execute(
        sa.text("SELECT session_user::text,current_user::text")
    ).one()
    if tuple(identity) != (_MIGRATION_OWNER, _MIGRATION_OWNER):
        raise RuntimeError("PAY-24-G migration requires migration_owner")
    head = bind.execute(
        sa.text("SELECT version_num::text FROM alembic_version")
    ).scalar_one()
    if head != down_revision:
        raise RuntimeError(
            f"PAY-24-G predecessor drift: expected {down_revision}, found {head}"
        )

    role = bind.execute(
        sa.text(
            """
            SELECT rolcanlogin,rolsuper,rolinherit,rolcreatedb,rolcreaterole,
                   rolreplication,rolbypassrls
            FROM pg_catalog.pg_roles WHERE rolname=:role
            """
        ),
        {"role": _RECONCILIATION},
    ).mappings().one_or_none()
    if role is None or bool(role["rolcanlogin"]):
        raise RuntimeError("PAY-24-G reconciliation capability role drift")
    for field in (
        "rolsuper","rolinherit","rolcreatedb","rolcreaterole",
        "rolreplication","rolbypassrls",
    ):
        if bool(role[field]):
            raise RuntimeError(
                f"PAY-24-G reconciliation role is not reduced: {field}"
            )

    confirm = _definition(bind, _CONFIRM_NAME, 10)
    apply = _definition(bind, _APPLY_NAME, 6)
    reserve = _definition(bind, _RESERVE_NAME, 5)
    complete = _definition(bind, _COMPLETE_NAME, 2)
    if _OLD_PROVIDER_GATE not in confirm:
        raise RuntimeError("PAY-24-G provider-evidence predecessor drift")
    if _LIVE_STAGE_FENCE.strip() in confirm:
        raise RuntimeError("PAY-24-G live evidence fence already exists")
    if _LIVE_APPLY_FENCE.strip() in apply:
        raise RuntimeError("PAY-24-G live application fence already exists")
    if _OLD_RESERVE_GATE not in reserve:
        raise RuntimeError("PAY-24-G idempotency reserve predecessor drift")
    if _OLD_COMPLETE_GATE not in complete:
        raise RuntimeError("PAY-24-G idempotency completion predecessor drift")
    if _COMPLETE_RECON_SCOPE_FENCE.strip() in complete:
        raise RuntimeError("PAY-24-G idempotency completion fence already exists")

    live_events = bind.execute(
        sa.text(
            "SELECT count(*) FROM finance.payment_events "
            "WHERE provider_code='razorpay'"
        )
    ).scalar_one()
    if int(live_events) != 0:
        raise RuntimeError("PAY-24-G unexpected predecessor live payment events")
    return confirm, apply, reserve, complete


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    confirm, apply, reserve, complete = _preflight(bind)

    confirm = _replace_once(
        confirm,
        _OLD_PROVIDER_GATE,
        _NEW_PROVIDER_GATE,
        "provider role gate",
    )
    confirm = _replace_once(
        confirm,
        _PROVIDER_ORG_MARKER,
        _PROVIDER_ORG_MARKER + _LIVE_STAGE_FENCE,
        "live evidence Stage-1 fence",
    )
    apply = _replace_once(
        apply,
        _APPLY_PAYMENT_MARKER,
        _APPLY_PAYMENT_MARKER + _LIVE_APPLY_FENCE,
        "live payment application Stage-1 fence",
    )
    reserve = _replace_once(
        reserve,
        _OLD_RESERVE_GATE,
        _NEW_RESERVE_GATE,
        "live evidence idempotency reserve gate",
    )
    complete = _replace_once(
        complete,
        _OLD_COMPLETE_GATE,
        _NEW_COMPLETE_GATE,
        "live evidence idempotency completion role gate",
    )
    complete = _replace_once(
        complete,
        _COMPLETE_VALIDATION_MARKER,
        _COMPLETE_VALIDATION_MARKER + _COMPLETE_RECON_SCOPE_FENCE,
        "live evidence idempotency completion scope fence",
    )

    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        op.execute(reserve)
        op.execute(complete)
        op.execute(confirm)
        op.execute(apply)
        op.execute(
            "GRANT USAGE ON SCHEMA app_secure "
            "TO finance_reconciliation_runtime"
        )
        op.execute(
            """
            GRANT EXECUTE ON FUNCTION
                app_secure.confirm_finance_provider_evidence(
                    text,text,text,text,text,bigint,text,text,text,text
                )
            TO finance_reconciliation_runtime
            """
        )
    finally:
        op.execute("RESET ROLE")

    current_confirm = _definition(bind, _CONFIRM_NAME, 10)
    current_apply = _definition(bind, _APPLY_NAME, 6)
    current_reserve = _definition(bind, _RESERVE_NAME, 5)
    current_complete = _definition(bind, _COMPLETE_NAME, 2)
    for token in (
        "finance_reconciliation_runtime",
        "PAY-24-G live provider evidence denied by Stage-1 authority",
    ):
        if token not in current_confirm:
            raise RuntimeError(f"PAY-24-G confirm function missing {token}")
    if "PAY-24-G live payment application denied by Stage-1 authority" not in current_apply:
        raise RuntimeError("PAY-24-G apply function fence missing")
    if "finance.provider.capture.confirm" not in current_reserve:
        raise RuntimeError("PAY-24-G reserve helper scope fence missing")
    if "PAY-24-G finance idempotency completion scope unavailable" not in current_complete:
        raise RuntimeError("PAY-24-G completion helper scope fence missing")

    reserve_signature = (
        "app_secure.reserve_finance_idempotency("
        "text,text,text,uuid,timestamp with time zone)"
    )
    complete_signature = "app_secure.complete_finance_idempotency(uuid,text)"
    for signature in (reserve_signature, complete_signature):
        can_execute = bind.execute(
            sa.text(
                "SELECT pg_catalog.has_function_privilege("
                ":role,:signature,'EXECUTE')"
            ),
            {"role": _RECONCILIATION, "signature": signature},
        ).scalar_one()
        if can_execute:
            raise RuntimeError(
                "PAY-24-G reconciliation must not directly execute "
                f"generic idempotency helper {signature}"
            )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(
        sa.text(
            "SELECT EXISTS("
            "SELECT 1 FROM finance.payment_events WHERE provider_code='razorpay'"
            ")"
        )
    ).scalar_one():
        raise RuntimeError(
            "PAY-24-G downgrade refused while live payment evidence exists"
        )
    if bind.execute(
        sa.text(
            "SELECT EXISTS("
            "SELECT 1 FROM finance.payments "
            "WHERE provider_code='razorpay' "
            "AND provider_payment_ref IS NOT NULL"
            ")"
        )
    ).scalar_one():
        raise RuntimeError(
            "PAY-24-G downgrade refused while live provider payment refs exist"
        )

    confirm = _definition(bind, _CONFIRM_NAME, 10)
    apply = _definition(bind, _APPLY_NAME, 6)
    reserve = _definition(bind, _RESERVE_NAME, 5)
    complete = _definition(bind, _COMPLETE_NAME, 2)
    confirm = _replace_once(
        confirm,
        _NEW_PROVIDER_GATE,
        _OLD_PROVIDER_GATE,
        "provider role downgrade",
    )
    confirm = _replace_once(
        confirm,
        _PROVIDER_ORG_MARKER + _LIVE_STAGE_FENCE,
        _PROVIDER_ORG_MARKER,
        "live evidence fence downgrade",
    )
    apply = _replace_once(
        apply,
        _APPLY_PAYMENT_MARKER + _LIVE_APPLY_FENCE,
        _APPLY_PAYMENT_MARKER,
        "live payment application fence downgrade",
    )
    reserve = _replace_once(
        reserve,
        _NEW_RESERVE_GATE,
        _OLD_RESERVE_GATE,
        "idempotency reserve gate downgrade",
    )
    complete = _replace_once(
        complete,
        _NEW_COMPLETE_GATE,
        _OLD_COMPLETE_GATE,
        "idempotency completion role gate downgrade",
    )
    complete = _replace_once(
        complete,
        _COMPLETE_VALIDATION_MARKER + _COMPLETE_RECON_SCOPE_FENCE,
        _COMPLETE_VALIDATION_MARKER,
        "idempotency completion scope fence downgrade",
    )

    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        op.execute(
            """
            REVOKE EXECUTE ON FUNCTION
                app_secure.confirm_finance_provider_evidence(
                    text,text,text,text,text,bigint,text,text,text,text
                )
            FROM finance_reconciliation_runtime
            """
        )
        op.execute(
            "REVOKE USAGE ON SCHEMA app_secure "
            "FROM finance_reconciliation_runtime"
        )
        op.execute(reserve)
        op.execute(complete)
        op.execute(confirm)
        op.execute(apply)
    finally:
        op.execute("RESET ROLE")
