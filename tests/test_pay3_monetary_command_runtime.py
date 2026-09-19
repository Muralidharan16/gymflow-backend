from __future__ import annotations

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


def _reserve(conn, *, key="doers:pay:sub_123:1", request_hash="a"*64, business_ref="sub_123"):
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('app.current_org_id',%s,false)",(str(ORG),))
        cur.execute(
            """
            SELECT command_id,status,response_ref,error_code,inserted,replayed
            FROM app_secure.reserve_finance_monetary_command(
                'finance.payment.capture',%s,%s,%s,%s,'system',%s
            )
            """,
            (key,request_hash,business_ref,CORR,"b"*64),
        )
        return cur.fetchone()


def test_same_payload_replays_and_different_payload_conflicts():
    with psycopg.connect(URL) as conn:
        first=_reserve(conn)
        replay=_reserve(conn)
        assert first[0]==replay[0]
        assert first[4:] == (True,False)
        assert replay[4:] == (False,True)
        with pytest.raises(psycopg.errors.UniqueViolation):
            _reserve(conn,request_hash="c"*64)
        conn.rollback()


def test_unknown_is_replayed_then_reconciled_to_success():
    with psycopg.connect(URL) as conn:
        row=_reserve(conn,key="doers:pay:sub_124:1",request_hash="d"*64,business_ref="sub_124")
        command_id=row[0]
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('app.current_org_id',%s,false)",(str(ORG),))
            cur.execute(
                "SELECT status,replayed FROM app_secure.mark_finance_monetary_command_unknown(%s,'provider_timeout')",
                (command_id,),
            )
            assert cur.fetchone()==("unknown",False)
        replay=_reserve(conn,key="doers:pay:sub_124:1",request_hash="d"*64,business_ref="sub_124")
        assert replay[1]=="unknown"
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status,response_ref,replayed FROM app_secure.complete_finance_monetary_command(%s,'payment:pay_124')",
                (command_id,),
            )
            assert cur.fetchone()==("succeeded","payment:pay_124",False)
            cur.execute(
                "SELECT status,response_ref,replayed FROM app_secure.complete_finance_monetary_command(%s,'payment:pay_124')",
                (command_id,),
            )
            assert cur.fetchone()==("succeeded","payment:pay_124",True)
        conn.commit()


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
