from __future__ import annotations

import os
import uuid
from decimal import Decimal

import psycopg
import pytest
from psycopg.errors import CheckViolation


ADMIN_URL = os.environ.get("PAY10_ADMIN_DATABASE_URL")
REFUND_URL = os.environ.get("PAY10_REFUND_DATABASE_URL")
RECON_URL = os.environ.get("PAY10_RECON_DATABASE_URL")
APP_URL = os.environ.get("PAY10_APP_DATABASE_URL")
WORKER_URL = os.environ.get("PAY10_WORKER_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not all((ADMIN_URL, REFUND_URL, RECON_URL, APP_URL, WORKER_URL)),
    reason="PAY-10-D isolated PG16 harness is not configured",
)


ORG_ID = uuid.UUID("d1000000-0000-4000-8000-000000000001")
ENTITY_ID = uuid.UUID("d1000000-0000-4000-8000-000000000002")
GST_ID = uuid.UUID("d1000000-0000-4000-8000-000000000003")
DIVISION_ID = uuid.UUID("d1000000-0000-4000-8000-000000000004")
BRAND_ID = uuid.UUID("d1000000-0000-4000-8000-000000000005")
BILLING_ID = uuid.UUID("d1000000-0000-4000-8000-000000000006")
INVOICE_ID = uuid.UUID("d1000000-0000-4000-8000-000000000007")
PAYMENT_ID = uuid.UUID("d1000000-0000-4000-8000-000000000008")
ALLOCATION_ID = uuid.UUID("d1000000-0000-4000-8000-000000000009")
REFUND_ID = uuid.UUID("d1000000-0000-4000-8000-000000000010")
COMMAND_ID = uuid.UUID("d1000000-0000-4000-8000-000000000011")
WORKER_ID = uuid.UUID("d1000000-0000-4000-8000-000000000012")
CREDIT_NOTE_ID = uuid.UUID("d1000000-0000-4000-8000-000000000013")
CREDIT_LEDGER_ID = uuid.UUID("d1000000-0000-4000-8000-000000000014")
AR_ACCOUNT_ID = uuid.UUID("d1000000-0000-4000-8000-000000000015")
CLEARING_ACCOUNT_ID = uuid.UUID("d1000000-0000-4000-8000-000000000016")
REVENUE_ACCOUNT_ID = uuid.UUID("d1000000-0000-4000-8000-000000000017")

PROVIDER_CODE = "razorpay_sandbox"
PROVIDER_PAYMENT_REF = "pay_PAY10DProvider01"
PROVIDER_REFUND_REF = "rfnd_PAY10DProvider01"
REQUEST_HASH = "a" * 64
EVIDENCE_HASH = "b" * 64

LINK = "app_secure.link_pay10_refund_credit_note(uuid,uuid)"
FINALIZE = "app_secure.finalize_pay10_refund(uuid)"


def _connect(url: str | None, *, autocommit: bool = False):
    assert url
    return psycopg.connect(url, autocommit=autocommit)


def _admin_scalar(sql: str, params=()):
    with _connect(ADMIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            assert row is not None
            return row[0]


def _admin_row(sql: str, params=()):
    with _connect(ADMIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            assert row is not None
            return row


def _reset_state(refund_amount: Decimal = Decimal("25.00")) -> None:
    with _connect(ADMIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE finance.refund_provider_evidence")
            cur.execute(
                "DELETE FROM finance.outbox_events "
                "WHERE idempotency_key LIKE %s",
                (f"pay10:{COMMAND_ID}:%",),
            )
            cur.execute(
                "DELETE FROM finance.refund_credit_note_links "
                "WHERE refund_id=%s OR credit_note_id=%s",
                (REFUND_ID, CREDIT_NOTE_ID),
            )
            cur.execute(
                """
                DELETE FROM finance.ledger_entry_lines
                WHERE ledger_entry_id IN (
                    SELECT id
                    FROM finance.ledger_entries
                    WHERE (source_type='refund' AND source_id=%s)
                       OR (source_type='credit_note' AND source_id=%s)
                )
                """,
                (REFUND_ID, CREDIT_NOTE_ID),
            )
            cur.execute(
                """
                DELETE FROM finance.ledger_entries
                WHERE (source_type='refund' AND source_id=%s)
                   OR (source_type='credit_note' AND source_id=%s)
                """,
                (REFUND_ID, CREDIT_NOTE_ID),
            )
            cur.execute(
                "DELETE FROM finance.credit_note_lines "
                "WHERE credit_note_id=%s",
                (CREDIT_NOTE_ID,),
            )
            cur.execute(
                "DELETE FROM finance.credit_notes WHERE id=%s",
                (CREDIT_NOTE_ID,),
            )
            cur.execute(
                "DELETE FROM finance.refund_execution_commands WHERE command_id=%s",
                (COMMAND_ID,),
            )
            cur.execute(
                "DELETE FROM finance.refunds WHERE id=%s",
                (REFUND_ID,),
            )
            cur.execute(
                "DELETE FROM finance.payment_allocations WHERE id=%s",
                (ALLOCATION_ID,),
            )
            cur.execute(
                "DELETE FROM finance.payments WHERE id=%s",
                (PAYMENT_ID,),
            )
            cur.execute(
                "DELETE FROM finance.invoices WHERE id=%s",
                (INVOICE_ID,),
            )
            cur.execute(
                "DELETE FROM finance.billing_parties WHERE id=%s",
                (BILLING_ID,),
            )
            cur.execute(
                "DELETE FROM finance.ledger_accounts WHERE id IN (%s,%s,%s)",
                (AR_ACCOUNT_ID, CLEARING_ACCOUNT_ID, REVENUE_ACCOUNT_ID),
            )
            cur.execute("DELETE FROM finance.brands WHERE id=%s", (BRAND_ID,))
            cur.execute(
                "DELETE FROM finance.divisions WHERE id=%s",
                (DIVISION_ID,),
            )
            cur.execute(
                "DELETE FROM finance.gst_registrations WHERE id=%s",
                (GST_ID,),
            )
            cur.execute(
                "DELETE FROM finance.legal_entities WHERE id=%s",
                (ENTITY_ID,),
            )
            cur.execute(
                "DELETE FROM public.organizations WHERE id=%s",
                (ORG_ID,),
            )

            cur.execute(
                """
                INSERT INTO public.organizations(
                    id,name,slug,tier,is_active,max_branches,
                    default_currency_code
                )
                VALUES(
                    %s,'PAY10D Runtime Org','pay10d-runtime-org',
                    'basic',true,10,'INR'
                )
                """,
                (ORG_ID,),
            )
            cur.execute(
                """
                INSERT INTO finance.legal_entities(
                    id,code,legal_name,registered_address,status
                )
                VALUES(
                    %s,'PAY10D_ENTITY','PAY10D Runtime Entity',
                    'PAY10D Runtime Address','active'
                )
                """,
                (ENTITY_ID,),
            )
            cur.execute(
                """
                INSERT INTO finance.gst_registrations(
                    id,legal_entity_id,gstin,state_code,state_name,
                    registered_address,status
                )
                VALUES(
                    %s,%s,'33ABCDE1234F1Z5','33','Tamil Nadu',
                    'PAY10D Runtime Address','active'
                )
                """,
                (GST_ID, ENTITY_ID),
            )
            cur.execute(
                """
                INSERT INTO finance.divisions(
                    id,legal_entity_id,code,name,status
                )
                VALUES(%s,%s,'VD','PAY10D Division','active')
                """,
                (DIVISION_ID, ENTITY_ID),
            )
            cur.execute(
                """
                INSERT INTO finance.brands(
                    id,legal_entity_id,division_id,code,name,status
                )
                VALUES(%s,%s,%s,'DD','PAY10D Brand','active')
                """,
                (BRAND_ID, ENTITY_ID, DIVISION_ID),
            )
            cur.execute(
                """
                INSERT INTO finance.ledger_accounts(
                    id,legal_entity_id,code,name,account_type,status
                )
                VALUES
                    (%s,%s,'AR','Accounts Receivable','asset','active'),
                    (%s,%s,'PAYMENT_CLEARING','Payment Clearing','asset','active'),
                    (%s,%s,'REVENUE','Revenue','revenue','active')
                """,
                (
                    AR_ACCOUNT_ID,
                    ENTITY_ID,
                    CLEARING_ACCOUNT_ID,
                    ENTITY_ID,
                    REVENUE_ACCOUNT_ID,
                    ENTITY_ID,
                ),
            )
            cur.execute(
                """
                INSERT INTO finance.billing_parties(
                    id,organization_id,buyer_kind,billing_name,party_type,
                    gst_treatment,billing_address,
                    place_of_supply_state_code,status
                )
                VALUES(
                    %s,%s,'organization','PAY10D Buyer','business',
                    'b2c','PAY10D Buyer Address','33','active'
                )
                """,
                (BILLING_ID, ORG_ID),
            )
            cur.execute(
                """
                INSERT INTO finance.invoices(
                    id,organization_id,billing_party_id,legal_entity_id,
                    gst_registration_id,division_id,brand_id,financial_year,
                    status,currency_code,seller_legal_name,seller_gstin,
                    seller_registered_address,seller_state_code,
                    buyer_billing_name,buyer_address,
                    buyer_place_of_supply_state_code,buyer_gst_treatment,
                    gst_supply_type,subtotal_amount,taxable_amount,
                    total_tax_amount,grand_total_amount
                )
                VALUES(
                    %s,%s,%s,%s,%s,%s,%s,'2026',
                    'paid','INR','PAY10D Seller','33ABCDE1234F1Z5',
                    'PAY10D Seller Address','33',
                    'PAY10D Buyer','PAY10D Buyer Address',
                    '33','b2c','intra_state',100,100,0,100
                )
                """,
                (
                    INVOICE_ID,
                    ORG_ID,
                    BILLING_ID,
                    ENTITY_ID,
                    GST_ID,
                    DIVISION_ID,
                    BRAND_ID,
                ),
            )
            cur.execute(
                """
                INSERT INTO finance.payments(
                    id,organization_id,legal_entity_id,gst_registration_id,
                    division_id,brand_id,provider_code,
                    provider_payment_ref,amount,currency_code,status
                )
                VALUES(
                    %s,%s,%s,%s,%s,%s,%s,%s,100,'INR','captured'
                )
                """,
                (
                    PAYMENT_ID,
                    ORG_ID,
                    ENTITY_ID,
                    GST_ID,
                    DIVISION_ID,
                    BRAND_ID,
                    PROVIDER_CODE,
                    PROVIDER_PAYMENT_REF,
                ),
            )
            cur.execute(
                """
                INSERT INTO finance.payment_allocations(
                    id,payment_id,invoice_id,allocated_amount
                )
                VALUES(%s,%s,%s,100)
                """,
                (ALLOCATION_ID, PAYMENT_ID, INVOICE_ID),
            )
            cur.execute(
                """
                INSERT INTO finance.refunds(
                    id,organization_id,payment_id,legal_entity_id,
                    division_id,brand_id,amount,currency_code,status,
                    reason_code
                )
                VALUES(
                    %s,%s,%s,%s,%s,%s,%s,'INR','approved','pay10d'
                )
                """,
                (
                    REFUND_ID,
                    ORG_ID,
                    PAYMENT_ID,
                    ENTITY_ID,
                    DIVISION_ID,
                    BRAND_ID,
                    refund_amount,
                ),
            )
            cur.execute(
                """
                INSERT INTO finance.refund_execution_commands(
                    command_id,refund_id,payment_id,organization_id,
                    legal_entity_id,division_id,brand_id,source_type,
                    source_id,logical_obligation_key,amount,currency_code,
                    status
                )
                VALUES(
                    %s,%s,%s,%s,%s,%s,%s,'branch.refund_required',
                    %s,%s,%s,'INR','pending'
                )
                """,
                (
                    COMMAND_ID,
                    REFUND_ID,
                    PAYMENT_ID,
                    ORG_ID,
                    ENTITY_ID,
                    DIVISION_ID,
                    BRAND_ID,
                    uuid.UUID("d1000000-0000-4000-8000-000000000018"),
                    f"finance-refund/{REFUND_ID}",
                    refund_amount,
                ),
            )
            cur.execute(
                """
                INSERT INTO finance.credit_notes(
                    id,organization_id,invoice_id,legal_entity_id,
                    gst_registration_id,division_id,brand_id,financial_year,
                    credit_note_number,status,total_amount,issued_at
                )
                VALUES(
                    %s,%s,%s,%s,%s,%s,%s,'2026',
                    'CN-PAY10D-001','issued',%s,clock_timestamp()
                )
                """,
                (
                    CREDIT_NOTE_ID,
                    ORG_ID,
                    INVOICE_ID,
                    ENTITY_ID,
                    GST_ID,
                    DIVISION_ID,
                    BRAND_ID,
                    refund_amount,
                ),
            )
            cur.execute(
                """
                INSERT INTO finance.credit_note_lines(
                    credit_note_id,description,amount
                )
                VALUES(%s,'PAY-10-D refund backing',%s)
                """,
                (CREDIT_NOTE_ID, refund_amount),
            )
            cur.execute(
                """
                INSERT INTO finance.ledger_entries(
                    id,legal_entity_id,division_id,brand_id,
                    entry_type,source_type,source_id,status,posted_at
                )
                VALUES(
                    %s,%s,%s,%s,'credit_note','credit_note',
                    %s,'posted',clock_timestamp()
                )
                """,
                (
                    CREDIT_LEDGER_ID,
                    ENTITY_ID,
                    DIVISION_ID,
                    BRAND_ID,
                    CREDIT_NOTE_ID,
                ),
            )
            cur.execute(
                """
                INSERT INTO finance.ledger_entry_lines(
                    ledger_entry_id,ledger_account_id,
                    debit_amount,credit_amount,memo
                )
                VALUES
                    (%s,%s,%s,0,'Credit note revenue reversal'),
                    (%s,%s,0,%s,'Credit note receivable reduction')
                """,
                (
                    CREDIT_LEDGER_ID,
                    REVENUE_ACCOUNT_ID,
                    refund_amount,
                    CREDIT_LEDGER_ID,
                    AR_ACCOUNT_ID,
                    refund_amount,
                ),
            )
        conn.commit()


@pytest.fixture(autouse=True)
def fresh_pay10d_state():
    _reset_state()


def _prepare_processed() -> None:
    with _connect(REFUND_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM app_secure.claim_pay10_refund_provider_execution(%s,1)
                """,
                (WORKER_ID,),
            )
            claim = cur.fetchone()
            assert claim is not None
            fence = claim[9]
        conn.commit()

    with _connect(REFUND_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM app_secure.bind_pay10_refund_provider_request(
                    %s,%s,%s,%s,%s,%s,'INR',%s
                )
                """,
                (
                    COMMAND_ID,
                    WORKER_ID,
                    fence,
                    PROVIDER_CODE,
                    PROVIDER_PAYMENT_REF,
                    _admin_scalar(
                        "SELECT amount FROM finance.refunds WHERE id=%s",
                        (REFUND_ID,),
                    ),
                    REQUEST_HASH,
                ),
            )
            cur.fetchone()
        conn.commit()

    with _connect(REFUND_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM app_secure.record_pay10_refund_provider_outcome(
                    %s,%s,%s,%s,'processed',%s,NULL
                )
                """,
                (
                    COMMAND_ID,
                    WORKER_ID,
                    fence,
                    PROVIDER_REFUND_REF,
                    EVIDENCE_HASH,
                ),
            )
            outcome = cur.fetchone()
            assert outcome is not None
            assert outcome[2] == "processed"
            assert outcome[3] == "reconciliation_pending"
        conn.commit()


def _link():
    with _connect(REFUND_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM app_secure.link_pay10_refund_credit_note(%s,%s)
                """,
                (REFUND_ID, CREDIT_NOTE_ID),
            )
            row = cur.fetchone()
        conn.commit()
        return row


def _finalize():
    with _connect(REFUND_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM app_secure.finalize_pay10_refund(%s)",
                (COMMAND_ID,),
            )
            row = cur.fetchone()
        conn.commit()
        return row


def test_pay10d_exact_execute_acl_and_direct_table_blindness():
    identities = {
        "refund": REFUND_URL,
        "recon": RECON_URL,
        "app": APP_URL,
        "worker": WORKER_URL,
    }
    relations = (
        "finance.refund_credit_note_links",
        "finance.credit_notes",
        "finance.ledger_entries",
        "finance.ledger_entry_lines",
        "finance.outbox_events",
    )
    with _connect(ADMIN_URL, autocommit=True) as conn:
        with conn.cursor() as cur:
            relation_oids = {}
            for relation in relations:
                cur.execute("SELECT %s::regclass::oid", (relation,))
                relation_oids[relation] = cur.fetchone()[0]

    for label, url in identities.items():
        with _connect(url, autocommit=True) as conn:
            with conn.cursor() as cur:
                for signature in (LINK, FINALIZE):
                    cur.execute(
                        """
                        SELECT pg_catalog.has_function_privilege(
                            current_user,%s,'EXECUTE'
                        )
                        """,
                        (signature,),
                    )
                    assert cur.fetchone()[0] is (label == "refund")
                for oid in relation_oids.values():
                    for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                        cur.execute(
                            """
                            SELECT pg_catalog.has_table_privilege(
                                current_user,%s::oid,%s
                            )
                            """,
                            (oid, privilege),
                        )
                        assert cur.fetchone()[0] is False


def test_pay10d_finalization_requires_exact_issued_credit_note_backing():
    _prepare_processed()

    with _connect(REFUND_URL) as conn:
        with pytest.raises(CheckViolation):
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM app_secure.finalize_pay10_refund(%s)",
                    (COMMAND_ID,),
                )
        conn.rollback()

    linked = _link()
    assert linked is not None
    assert linked[1:5] == (
        REFUND_ID,
        CREDIT_NOTE_ID,
        Decimal("25.00"),
        Decimal("25.00"),
    )
    assert linked[-1] is False

    replay = _link()
    assert replay is not None
    assert replay[0] == linked[0]
    assert replay[-1] is True


@pytest.mark.parametrize(
    ("refund_amount", "expected_payment_status"),
    [
        (Decimal("25.00"), "partially_refunded"),
        (Decimal("100.00"), "refunded"),
    ],
)
def test_pay10d_atomic_finalization_and_replay(
    refund_amount: Decimal,
    expected_payment_status: str,
):
    _reset_state(refund_amount)
    _prepare_processed()
    _link()

    before_credit_ledgers = _admin_scalar(
        """
        SELECT count(*)
        FROM finance.ledger_entries
        WHERE source_type='credit_note'
          AND source_id=%s
        """,
        (CREDIT_NOTE_ID,),
    )

    first = _finalize()
    assert first is not None
    assert first[:4] == (
        COMMAND_ID,
        REFUND_ID,
        PAYMENT_ID,
        first[3],
    )
    assert first[4:] == (
        "succeeded",
        expected_payment_status,
        "succeeded",
        False,
    )
    ledger_id = first[3]

    assert _admin_row(
        """
        SELECT
            (SELECT status FROM finance.refunds WHERE id=%s),
            (SELECT status FROM finance.payments WHERE id=%s),
            (
                SELECT status
                FROM finance.refund_execution_commands
                WHERE command_id=%s
            ),
            (
                SELECT completed_at IS NOT NULL
                FROM finance.refund_execution_commands
                WHERE command_id=%s
            )
        """,
        (REFUND_ID, PAYMENT_ID, COMMAND_ID, COMMAND_ID),
    ) == (
        "succeeded",
        expected_payment_status,
        "succeeded",
        True,
    )

    assert _admin_row(
        """
        SELECT
            count(*),
            coalesce(sum(ll.debit_amount),0),
            coalesce(sum(ll.credit_amount),0),
            count(*) FILTER (
                WHERE la.code='AR'
                  AND ll.debit_amount=%s
                  AND ll.credit_amount=0
            ),
            count(*) FILTER (
                WHERE la.code='PAYMENT_CLEARING'
                  AND ll.debit_amount=0
                  AND ll.credit_amount=%s
            )
        FROM finance.ledger_entry_lines ll
        JOIN finance.ledger_accounts la
          ON la.id=ll.ledger_account_id
        WHERE ll.ledger_entry_id=%s
        """,
        (refund_amount, refund_amount, ledger_id),
    ) == (
        2,
        refund_amount,
        refund_amount,
        1,
        1,
    )

    assert _admin_scalar(
        """
        SELECT count(*)
        FROM finance.outbox_events
        WHERE idempotency_key LIKE %s
        """,
        (f"pay10:{COMMAND_ID}:%",),
    ) == 3
    assert _admin_scalar(
        """
        SELECT count(*)
        FROM finance.ledger_entries
        WHERE source_type='credit_note'
          AND source_id=%s
        """,
        (CREDIT_NOTE_ID,),
    ) == before_credit_ledgers

    second = _finalize()
    assert second is not None
    assert second[3] == ledger_id
    assert second[-1] is True
    assert _admin_scalar(
        """
        SELECT count(*)
        FROM finance.ledger_entries
        WHERE source_type='refund'
          AND source_id=%s
          AND status='posted'
        """,
        (REFUND_ID,),
    ) == 1
    assert _admin_scalar(
        """
        SELECT count(*)
        FROM finance.outbox_events
        WHERE idempotency_key LIKE %s
        """,
        (f"pay10:{COMMAND_ID}:%",),
    ) == 3


def test_pay10d_rejects_credit_note_without_posted_accounting_reversal():
    _prepare_processed()
    with _connect(ADMIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM finance.ledger_entry_lines "
                "WHERE ledger_entry_id=%s",
                (CREDIT_LEDGER_ID,),
            )
            cur.execute(
                "DELETE FROM finance.ledger_entries WHERE id=%s",
                (CREDIT_LEDGER_ID,),
            )
        conn.commit()

    with _connect(REFUND_URL) as conn:
        with pytest.raises(CheckViolation):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT *
                    FROM app_secure.link_pay10_refund_credit_note(%s,%s)
                    """,
                    (REFUND_ID, CREDIT_NOTE_ID),
                )
        conn.rollback()


def test_pay10d_processed_evidence_alone_cannot_double_reverse_revenue():
    _prepare_processed()
    _link()
    _finalize()

    assert _admin_scalar(
        """
        SELECT count(*)
        FROM finance.ledger_entry_lines ll
        JOIN finance.ledger_accounts la
          ON la.id=ll.ledger_account_id
        JOIN finance.ledger_entries le
          ON le.id=ll.ledger_entry_id
        WHERE le.source_type='refund'
          AND le.source_id=%s
          AND la.code='REVENUE'
        """,
        (REFUND_ID,),
    ) == 0
