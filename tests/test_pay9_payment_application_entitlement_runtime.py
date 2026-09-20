from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import os
import uuid

import psycopg
import pytest

from tests import test_pay4_member_finance_binding_runtime as pay4


APP_URL = os.environ.get("PAY9_APP_DATABASE_URL")
PAYMENT_URL = os.environ.get("PAY9_PAYMENT_DATABASE_URL")
WORKER_URL = os.environ.get("PAY9_WORKER_DATABASE_URL")
ADMIN_URL = os.environ.get("PAY9_ADMIN_DATABASE_URL")
MIGRATION_URL = os.environ.get("PAY9_MIGRATION_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not (
        APP_URL
        and PAYMENT_URL
        and WORKER_URL
        and ADMIN_URL
        and MIGRATION_URL
        and pay4.APP_URL
        and pay4.WORKER_URL
        and pay4.MIGRATION_URL
        and pay4.ADMIN_URL
    ),
    reason="PAY-9 isolated PG16 harness is not configured",
)


PROVIDER_PAYMENT = uuid.UUID("99000000-0000-4000-8000-000000000071")
OFFLINE_PAYMENT = uuid.UUID("99000000-0000-4000-8000-000000000072")
EVENT_A = uuid.UUID("99000000-0000-4000-8000-000000000081")
EVENT_B = uuid.UUID("99000000-0000-4000-8000-000000000082")
HASH = "9" * 64


def _admin_execute(sql: str, params=()) -> None:
    assert ADMIN_URL
    with psycopg.connect(ADMIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()


def _admin_scalar(sql: str, params=()):
    assert ADMIN_URL
    with psycopg.connect(ADMIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()[0]


def _cleanup() -> None:
    if ADMIN_URL:
        with psycopg.connect(ADMIN_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "TRUNCATE TABLE finance.payment_application_records"
                )
                cur.execute(
                    "DELETE FROM finance.member_subscription_checkout_bindings "
                    "WHERE organization_id=%s",
                    (pay4.ORG,),
                )
                cur.execute(
                    """
                    DELETE FROM finance.ledger_entry_lines
                    WHERE ledger_entry_id IN (
                        SELECT e.id
                        FROM finance.ledger_entries e
                        WHERE e.source_type='payment_allocation'
                          AND e.source_id IN (
                              SELECT a.id
                              FROM finance.payment_allocations a
                              WHERE a.invoice_id=%s
                          )
                    )
                    """,
                    (pay4.INVOICE,),
                )
                cur.execute(
                    """
                    DELETE FROM finance.ledger_entries
                    WHERE source_type='payment_allocation'
                      AND source_id IN (
                          SELECT a.id
                          FROM finance.payment_allocations a
                          WHERE a.invoice_id=%s
                      )
                    """,
                    (pay4.INVOICE,),
                )
                cur.execute(
                    "DELETE FROM finance.outbox_events "
                    "WHERE organization_id=%s",
                    (pay4.ORG,),
                )
                cur.execute(
                    "DELETE FROM finance.payment_allocations "
                    "WHERE invoice_id=%s",
                    (pay4.INVOICE,),
                )
                # payment_events is PAY-2 immutable history: row DELETE is
                # intentionally rejected even to test admin. This disposable
                # PAY-9 database uses a table TRUNCATE between test cases.
                cur.execute("TRUNCATE TABLE finance.payment_events")
                cur.execute(
                    "DELETE FROM finance.payments WHERE id IN (%s,%s)",
                    (PROVIDER_PAYMENT, OFFLINE_PAYMENT),
                )
                cur.execute(
                    "DELETE FROM finance.ledger_accounts "
                    "WHERE legal_entity_id=%s "
                    "AND code IN ('PAYMENT_CLEARING','AR')",
                    (pay4.ENTITY,),
                )
            conn.commit()
    if pay4.MIGRATION_URL and pay4.ADMIN_URL:
        pay4._cleanup()


@pytest.fixture(autouse=True)
def _fresh():
    _cleanup()
    yield
    _cleanup()


def _seed(
    *,
    payment_amount: str = "100.00",
    payment_currency: str = "INR",
    with_checkout_binding: bool = True,
):
    term_id = pay4._seed_pending()
    pay4._seed_invoice_and_binding(term_id)

    with psycopg.connect(ADMIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO finance.ledger_accounts(
                    legal_entity_id,code,name,account_type,status
                ) VALUES
                    (%s,'PAYMENT_CLEARING','Payment clearing','asset','active'),
                    (%s,'AR','Accounts receivable','asset','active')
                """,
                (pay4.ENTITY, pay4.ENTITY),
            )
            cur.execute(
                """
                INSERT INTO finance.payments(
                    id,organization_id,legal_entity_id,gst_registration_id,
                    division_id,brand_id,provider_code,provider_payment_ref,
                    provider_order_ref,amount,currency_code,status,raw_status
                ) VALUES(
                    %s,%s,%s,%s,%s,%s,
                    'razorpay_sandbox','pay_pay9_provider','order_pay9_provider',
                    %s,%s,'captured','captured'
                )
                """,
                (
                    PROVIDER_PAYMENT,
                    pay4.ORG,
                    pay4.ENTITY,
                    pay4.GST,
                    pay4.DIVISION,
                    pay4.BRAND,
                    Decimal(payment_amount),
                    payment_currency,
                ),
            )
            cur.execute(
                """
                INSERT INTO finance.payment_events(
                    id,payment_id,provider_code,provider_event_id,
                    event_type,event_payload_sha256
                ) VALUES(
                    %s,%s,'razorpay_sandbox','evt_pay9_captured',
                    'payment.captured',%s
                )
                """,
                (EVENT_A, PROVIDER_PAYMENT, HASH),
            )
            if with_checkout_binding:
                cur.execute(
                    """
                    INSERT INTO finance.member_subscription_checkout_bindings(
                        id,organization_id,subscription_id,invoice_id,
                        checkout_intent_id,source_table
                    ) VALUES(
                        gen_random_uuid(),%s,%s,%s,%s,
                        'member_subscriptions_v2'
                    )
                    """,
                    (
                        pay4.ORG,
                        pay4.SUB,
                        pay4.INVOICE,
                        PROVIDER_PAYMENT,
                    ),
                )
        conn.commit()
    return term_id


def _apply(
    event_id: uuid.UUID = EVENT_A,
    *,
    payment_runtime: bool = True,
    org_context: uuid.UUID | None = None,
):
    url = PAYMENT_URL if payment_runtime else APP_URL
    assert url
    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            if org_context is not None:
                pay4._set_org(cur, org_context)
            cur.execute(
                """
                SELECT *
                FROM app_secure.apply_verified_provider_payment(%s,%s)
                """,
                (PROVIDER_PAYMENT, event_id),
            )
            row = cur.fetchone()
        conn.commit()
        return row


def _seed_prior_offline_allocation(amount: str = "40.00") -> None:
    with psycopg.connect(ADMIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO finance.payments(
                    id,organization_id,legal_entity_id,gst_registration_id,
                    division_id,brand_id,provider_code,provider_payment_ref,
                    amount,currency_code,status,raw_status
                ) VALUES(
                    %s,%s,%s,%s,%s,%s,'manual','pay9-offline',
                    %s,'INR','captured','verified'
                )
                """,
                (
                    OFFLINE_PAYMENT,
                    pay4.ORG,
                    pay4.ENTITY,
                    pay4.GST,
                    pay4.DIVISION,
                    pay4.BRAND,
                    Decimal(amount),
                ),
            )
            cur.execute(
                """
                INSERT INTO finance.payment_allocations(
                    payment_id,invoice_id,allocated_amount
                ) VALUES(%s,%s,%s)
                """,
                (OFFLINE_PAYMENT, pay4.INVOICE, Decimal(amount)),
            )
            cur.execute(
                "UPDATE finance.invoices SET status='partially_paid' "
                "WHERE id=%s",
                (pay4.INVOICE,),
            )
        conn.commit()


def _insert_second_provider_event() -> None:
    _admin_execute(
        """
        INSERT INTO finance.payment_events(
            id,payment_id,provider_code,provider_event_id,
            event_type,event_payload_sha256
        ) VALUES(
            %s,%s,'razorpay_sandbox','evt_pay9_order_paid',
            'order.paid',%s
        )
        """,
        (EVENT_B, PROVIDER_PAYMENT, "8" * 64),
    )


def _activate_from_paid_event(term_id: uuid.UUID):
    event_id = _admin_scalar(
        """
        SELECT id
        FROM finance.outbox_events
        WHERE organization_id=%s
          AND aggregate_type='invoice'
          AND aggregate_id=%s
          AND event_type='finance.invoice.paid'
        ORDER BY created_at,id
        LIMIT 1
        """,
        (pay4.ORG, pay4.INVOICE),
    )
    worker = uuid.UUID("99000000-0000-4000-8000-000000000091")
    assert WORKER_URL
    with psycopg.connect(WORKER_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM app_secure.claim_member_subscription_finance_events(
                    %s,10,60
                )
                """,
                (worker,),
            )
            claimed = cur.fetchall()
        conn.commit()
    row = next(item for item in claimed if item[0] == event_id)
    fence = int(row[5])

    with psycopg.connect(WORKER_URL) as conn:
        with conn.cursor() as cur:
            pay4._set_org(cur)
            cur.execute(
                """
                SELECT *
                FROM app_secure.consume_member_subscription_finance_event(
                    %s,%s,%s
                )
                """,
                (event_id, worker, fence),
            )
            consumed = cur.fetchone()
            cur.execute(
                """
                SELECT app_secure.acknowledge_member_subscription_finance_event(
                    %s,%s,%s
                )
                """,
                (event_id, worker, fence),
            )
            acknowledged = cur.fetchone()[0]
        conn.commit()

    assert consumed[0] == term_id
    assert acknowledged is True
    return consumed


def test_verified_capture_settles_invoice_then_pay5_activates_entitlement_exactly_once():
    term_id = _seed()
    assert pay4._status(term_id) == "pending_payment"

    row = _apply()
    assert row[4] == Decimal("100.00")
    assert row[5] == Decimal("0.00")
    assert row[6] == Decimal("0.00")
    assert row[7] == "paid"
    assert row[8] == "applied_paid"
    assert row[9] is False

    assert _admin_scalar(
        "SELECT status FROM finance.payments WHERE id=%s",
        (PROVIDER_PAYMENT,),
    ) == "captured"
    assert _admin_scalar(
        "SELECT status FROM finance.invoices WHERE id=%s",
        (pay4.INVOICE,),
    ) == "paid"
    assert _admin_scalar(
        "SELECT count(*) FROM finance.payment_allocations "
        "WHERE invoice_id=%s",
        (pay4.INVOICE,),
    ) == 1
    assert _admin_scalar(
        "SELECT count(*) FROM finance.ledger_entries "
        "WHERE source_type='payment_allocation'",
    ) == 1
    assert pay4._status(term_id) == "pending_payment"

    consumed = _activate_from_paid_event(term_id)
    assert consumed[2] is True
    assert pay4._status(term_id) == "active"


def test_underpayment_remains_partial_and_cannot_activate_entitlement():
    term_id = _seed(payment_amount="40.00")
    row = _apply()

    assert row[4] == Decimal("40.00")
    assert row[5] == Decimal("0.00")
    assert row[6] == Decimal("60.00")
    assert row[7] == "partially_paid"
    assert row[8] == "applied_partial"
    assert _admin_scalar(
        "SELECT count(*) FROM finance.outbox_events "
        "WHERE event_type='finance.invoice.paid'"
    ) == 0
    assert pay4._status(term_id) == "pending_payment"


def test_overpayment_pays_only_outstanding_and_preserves_unapplied_credit():
    _seed(payment_amount="150.00")
    row = _apply()

    assert row[4] == Decimal("100.00")
    assert row[5] == Decimal("50.00")
    assert row[6] == Decimal("0.00")
    assert row[8] == "applied_paid"
    assert _admin_scalar(
        """
        SELECT p.amount-COALESCE(sum(a.allocated_amount),0)
        FROM finance.payments p
        LEFT JOIN finance.payment_allocations a
          ON a.payment_id=p.id
        WHERE p.id=%s
        GROUP BY p.amount
        """,
        (PROVIDER_PAYMENT,),
    ) == Decimal("50.00")


def test_split_tender_multiple_payments_converge_to_paid_then_entitlement():
    term_id = _seed(payment_amount="60.00")
    _seed_prior_offline_allocation("40.00")

    row = _apply()
    assert row[4] == Decimal("60.00")
    assert row[6] == Decimal("0.00")
    assert row[8] == "applied_paid"
    assert _admin_scalar(
        "SELECT sum(allocated_amount) FROM finance.payment_allocations "
        "WHERE invoice_id=%s",
        (pay4.INVOICE,),
    ) == Decimal("100.00")

    _activate_from_paid_event(term_id)
    assert pay4._status(term_id) == "active"


def test_unbound_or_currency_mismatched_captured_money_stays_unapplied():
    term_id = _seed(with_checkout_binding=False)
    row = _apply()
    assert row[2] is None
    assert row[3] is None
    assert row[4] == Decimal("0.00")
    assert row[5] == Decimal("100.00")
    assert row[8] == "unapplied_no_checkout_binding"
    assert _admin_scalar(
        "SELECT count(*) FROM finance.payment_allocations"
    ) == 0
    assert pay4._status(term_id) == "pending_payment"

    _cleanup()
    term_id = _seed(payment_currency="USD")
    row = _apply()
    assert row[4] == Decimal("0.00")
    assert row[8] == "unapplied_currency_mismatch"
    assert _admin_scalar(
        "SELECT count(*) FROM finance.payment_allocations"
    ) == 0
    assert pay4._status(term_id) == "pending_payment"


def test_exact_replay_and_distinct_provider_replay_cannot_duplicate_financial_effects():
    _seed()

    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(lambda _: _apply(), range(2)))

    assert {row[0] for row in rows}.__len__() == 1
    assert sorted(bool(row[9]) for row in rows) == [False, True]
    assert _admin_scalar(
        "SELECT count(*) FROM finance.payment_application_records"
    ) == 1
    assert _admin_scalar(
        "SELECT count(*) FROM finance.payment_allocations"
    ) == 1
    assert _admin_scalar(
        "SELECT count(*) FROM finance.ledger_entries "
        "WHERE source_type='payment_allocation'"
    ) == 1

    _insert_second_provider_event()
    second = _apply(EVENT_B)
    assert second[8] == "replayed_existing_allocation"
    assert second[4] == Decimal("0.00")
    assert _admin_scalar(
        "SELECT count(*) FROM finance.payment_allocations"
    ) == 1
    assert _admin_scalar(
        "SELECT count(*) FROM finance.ledger_entries "
        "WHERE source_type='payment_allocation'"
    ) == 1


def test_cross_tenant_context_and_direct_table_access_are_denied():
    _seed()
    with pytest.raises(psycopg.Error):
        _apply(payment_runtime=False, org_context=pay4.OTHER_ORG)

    for url in (APP_URL, PAYMENT_URL, WORKER_URL):
        assert url
        with psycopg.connect(url) as conn:
            with conn.cursor() as cur:
                with pytest.raises(psycopg.Error):
                    cur.execute(
                        "SELECT count(*) "
                        "FROM finance.payment_application_records"
                    )
            conn.rollback()


def test_non_captured_provider_evidence_cannot_be_applied():
    _seed()
    _admin_execute(
        "UPDATE finance.payments SET status='authorized' WHERE id=%s",
        (PROVIDER_PAYMENT,),
    )
    with pytest.raises(psycopg.Error):
        _apply()
    assert _admin_scalar(
        "SELECT count(*) FROM finance.payment_application_records"
    ) == 0
    assert _admin_scalar(
        "SELECT count(*) FROM finance.payment_allocations"
    ) == 0
