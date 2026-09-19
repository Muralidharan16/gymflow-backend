from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import uuid

import psycopg
import pytest

URL=os.environ.get("PAY3_RUNTIME_DATABASE_URL")
ADMIN_URL=os.environ.get("PAY3_ADMIN_DATABASE_URL")
ORG=uuid.UUID("33000000-0000-4000-8000-000000000001")
CORR=uuid.UUID("33000000-0000-4000-8000-000000000002")

pytestmark=pytest.mark.skipif(
    URL is None or ADMIN_URL is None,
    reason="PAY-3 isolated runtime harness is not configured",
)


def _reserve(
    conn,
    *,
    key="doers:pay:sub_123:1",
    request_hash="a"*64,
    business_ref="sub_123",
    actor_type="system",
    actor_hash="b"*64,
):
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('app.current_org_id',%s,false)",(str(ORG),))
        cur.execute(
            """
            SELECT command_id,status,response_ref,error_code,inserted,replayed
            FROM app_secure.reserve_finance_monetary_command(
                'finance.payment.capture',%s,%s,%s,%s,%s,%s
            )
            """,
            (key,request_hash,business_ref,CORR,actor_type,actor_hash),
        )
        return cur.fetchone()


def _reserve_committed(**kwargs):
    with psycopg.connect(URL) as conn:
        row=_reserve(conn,**kwargs)
        conn.commit()
        return row


def test_same_payload_replays_and_different_payload_or_actor_conflicts():
    with psycopg.connect(URL) as conn:
        first=_reserve(conn)
        replay=_reserve(conn)
        assert first[0]==replay[0]
        assert first[4:] == (True,False)
        assert replay[4:] == (False,True)
        with pytest.raises(psycopg.errors.UniqueViolation):
            _reserve(conn,request_hash="c"*64)
        conn.rollback()

    with psycopg.connect(URL) as conn:
        _reserve(conn,key="doers:pay:actor_fence:1",request_hash="e"*64,business_ref="actor_fence")
        with pytest.raises(psycopg.errors.UniqueViolation):
            _reserve(
                conn,
                key="doers:pay:actor_fence:1",
                request_hash="e"*64,
                business_ref="actor_fence",
                actor_type="member",
                actor_hash="f"*64,
            )
        conn.rollback()


def test_unknown_reason_is_durable_after_reconciliation():
    # Persist the original ambiguity evidence first.  A later conflicting report
    # must fail without rolling back the already-durable command history.
    with psycopg.connect(URL) as conn:
        row=_reserve(conn,key="doers:pay:sub_124:1",request_hash="d"*64,business_ref="sub_124")
        command_id=row[0]
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('app.current_org_id',%s,false)",(str(ORG),))
            cur.execute(
                "SELECT status,error_code,replayed FROM app_secure.mark_finance_monetary_command_unknown(%s,'provider_timeout')",
                (command_id,),
            )
            assert cur.fetchone()==("unknown","provider_timeout",False)
            cur.execute(
                "SELECT status,error_code,replayed FROM app_secure.mark_finance_monetary_command_unknown(%s,'provider_timeout')",
                (command_id,),
            )
            assert cur.fetchone()==("unknown","provider_timeout",True)
        conn.commit()

    # A different ambiguity reason for the same command is conflicting evidence,
    # not a replacement for the first durable observation.
    with psycopg.connect(URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('app.current_org_id',%s,false)",(str(ORG),))
            with pytest.raises(psycopg.errors.UniqueViolation):
                cur.execute(
                    "SELECT * FROM app_secure.mark_finance_monetary_command_unknown(%s,'provider_network_loss')",
                    (command_id,),
                )
        conn.rollback()

    # Reconciliation may resolve the command, but it must not erase the evidence
    # showing that the external outcome was once unknown.
    with psycopg.connect(URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('app.current_org_id',%s,false)",(str(ORG),))
            cur.execute(
                "SELECT status,response_ref,replayed FROM app_secure.complete_finance_monetary_command(%s,'payment:pay_124')",
                (command_id,),
            )
            assert cur.fetchone()==("succeeded","payment:pay_124",False)
        conn.commit()

    with psycopg.connect(ADMIN_URL) as admin:
        with admin.cursor() as cur:
            cur.execute(
                "SELECT status,response_ref,ambiguity_code,unknown_at IS NOT NULL "
                "FROM finance.monetary_commands WHERE id=%s",
                (command_id,),
            )
            assert cur.fetchone()==("succeeded","payment:pay_124","provider_timeout",True)


def test_concurrent_same_key_same_payload_has_exactly_one_insert():
    key="doers:pay:concurrent_same:1"
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows=list(pool.map(
            lambda _: _reserve_committed(
                key=key,request_hash="1"*64,business_ref="concurrent_same"
            ),
            range(8),
        ))
    assert len({row[0] for row in rows})==1
    assert sum(1 for row in rows if row[4] is True)==1
    assert sum(1 for row in rows if row[5] is True)==7


def test_crash_before_commit_rolls_back_and_after_commit_replays():
    before_key="doers:pay:crash_before:1"
    conn=psycopg.connect(URL)
    before=_reserve(conn,key=before_key,request_hash="2"*64,business_ref="crash_before")
    before_id=before[0]
    conn.close()  # uncommitted transaction is rolled back

    retry=_reserve_committed(key=before_key,request_hash="2"*64,business_ref="crash_before")
    assert retry[4:] == (True,False)
    assert retry[0] != before_id

    after_key="doers:pay:crash_after:1"
    conn=psycopg.connect(URL)
    after=_reserve(conn,key=after_key,request_hash="3"*64,business_ref="crash_after")
    after_id=after[0]
    conn.commit()
    conn.close()  # caller can die after durable commit and before consuming response

    replay=_reserve_committed(key=after_key,request_hash="3"*64,business_ref="crash_after")
    assert replay[0]==after_id
    assert replay[4:] == (False,True)


def test_runtime_has_no_direct_table_authority():
    with psycopg.connect(URL) as conn:
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("SELECT count(*) FROM finance.monetary_commands")
        conn.rollback()


def test_admin_sees_persistent_command_evidence():
    with psycopg.connect(ADMIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM finance.monetary_commands")
            assert cur.fetchone()[0] >= 1
