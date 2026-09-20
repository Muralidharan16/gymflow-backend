from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import uuid

import psycopg
import pytest
from psycopg.errors import InsufficientPrivilege

from tests import test_pay4_member_finance_binding_runtime as pay4


WORKER_URL=os.environ.get("PAY5_WORKER_DATABASE_URL")
ADMIN_URL=os.environ.get("PAY5_ADMIN_DATABASE_URL")

pytestmark=pytest.mark.skipif(
    not (
        WORKER_URL
        and ADMIN_URL
        and pay4.APP_URL
        and pay4.WORKER_URL
        and pay4.MIGRATION_URL
        and pay4.ADMIN_URL
    ),
    reason="PAY-5 isolated PG16 harness is not configured",
)

WORKER_A=uuid.UUID("55000000-0000-4000-8000-000000000001")
WORKER_B=uuid.UUID("55000000-0000-4000-8000-000000000002")


def _cleanup_pay5() -> None:
    if ADMIN_URL:
        with psycopg.connect(ADMIN_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM public.member_subscription_finance_event_consumptions "
                    "WHERE org_id=%s",
                    (pay4.ORG,),
                )
            conn.commit()
    pay4._cleanup()


@pytest.fixture(autouse=True)
def _fresh():
    _cleanup_pay5()
    yield
    _cleanup_pay5()


def _seed_ready(*, start_offset_days: int = 0):
    term=pay4._seed_pending(start_offset_days=start_offset_days)
    pay4._seed_invoice_and_binding(term)
    pay4._finance_event(payment_status="captured",allocated="100.00")
    return term


def _claim(worker_id: uuid.UUID, *, batch_size: int = 1, lease_seconds: int = 60):
    with psycopg.connect(WORKER_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM app_secure.claim_member_subscription_finance_events(%s,%s,%s)
                """,
                (worker_id,batch_size,lease_seconds),
            )
            rows=cur.fetchall()
        conn.commit()
        return rows


def _consume(
    worker_id: uuid.UUID,
    fence: int,
    *,
    commit: bool = True,
    org=pay4.ORG,
):
    with psycopg.connect(WORKER_URL) as conn:
        with conn.cursor() as cur:
            pay4._set_org(cur,org)
            cur.execute(
                """
                SELECT *
                FROM app_secure.consume_member_subscription_finance_event(%s,%s,%s)
                """,
                (pay4.EVENT,worker_id,fence),
            )
            row=cur.fetchone()
        if commit:
            conn.commit()
        else:
            conn.rollback()
        return row


def _ack(worker_id: uuid.UUID, fence: int, *, org=pay4.ORG):
    with psycopg.connect(WORKER_URL) as conn:
        with conn.cursor() as cur:
            pay4._set_org(cur,org)
            cur.execute(
                """
                SELECT app_secure.acknowledge_member_subscription_finance_event(%s,%s,%s)
                """,
                (pay4.EVENT,worker_id,fence),
            )
            result=cur.fetchone()[0]
        conn.commit()
        return result


def _release(
    worker_id: uuid.UUID,
    fence: int,
    *,
    error_code: str = "retryable_test",
    permanent: bool = False,
):
    with psycopg.connect(WORKER_URL) as conn:
        with conn.cursor() as cur:
            pay4._set_org(cur)
            cur.execute(
                """
                SELECT app_secure.release_member_subscription_finance_event(
                    %s,%s,%s,%s,%s
                )
                """,
                (pay4.EVENT,worker_id,fence,error_code,permanent),
            )
            result=cur.fetchone()[0]
        conn.commit()
        return result


def _expire_lease() -> None:
    with psycopg.connect(ADMIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE finance.outbox_events
                SET leased_until=pg_catalog.clock_timestamp()-INTERVAL '1 second'
                WHERE id=%s
                """,
                (pay4.EVENT,),
            )
        conn.commit()


def _state():
    with psycopg.connect(ADMIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT status,attempt_count,max_attempts,lease_fence,leased_by,
                       leased_until IS NOT NULL,published_at IS NOT NULL,
                       acknowledged_at IS NOT NULL,last_error_code
                FROM finance.outbox_events
                WHERE id=%s
                """,
                (pay4.EVENT,),
            )
            return cur.fetchone()


def _consumption_count() -> int:
    with psycopg.connect(ADMIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*)
                FROM public.member_subscription_finance_event_consumptions
                WHERE finance_event_id=%s
                """,
                (pay4.EVENT,),
            )
            return cur.fetchone()[0]


def _finance_subscription_event_count() -> int:
    with psycopg.connect(ADMIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*)
                FROM public.subscription_events
                WHERE org_id=%s
                  AND event_source='finance'
                  AND metadata->>'finance_event_id'=%s
                """,
                (pay4.ORG,str(pay4.EVENT)),
            )
            return cur.fetchone()[0]


def test_two_workers_racing_claim_get_exactly_one_owner():
    _seed_ready()

    with ThreadPoolExecutor(max_workers=2) as pool:
        rows=list(pool.map(_claim,(WORKER_A,WORKER_B)))

    claimed=[item[0] for item in rows if item]
    assert len(claimed)==1
    row=claimed[0]
    assert row[0]==pay4.EVENT
    assert row[1]==pay4.ORG
    assert row[3]==1
    assert row[4]==15
    assert row[5]==1

    state=_state()
    assert state[0]=="processing"
    assert state[1]==1
    assert state[3]==1
    assert state[4] in (WORKER_A,WORKER_B)

    with psycopg.connect(WORKER_URL) as conn:
        with conn.cursor() as cur:
            with pytest.raises(InsufficientPrivilege):
                cur.execute("SELECT count(*) FROM finance.outbox_events")
        conn.rollback()


def test_worker_cannot_directly_execute_pay4_activation():
    _seed_ready()
    with psycopg.connect(WORKER_URL) as conn:
        with conn.cursor() as cur:
            pay4._set_org(cur)
            with pytest.raises(InsufficientPrivilege):
                cur.execute(
                    "SELECT * FROM app_secure.apply_member_subscription_finance_event(%s,%s)",
                    (pay4.EVENT,"forbidden-direct-pay4"),
                )
        conn.rollback()


def test_worker_death_before_consumer_commit_rolls_back_and_reclaims():
    term=_seed_ready()
    first=_claim(WORKER_A)[0]
    fence=int(first[5])

    consumed=_consume(WORKER_A,fence,commit=False)
    assert consumed[0]==term
    assert pay4._status(term)=="pending_payment"
    assert _consumption_count()==0
    assert _finance_subscription_event_count()==0

    _expire_lease()
    reclaimed=_claim(WORKER_B)[0]
    assert reclaimed[3]==1
    assert reclaimed[5]==fence+1

    applied=_consume(WORKER_B,int(reclaimed[5]))
    assert applied[0]==term
    assert applied[2] is True
    assert applied[3] is False
    assert _ack(WORKER_B,int(reclaimed[5])) is True

    assert pay4._status(term)=="active"
    assert _consumption_count()==1
    assert _finance_subscription_event_count()==1
    state=_state()
    assert state[0]=="published"
    assert state[6] is True
    assert state[7] is True


def test_worker_death_after_consumer_commit_before_ack_replays_only_consumption():
    term=_seed_ready()
    first=_claim(WORKER_A)[0]
    fence=int(first[5])

    applied=_consume(WORKER_A,fence)
    assert applied[0]==term
    assert applied[2] is True
    assert applied[3] is False
    assert pay4._status(term)=="active"
    assert _consumption_count()==1
    assert _finance_subscription_event_count()==1
    assert _state()[0]=="processing"

    _expire_lease()
    reclaimed=_claim(WORKER_B)[0]
    assert reclaimed[3]==1
    assert reclaimed[5]==fence+1

    replay=_consume(WORKER_B,int(reclaimed[5]))
    assert replay[0]==term
    assert replay[2] is False
    assert replay[3] is True
    assert _consumption_count()==1
    assert _finance_subscription_event_count()==1

    assert _ack(WORKER_B,int(reclaimed[5])) is True
    state=_state()
    assert state[0]=="published"
    assert state[1]==1
    assert state[7] is True


def test_stale_worker_and_fence_cannot_consume_or_ack_after_reclaim():
    term=_seed_ready()
    first=_claim(WORKER_A)[0]
    old_fence=int(first[5])
    _expire_lease()
    second=_claim(WORKER_B)[0]
    new_fence=int(second[5])
    assert new_fence==old_fence+1

    with pytest.raises(psycopg.Error):
        _consume(WORKER_A,old_fence)

    applied=_consume(WORKER_B,new_fence)
    assert applied[0]==term
    assert _ack(WORKER_B,new_fence) is True

    with pytest.raises(psycopg.Error):
        _ack(WORKER_A,old_fence)

    assert _consumption_count()==1
    assert _finance_subscription_event_count()==1


def test_two_consumers_racing_same_live_claim_converge_to_one_effect():
    term=_seed_ready()
    claim=_claim(WORKER_A)[0]
    fence=int(claim[5])

    def consume_same_claim(_):
        return _consume(WORKER_A,fence)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(consume_same_claim,range(2)))

    assert sorted((bool(r[2]),bool(r[3])) for r in results)==[
        (False,True),
        (True,False),
    ]
    assert pay4._status(term)=="active"
    assert _consumption_count()==1
    assert _finance_subscription_event_count()==1
    assert _ack(WORKER_A,fence) is True


def test_ack_before_product_commit_is_rejected():
    _seed_ready()
    claim=_claim(WORKER_A)[0]
    with pytest.raises(psycopg.Error):
        _ack(WORKER_A,int(claim[5]))
    assert _state()[0]=="processing"
    assert _consumption_count()==0


def test_retry_release_rotates_attempt_only_on_new_pending_claim():
    _seed_ready()
    first=_claim(WORKER_A)[0]
    first_fence=int(first[5])
    assert first[3]==1

    assert _release(WORKER_A,first_fence,permanent=False)=="pending"
    released=_state()
    assert released[0]=="pending"
    assert released[1]==1
    assert released[4] is None

    second=_claim(WORKER_B)[0]
    assert second[3]==2
    assert second[5]==first_fence+1


def test_permanent_release_fails_closed_and_is_not_reclaimed():
    _seed_ready()
    first=_claim(WORKER_A)[0]
    assert _release(
        WORKER_A,int(first[5]),error_code="deterministic_test",permanent=True
    )=="failed"

    state=_state()
    assert state[0]=="failed"
    assert state[8]=="deterministic_test"
    assert _claim(WORKER_B)==[]


def test_published_event_is_not_claimed_again_by_duplicate_poller_delivery():
    term=_seed_ready()
    claim=_claim(WORKER_A)[0]
    fence=int(claim[5])
    _consume(WORKER_A,fence)
    _ack(WORKER_A,fence)

    assert pay4._status(term)=="active"
    assert _claim(WORKER_B)==[]
    assert _consumption_count()==1
    assert _finance_subscription_event_count()==1


def test_cross_tenant_consumer_context_is_rejected():
    term=_seed_ready()
    claim=_claim(WORKER_A)[0]
    fence=int(claim[5])
    with pytest.raises(psycopg.Error):
        _consume(WORKER_A,fence,org=pay4.OTHER_ORG)
    assert pay4._status(term)=="pending_payment"
    assert _consumption_count()==0
