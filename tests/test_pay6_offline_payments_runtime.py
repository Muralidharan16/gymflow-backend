from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import os
import uuid

import psycopg
import pytest
from psycopg.errors import InsufficientPrivilege

from tests import test_pay4_member_finance_binding_runtime as pay4


APP_URL=os.environ.get("PAY6_APP_DATABASE_URL")
MIGRATION_URL=os.environ.get("PAY6_MIGRATION_DATABASE_URL")
ADMIN_URL=os.environ.get("PAY6_ADMIN_DATABASE_URL")

pytestmark=pytest.mark.skipif(
    not (
        APP_URL
        and MIGRATION_URL
        and ADMIN_URL
        and pay4.APP_URL
        and pay4.WORKER_URL
        and pay4.MIGRATION_URL
        and pay4.ADMIN_URL
    ),
    reason="PAY-6 isolated PG16 harness is not configured",
)

MAKER=uuid.UUID("66000000-0000-4000-8000-000000000001")
CHECKER_A=uuid.UUID("66000000-0000-4000-8000-000000000002")
CHECKER_B=uuid.UUID("66000000-0000-4000-8000-000000000003")
PROOF="6"*64


def _set_actor(
    cur,
    actor: uuid.UUID,
    *,
    org: uuid.UUID=pay4.ORG,
    role: str="owner",
    principal_type: str="owner",
) -> None:
    for name,value in (
        ("app.current_org_id",str(org)),
        ("app.current_user_id",str(actor)),
        ("app.current_user",str(actor)),
        ("app.current_principal_type",principal_type),
        ("app.current_role",role),
    ):
        cur.execute(
            "SELECT pg_catalog.set_config(%s,%s,true)",
            (name,value),
        )


def _cleanup_pay6() -> None:
    if not MIGRATION_URL:
        return
    with psycopg.connect(MIGRATION_URL) as conn:
        with conn.cursor() as cur:
            # PAY-6 tables and PAY-3 monetary commands use FORCE RLS. The
            # disposable migration-owner fixture opens a narrow cleanup window,
            # deletes only PAY-6 evidence, then restores FORCE RLS.
            for table in (
                "finance.offline_payment_events",
                "finance.offline_payment_requests",
                "finance.monetary_commands",
            ):
                cur.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
            try:
                cur.execute(
                    "DELETE FROM finance.offline_payment_events "
                    "WHERE organization_id=%s",
                    (pay4.ORG,),
                )
                cur.execute(
                    "DELETE FROM finance.offline_payment_requests "
                    "WHERE organization_id=%s",
                    (pay4.ORG,),
                )
                cur.execute(
                    """
                    DELETE FROM finance.monetary_commands
                    WHERE organization_id=%s
                      AND scope LIKE 'finance.offline_payment.%'
                    """,
                    (pay4.ORG,),
                )
            finally:
                for table in (
                    "finance.monetary_commands",
                    "finance.offline_payment_requests",
                    "finance.offline_payment_events",
                ):
                    cur.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

            # Remove PAY-6-created accounting/payment evidence before PAY-4
            # tears down the invoice/master-data fixture.
            cur.execute(
                """
                DELETE FROM finance.ledger_entry_lines l
                USING finance.ledger_entries e
                WHERE l.ledger_entry_id=e.id
                  AND e.legal_entity_id=%s
                """,
                (pay4.ENTITY,),
            )
            cur.execute(
                "DELETE FROM finance.ledger_entries WHERE legal_entity_id=%s",
                (pay4.ENTITY,),
            )
            cur.execute(
                """
                DELETE FROM finance.payment_events
                WHERE payment_id IN (
                    SELECT id FROM finance.payments
                    WHERE organization_id=%s AND provider_code='manual'
                )
                """,
                (pay4.ORG,),
            )
            cur.execute(
                """
                DELETE FROM finance.payment_allocations
                WHERE payment_id IN (
                    SELECT id FROM finance.payments
                    WHERE organization_id=%s AND provider_code='manual'
                )
                """,
                (pay4.ORG,),
            )
            cur.execute(
                "DELETE FROM finance.payments "
                "WHERE organization_id=%s AND provider_code='manual'",
                (pay4.ORG,),
            )
            cur.execute(
                "DELETE FROM finance.ledger_accounts WHERE legal_entity_id=%s",
                (pay4.ENTITY,),
            )
        conn.commit()
    pay4._cleanup()


@pytest.fixture(autouse=True)
def _fresh():
    _cleanup_pay6()
    yield
    _cleanup_pay6()


def _seed_invoice() -> uuid.UUID:
    term=pay4._seed_pending()
    pay4._seed_invoice_and_binding(term)
    with psycopg.connect(ADMIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO finance.ledger_accounts(
                    id,legal_entity_id,code,name,account_type,status
                ) VALUES
                    (gen_random_uuid(),%s,'AR','Accounts Receivable','asset','active'),
                    (gen_random_uuid(),%s,'PAYMENT_CLEARING','Payment Clearing','asset','active')
                """,
                (pay4.ENTITY,pay4.ENTITY),
            )
        conn.commit()
    return term


def _prepare(
    *,
    actor: uuid.UUID=MAKER,
    key: str="pay6:prepare:1",
    amount: str="100.00",
    method: str="bank_transfer",
    reference: str="UTR-PAY6-001",
    proof: str=PROOF,
    org: uuid.UUID=pay4.ORG,
):
    with psycopg.connect(APP_URL) as conn:
        with conn.cursor() as cur:
            _set_actor(cur,actor,org=org)
            cur.execute(
                """
                SELECT *
                FROM app_secure.prepare_offline_payment(
                    %s,%s,%s,%s,%s,%s,%s
                )
                """,
                (
                    pay4.INVOICE,method,Decimal(amount),"INR",
                    reference,proof,key,
                ),
            )
            row=cur.fetchone()
        conn.commit()
        return row


def _approve(
    request_id: uuid.UUID,
    *,
    actor: uuid.UUID=CHECKER_A,
    key: str="pay6:approve:1",
    org: uuid.UUID=pay4.ORG,
):
    with psycopg.connect(APP_URL) as conn:
        with conn.cursor() as cur:
            _set_actor(cur,actor,org=org)
            cur.execute(
                "SELECT * FROM app_secure.approve_offline_payment(%s,%s)",
                (request_id,key),
            )
            row=cur.fetchone()
        conn.commit()
        return row


def _reject(
    request_id: uuid.UUID,
    *,
    actor: uuid.UUID=CHECKER_A,
    key: str="pay6:reject:1",
    reason: str="proof_mismatch",
    org: uuid.UUID=pay4.ORG,
):
    with psycopg.connect(APP_URL) as conn:
        with conn.cursor() as cur:
            _set_actor(cur,actor,org=org)
            cur.execute(
                "SELECT * FROM app_secure.reject_offline_payment(%s,%s,%s)",
                (request_id,reason,key),
            )
            row=cur.fetchone()
        conn.commit()
        return row


def _count(sql: str, params=()) -> int:
    with psycopg.connect(ADMIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(sql,params)
            return int(cur.fetchone()[0])


def _invoice_status() -> str:
    with psycopg.connect(ADMIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status::text FROM finance.invoices WHERE id=%s",
                (pay4.INVOICE,),
            )
            return cur.fetchone()[0]


def test_prepare_requires_proof_and_is_actor_aware_idempotent():
    _seed_invoice()
    first=_prepare()
    replay=_prepare()
    assert first[0]==replay[0]
    assert first[2]=="prepared"
    assert first[3]==Decimal("100.00")
    assert first[4]=="INR"
    assert first[5]=="bank_transfer"
    assert first[7]==MAKER
    assert first[8] is False
    assert replay[8] is True

    with pytest.raises(psycopg.Error):
        _prepare(actor=CHECKER_A,key="pay6:prepare:1")

    with pytest.raises(psycopg.Error):
        _prepare(key="pay6:prepare:1",amount="90.00")

    assert _count(
        "SELECT count(*) FROM finance.offline_payment_requests WHERE organization_id=%s",
        (pay4.ORG,),
    )==1
    assert _count(
        "SELECT count(*) FROM finance.offline_payment_events "
        "WHERE organization_id=%s AND event_type='offline_payment.prepared'",
        (pay4.ORG,),
    )==1


def test_direct_app_runtime_table_dml_is_denied():
    _seed_invoice()
    _prepare()
    with psycopg.connect(APP_URL) as conn:
        with conn.cursor() as cur:
            _set_actor(cur,CHECKER_A)
            with pytest.raises(InsufficientPrivilege):
                cur.execute(
                    "SELECT count(*) FROM finance.offline_payment_requests"
                )
        conn.rollback()


def test_maker_cannot_approve_or_reject_own_request():
    _seed_invoice()
    request_id=_prepare()[0]
    with pytest.raises(psycopg.Error):
        _approve(request_id,actor=MAKER)
    with pytest.raises(psycopg.Error):
        _reject(request_id,actor=MAKER)
    assert _invoice_status()=="issued"
    assert _count(
        "SELECT count(*) FROM finance.payments "
        "WHERE organization_id=%s AND provider_code='manual'",
        (pay4.ORG,),
    )==0


def test_checker_approval_creates_canonical_payment_allocation_ledger_and_paid_event():
    _seed_invoice()
    request_id=_prepare()[0]
    approved=_approve(request_id)

    assert approved[0]==request_id
    payment_id=approved[1]
    allocation_id=approved[2]
    assert approved[3]==pay4.INVOICE
    assert approved[4]=="paid"
    assert approved[5]=="approved"
    assert approved[6] is False

    replay=_approve(request_id)
    assert replay[0:6]==approved[0:6]
    assert replay[6] is True

    with psycopg.connect(ADMIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT provider_code,status,raw_status,amount::text,
                       currency_code,provider_payment_ref
                FROM finance.payments WHERE id=%s
                """,
                (payment_id,),
            )
            payment=cur.fetchone()
            assert payment[0:5]==(
                "manual","captured","offline_approved","100.00","INR"
            )
            assert payment[5].startswith(
                f"manual/{pay4.ORG}/bank_transfer/"
            )
            cur.execute(
                """
                SELECT payment_id,invoice_id,allocated_amount::text
                FROM finance.payment_allocations WHERE id=%s
                """,
                (allocation_id,),
            )
            assert cur.fetchone()==(payment_id,pay4.INVOICE,"100.00")

    assert _invoice_status()=="paid"
    assert _count(
        "SELECT count(*) FROM finance.payment_events "
        "WHERE payment_id=%s AND provider_code='manual' "
        "AND event_type='manual.payment.approved'",
        (payment_id,),
    )==1
    assert _count(
        "SELECT count(*) FROM finance.ledger_entries "
        "WHERE source_type='payment_allocation' AND source_id=%s "
        "AND status='posted'",
        (allocation_id,),
    )==1
    assert _count(
        "SELECT count(*) FROM finance.outbox_events "
        "WHERE aggregate_type='invoice' AND aggregate_id=%s "
        "AND event_type='finance.invoice.paid'",
        (pay4.INVOICE,),
    )==1
    assert _count(
        "SELECT count(*) FROM finance.offline_payment_events "
        "WHERE offline_payment_request_id=%s",
        (request_id,),
    )==2
    assert _count(
        "SELECT count(*) FROM finance.monetary_commands "
        "WHERE organization_id=%s AND scope IN "
        "('finance.offline_payment.prepare','finance.offline_payment.approve') "
        "AND status='succeeded'",
        (pay4.ORG,),
    )==2


def test_terminal_approval_replay_is_fenced_to_original_checker():
    _seed_invoice()
    request_id=_prepare()[0]
    _approve(request_id,actor=CHECKER_A,key="pay6:approve:actor")
    with pytest.raises(psycopg.Error):
        _approve(
            request_id,
            actor=CHECKER_B,
            key="pay6:approve:actor",
        )
    assert _count(
        "SELECT count(*) FROM finance.payments "
        "WHERE organization_id=%s AND provider_code='manual'",
        (pay4.ORG,),
    )==1


def test_rejection_is_final_audited_and_creates_no_payment():
    _seed_invoice()
    request_id=_prepare(reference="UTR-PAY6-REJECT")[0]
    rejected=_reject(request_id)
    assert rejected==(request_id,"rejected","proof_mismatch",False)
    replay=_reject(request_id)
    assert replay==(request_id,"rejected","proof_mismatch",True)

    with pytest.raises(psycopg.Error):
        _reject(request_id,actor=CHECKER_B)
    with pytest.raises(psycopg.Error):
        _approve(request_id,key="pay6:approve:after-reject")

    assert _count(
        "SELECT count(*) FROM finance.payments "
        "WHERE organization_id=%s AND provider_code='manual'",
        (pay4.ORG,),
    )==0
    assert _count(
        "SELECT count(*) FROM finance.offline_payment_events "
        "WHERE offline_payment_request_id=%s",
        (request_id,),
    )==2


def test_partial_offline_payment_updates_invoice_but_does_not_emit_paid_event():
    _seed_invoice()
    request_id=_prepare(
        key="pay6:prepare:partial",
        amount="40.00",
        reference="CASH-RECEIPT-40",
        method="cash",
    )[0]
    approved=_approve(
        request_id,
        key="pay6:approve:partial",
    )
    assert approved[4]=="partially_paid"
    assert _invoice_status()=="partially_paid"
    assert _count(
        "SELECT count(*) FROM finance.outbox_events "
        "WHERE aggregate_type='invoice' AND aggregate_id=%s "
        "AND event_type='finance.invoice.paid'",
        (pay4.INVOICE,),
    )==0
    assert _count(
        "SELECT count(*) FROM finance.outbox_events "
        "WHERE aggregate_type='invoice' AND aggregate_id=%s "
        "AND event_type='finance.invoice.partially_paid'",
        (pay4.INVOICE,),
    )==1


def test_stale_prepared_request_cannot_overpay_after_other_payment_wins():
    _seed_invoice()
    first=_prepare(
        key="pay6:prepare:first",
        reference="UTR-FIRST",
    )[0]
    stale=_prepare(
        key="pay6:prepare:stale",
        reference="UTR-STALE",
    )[0]
    _approve(first,key="pay6:approve:first")
    with pytest.raises(psycopg.Error):
        _approve(stale,key="pay6:approve:stale")
    assert _invoice_status()=="paid"
    assert _count(
        "SELECT count(*) FROM finance.payments "
        "WHERE organization_id=%s AND provider_code='manual'",
        (pay4.ORG,),
    )==1


def test_two_checkers_racing_create_one_payment_effect():
    _seed_invoice()
    request_id=_prepare(
        key="pay6:prepare:race",
        reference="UTR-RACE",
    )[0]

    def attempt(args):
        actor,key=args
        try:
            return ("ok",_approve(request_id,actor=actor,key=key))
        except psycopg.Error as exc:
            return ("error",getattr(exc,"sqlstate",None))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(
            attempt,
            (
                (CHECKER_A,"pay6:approve:race:a"),
                (CHECKER_B,"pay6:approve:race:b"),
            ),
        ))
    assert sorted(x[0] for x in results)==["error","ok"]
    assert _count(
        "SELECT count(*) FROM finance.payments "
        "WHERE organization_id=%s AND provider_code='manual'",
        (pay4.ORG,),
    )==1
    assert _count(
        "SELECT count(*) FROM finance.payment_allocations "
        "WHERE invoice_id=%s",
        (pay4.INVOICE,),
    )==1


def test_cross_tenant_prepare_and_approval_fail_closed():
    _seed_invoice()
    with pytest.raises(psycopg.Error):
        _prepare(
            org=pay4.OTHER_ORG,
            key="pay6:prepare:cross",
            reference="UTR-CROSS",
        )

    request_id=_prepare(
        key="pay6:prepare:tenant",
        reference="UTR-TENANT",
    )[0]
    with pytest.raises(psycopg.Error):
        _approve(
            request_id,
            org=pay4.OTHER_ORG,
            key="pay6:approve:cross",
        )
    assert _invoice_status()=="issued"


def test_offline_audit_event_mutation_is_denied_even_through_security_owner_surface():
    _seed_invoice()
    request_id=_prepare(
        key="pay6:prepare:immutable",
        reference="UTR-IMMUTABLE",
    )[0]
    with psycopg.connect(APP_URL) as conn:
        with conn.cursor() as cur:
            _set_actor(cur,CHECKER_A)
            with pytest.raises(InsufficientPrivilege):
                cur.execute(
                    "UPDATE finance.offline_payment_events "
                    "SET proof_sha256=%s WHERE offline_payment_request_id=%s",
                    ("7"*64,request_id),
                )
        conn.rollback()
