from __future__ import annotations

from datetime import date, timedelta
import os
import uuid

import psycopg
import pytest
from psycopg.errors import InsufficientPrivilege

from tests import test_pay4_member_finance_binding_runtime as pay4


WORKER_URL = os.environ.get("PAY24C_WORKER_DATABASE_URL")
ENTITLEMENT_URL = os.environ.get("PAY24C_ENTITLEMENT_DATABASE_URL")
ADMIN_URL = os.environ.get("PAY24C_ADMIN_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not (
        WORKER_URL
        and ENTITLEMENT_URL
        and ADMIN_URL
        and pay4.APP_URL
        and pay4.WORKER_URL
        and pay4.MIGRATION_URL
        and pay4.ADMIN_URL
    ),
    reason="PAY-24-C isolated PG16 harness is not configured",
)

WORKER_A = uuid.UUID("24c00000-0000-4000-8000-000000000001")
ENTITLEMENT_A = uuid.UUID("24c00000-0000-4000-8000-000000000011")
ENTITLEMENT_B = uuid.UUID("24c00000-0000-4000-8000-000000000012")
ACTOR = uuid.UUID("24c00000-0000-4000-8000-000000000021")
REFUND = uuid.UUID("24c00000-0000-4000-8000-000000000031")
REFUND_EVENT = uuid.UUID("24c00000-0000-4000-8000-000000000032")


def _set_org(cur, org=pay4.ORG) -> None:
    pay4._set_org(cur, org)
    cur.execute(
        "SELECT pg_catalog.set_config('app.current_user_id',%s,true)",
        (str(ACTOR),),
    )


def _cleanup() -> None:
    if ADMIN_URL:
        with psycopg.connect(ADMIN_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM public.member_entitlement_commands "
                    "WHERE organization_id=%s",
                    (pay4.ORG,),
                )
                cur.execute(
                    "DELETE FROM public.member_subscription_finance_event_consumptions "
                    "WHERE org_id=%s",
                    (pay4.ORG,),
                )
                cur.execute(
                    "DELETE FROM public.subscription_freezes WHERE org_id=%s",
                    (pay4.ORG,),
                )
                cur.execute(
                    "DELETE FROM finance.refunds WHERE organization_id=%s",
                    (pay4.ORG,),
                )
            conn.commit()
    pay4._cleanup()


@pytest.fixture(autouse=True)
def _fresh():
    _cleanup()
    yield
    _cleanup()


def _seed_finance_ready(*, start_offset_days: int = 0):
    term = pay4._seed_pending(start_offset_days=start_offset_days)
    pay4._seed_invoice_and_binding(term)
    pay4._finance_event(payment_status="captured", allocated="100.00")
    return term


def _finance_claim(worker_id: uuid.UUID = WORKER_A):
    with psycopg.connect(WORKER_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM app_secure.claim_member_subscription_finance_events(
                    %s,1,600
                )
                """,
                (worker_id,),
            )
            row = cur.fetchone()
        conn.commit()
        return row


def _finance_consume_and_ack(claim, worker_id: uuid.UUID = WORKER_A):
    with psycopg.connect(WORKER_URL) as conn:
        with conn.cursor() as cur:
            _set_org(cur)
            cur.execute(
                """
                SELECT *
                FROM app_secure.consume_member_subscription_finance_event(
                    %s,%s,%s
                )
                """,
                (pay4.EVENT, worker_id, claim[5]),
            )
            consumed = cur.fetchone()
        conn.commit()
    with psycopg.connect(WORKER_URL) as conn:
        with conn.cursor() as cur:
            _set_org(cur)
            cur.execute(
                """
                SELECT app_secure.acknowledge_member_subscription_finance_event(
                    %s,%s,%s
                )
                """,
                (pay4.EVENT, worker_id, claim[5]),
            )
            assert cur.fetchone()[0] is True
        conn.commit()
    return consumed


def _entitlement_claim(worker_id: uuid.UUID = ENTITLEMENT_A):
    with psycopg.connect(ENTITLEMENT_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM app_secure.pay24c_claim_entitlement_commands(%s,1,600)
                """,
                (worker_id,),
            )
            row = cur.fetchone()
        conn.commit()
        return row


def _entitlement_apply(
    claim,
    worker_id: uuid.UUID = ENTITLEMENT_A,
    *,
    commit: bool = True,
):
    with psycopg.connect(ENTITLEMENT_URL) as conn:
        with conn.cursor() as cur:
            _set_org(cur)
            cur.execute(
                """
                SELECT *
                FROM app_secure.pay24c_apply_entitlement_command(%s,%s,%s)
                """,
                (claim[0], worker_id, claim[5]),
            )
            row = cur.fetchone()
        if commit:
            conn.commit()
        else:
            conn.rollback()
        return row


def _request_admin(
    term_id,
    command_type: str,
    key: str,
    *,
    reason: str | None = None,
    until: date | None = None,
):
    with psycopg.connect(pay4.APP_URL) as conn:
        with conn.cursor() as cur:
            _set_org(cur)
            cur.execute(
                """
                SELECT *
                FROM app_secure.pay24c_request_admin_entitlement(
                    %s,%s,%s,%s,%s
                )
                """,
                (term_id, command_type, key, reason, until),
            )
            row = cur.fetchone()
        conn.commit()
        return row


def _access() -> bool:
    with psycopg.connect(pay4.APP_URL) as conn:
        with conn.cursor() as cur:
            _set_org(cur)
            cur.execute(
                "SELECT app_secure.member_entitlement_access_active(%s,%s)",
                (pay4.ORG, pay4.MEMBER),
            )
            return bool(cur.fetchone()[0])


def _command_count(command_type: str | None = None) -> int:
    with psycopg.connect(ADMIN_URL) as conn:
        with conn.cursor() as cur:
            if command_type is None:
                cur.execute(
                    "SELECT count(*) FROM public.member_entitlement_commands "
                    "WHERE organization_id=%s",
                    (pay4.ORG,),
                )
            else:
                cur.execute(
                    "SELECT count(*) FROM public.member_entitlement_commands "
                    "WHERE organization_id=%s AND command_type=%s",
                    (pay4.ORG, command_type),
                )
            return int(cur.fetchone()[0])


def _expire_command_lease(command_id) -> None:
    with psycopg.connect(ADMIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE public.member_entitlement_commands
                SET leased_until=pg_catalog.clock_timestamp()-INTERVAL '1 second'
                WHERE command_id=%s
                """,
                (command_id,),
            )
        conn.commit()


def _activate_paid():
    term = _seed_finance_ready()
    finance_claim = _finance_claim()
    assert finance_claim is not None
    consumed = _finance_consume_and_ack(finance_claim)
    assert consumed[0] == term
    assert consumed[1] == "entitlement_pending"
    assert consumed[2] is False
    assert pay4._status(term) == "pending_payment"
    assert _command_count("activate_paid") == 1

    claim = _entitlement_claim()
    assert claim is not None
    applied = _entitlement_apply(claim)
    assert applied[1] == term
    assert applied[2] == "active"
    assert applied[3] is True
    assert pay4._status(term) == "active"
    return term


def test_payment_fact_only_enqueues_until_entitlement_runtime_applies():
    term = _seed_finance_ready()
    claim = _finance_claim()
    consumed = _finance_consume_and_ack(claim)

    assert consumed[0] == term
    assert consumed[1] == "entitlement_pending"
    assert consumed[2] is False
    assert pay4._status(term) == "pending_payment"
    assert _access() is False
    assert _command_count("activate_paid") == 1

    entitlement_claim = _entitlement_claim()
    _entitlement_apply(entitlement_claim)
    assert pay4._status(term) == "active"
    assert _access() is True


def test_worker_and_api_cannot_bypass_entitlement_authority():
    term = _seed_finance_ready()
    claim = _finance_claim()

    with psycopg.connect(WORKER_URL) as conn:
        with conn.cursor() as cur:
            _set_org(cur)
            with pytest.raises(InsufficientPrivilege):
                cur.execute(
                    "SELECT * FROM app_secure.apply_member_subscription_finance_event(%s,%s)",
                    (pay4.EVENT, "pay24c:forbidden"),
                )
        conn.rollback()

    with psycopg.connect(pay4.APP_URL) as conn:
        with conn.cursor() as cur:
            _set_org(cur)
            with pytest.raises(InsufficientPrivilege):
                cur.execute(
                    "UPDATE public.subscription_terms SET status='active' WHERE id=%s",
                    (term,),
                )
        conn.rollback()

    _finance_consume_and_ack(claim)
    entitlement_claim = _entitlement_claim()

    with psycopg.connect(WORKER_URL) as conn:
        with conn.cursor() as cur:
            _set_org(cur)
            with pytest.raises(InsufficientPrivilege):
                cur.execute(
                    """
                    SELECT *
                    FROM app_secure.pay24c_apply_entitlement_command(%s,%s,%s)
                    """,
                    (entitlement_claim[0], WORKER_A, entitlement_claim[5]),
                )
        conn.rollback()


def test_duplicate_finance_delivery_creates_one_entitlement_command():
    term = _seed_finance_ready()
    claim = _finance_claim()
    first = _finance_consume_and_ack(claim)
    assert first[0] == term
    assert _command_count("activate_paid") == 1
    assert _finance_claim() is None
    assert _command_count("activate_paid") == 1


def test_crash_after_command_claim_before_commit_reclaims_without_duplicate_effect():
    term = _seed_finance_ready()
    _finance_consume_and_ack(_finance_claim())
    claim = _entitlement_claim(ENTITLEMENT_A)
    rolled_back = _entitlement_apply(
        claim, ENTITLEMENT_A, commit=False
    )
    assert rolled_back[1] == term
    assert pay4._status(term) == "pending_payment"
    assert _access() is False

    _expire_command_lease(claim[0])
    reclaimed = _entitlement_claim(ENTITLEMENT_B)
    assert reclaimed[0] == claim[0]
    assert reclaimed[5] == claim[5] + 1
    _entitlement_apply(reclaimed, ENTITLEMENT_B)

    assert pay4._status(term) == "active"
    assert _access() is True
    assert _command_count("activate_paid") == 1


def test_freeze_resume_cancel_use_one_command_authority_and_access_gate():
    term = _activate_paid()
    assert _access() is True

    freeze = _request_admin(
        term,
        "freeze",
        "pay24c:test:freeze",
        reason="member_requested",
        until=date.today() + timedelta(days=2),
    )
    assert freeze[1] == "pending"
    _entitlement_apply(_entitlement_claim())
    assert _access() is False

    resume = _request_admin(
        term,
        "resume",
        "pay24c:test:resume",
        reason="member_returned",
    )
    assert resume[1] == "pending"
    _entitlement_apply(_entitlement_claim())
    assert _access() is True

    cancel = _request_admin(
        term,
        "cancel",
        "pay24c:test:cancel",
        reason="member_requested",
    )
    assert cancel[1] == "pending"
    _entitlement_apply(_entitlement_claim())
    assert pay4._status(term) == "cancelled"
    assert _access() is False


def _seed_succeeded_refund_fact() -> None:
    # Infrastructure-superuser fixture only: seed the already-authoritative
    # post-PAY10 financial fact without performing provider I/O.
    with psycopg.connect(ADMIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL session_replication_role=replica")
            cur.execute(
                """
                INSERT INTO finance.refunds(
                    id,organization_id,payment_id,legal_entity_id,
                    division_id,brand_id,amount,status,reason_code,currency_code
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,100,'succeeded',
                    'pay24c_runtime_test','INR'
                )
                """,
                (
                    REFUND,
                    pay4.ORG,
                    pay4.PAYMENT,
                    pay4.ENTITY,
                    pay4.DIVISION,
                    pay4.BRAND,
                ),
            )
            cur.execute(
                """
                INSERT INTO finance.outbox_events(
                    id,organization_id,aggregate_type,aggregate_id,event_type,
                    idempotency_key,payload_json,payload_sha256,status,attempt_count
                ) VALUES (
                    %s,%s,'refund',%s,'finance.refund.completed',
                    'pay24c:refund:completed',
                    jsonb_build_object('refund_id',%s::text,'status','succeeded'),
                    %s,'pending',0
                )
                """,
                (
                    REFUND_EVENT,
                    pay4.ORG,
                    REFUND,
                    REFUND,
                    "b" * 64,
                ),
            )
        conn.commit()


def test_succeeded_refund_is_enqueued_then_revokes_entitlement_exactly_once():
    term = _activate_paid()
    assert _access() is True
    _seed_succeeded_refund_fact()

    with psycopg.connect(WORKER_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM app_secure.pay24c_claim_refund_events(%s,1,600)",
                (WORKER_A,),
            )
            claim = cur.fetchone()
        conn.commit()
    assert claim is not None

    with psycopg.connect(WORKER_URL) as conn:
        with conn.cursor() as cur:
            _set_org(cur)
            cur.execute(
                "SELECT app_secure.pay24c_consume_refund_event(%s,%s,%s)",
                (REFUND_EVENT, WORKER_A, claim[5]),
            )
            assert cur.fetchone()[0] == 1
            cur.execute(
                "SELECT app_secure.pay24c_ack_refund_event(%s,%s,%s)",
                (REFUND_EVENT, WORKER_A, claim[5]),
            )
            assert cur.fetchone()[0] is True
        conn.commit()

    assert pay4._status(term) == "active"
    assert _command_count("recompute_refund") == 1

    entitlement_claim = _entitlement_claim()
    applied = _entitlement_apply(entitlement_claim)
    assert applied[2] == "terminated"
    assert pay4._status(term) == "terminated"
    assert _access() is False
    assert _command_count("recompute_refund") == 1
