from __future__ import annotations

import os
import uuid
from decimal import Decimal

import psycopg
import pytest
from psycopg.errors import (
    CheckViolation,
    InsufficientPrivilege,
    SerializationFailure,
    UniqueViolation,
)


ADMIN_URL = os.environ.get("PAY10_ADMIN_DATABASE_URL")
REFUND_URL = os.environ.get("PAY10_REFUND_DATABASE_URL")
RECON_URL = os.environ.get("PAY10_RECON_DATABASE_URL")
APP_URL = os.environ.get("PAY10_APP_DATABASE_URL")
WORKER_URL = os.environ.get("PAY10_WORKER_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not all((ADMIN_URL, REFUND_URL, RECON_URL, APP_URL, WORKER_URL)),
    reason="PAY-10-C isolated PG16 harness is not configured",
)


ORG_ID = uuid.UUID("c1000000-0000-4000-8000-000000000001")
ENTITY_ID = uuid.UUID("c1000000-0000-4000-8000-000000000002")
GST_ID = uuid.UUID("c1000000-0000-4000-8000-000000000003")
DIVISION_ID = uuid.UUID("c1000000-0000-4000-8000-000000000004")
BRAND_ID = uuid.UUID("c1000000-0000-4000-8000-000000000005")
BILLING_ID = uuid.UUID("c1000000-0000-4000-8000-000000000006")
INVOICE_ID = uuid.UUID("c1000000-0000-4000-8000-000000000007")
PAYMENT_ID = uuid.UUID("c1000000-0000-4000-8000-000000000008")
ALLOCATION_ID = uuid.UUID("c1000000-0000-4000-8000-000000000009")
REFUND_ID = uuid.UUID("c1000000-0000-4000-8000-000000000010")
COMMAND_ID = uuid.UUID("c1000000-0000-4000-8000-000000000011")
WORKER_A = uuid.UUID("c1000000-0000-4000-8000-000000000012")
WORKER_B = uuid.UUID("c1000000-0000-4000-8000-000000000013")

PROVIDER_CODE = "razorpay_sandbox"
PROVIDER_PAYMENT_REF = "pay_PAY10CProvider01"
PROVIDER_REFUND_REF = "rfnd_PAY10CProvider01"
REQUEST_HASH = "a" * 64
EVIDENCE_HASH = "b" * 64

CLAIM = "app_secure.claim_pay10_refund_provider_execution(uuid,integer)"
BIND = (
    "app_secure.bind_pay10_refund_provider_request("
    "uuid,uuid,bigint,text,text,numeric,text,text)"
)
OUTCOME = (
    "app_secure.record_pay10_refund_provider_outcome("
    "uuid,uuid,bigint,text,text,text,timestamp with time zone)"
)
UNKNOWN = (
    "app_secure.record_pay10_refund_provider_unknown("
    "uuid,uuid,bigint,text)"
)
FAILURE = (
    "app_secure.record_pay10_refund_provider_failure("
    "uuid,uuid,bigint,text,boolean)"
)
EXTERNAL = (
    "app_secure.record_pay10_refund_external_evidence("
    "uuid,text,text,text,text,text,text,timestamp with time zone)"
)


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


def _admin_execute(sql: str, params=()) -> None:
    with _connect(ADMIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()


def _reset_state() -> None:
    with _connect(ADMIN_URL) as conn:
        with conn.cursor() as cur:
            # Provider evidence is immutable to UPDATE/DELETE by design.
            # TRUNCATE does not fire the row mutation trigger and is safe only
            # in this disposable isolated CI database.
            cur.execute("TRUNCATE TABLE finance.refund_provider_evidence")
            cur.execute(
                "DELETE FROM finance.refund_execution_commands WHERE command_id=%s",
                (COMMAND_ID,),
            )
            cur.execute("DELETE FROM finance.refunds WHERE id=%s", (REFUND_ID,))
            cur.execute(
                "DELETE FROM finance.payment_allocations WHERE id=%s",
                (ALLOCATION_ID,),
            )
            cur.execute("DELETE FROM finance.payments WHERE id=%s", (PAYMENT_ID,))
            cur.execute("DELETE FROM finance.invoices WHERE id=%s", (INVOICE_ID,))
            cur.execute(
                "DELETE FROM finance.billing_parties WHERE id=%s",
                (BILLING_ID,),
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
            cur.execute("DELETE FROM public.organizations WHERE id=%s", (ORG_ID,))

            cur.execute(
                """
                INSERT INTO public.organizations(
                    id,name,slug,tier,is_active,max_branches,
                    default_currency_code
                )
                VALUES(
                    %s,'PAY10C Runtime Org','pay10c-runtime-org',
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
                    %s,'PAY10C_ENTITY','PAY10C Runtime Entity',
                    'PAY10C Runtime Address','active'
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
                    'PAY10C Runtime Address','active'
                )
                """,
                (GST_ID, ENTITY_ID),
            )
            cur.execute(
                """
                INSERT INTO finance.divisions(
                    id,legal_entity_id,code,name,status
                )
                VALUES(%s,%s,'VS','PAY10C Division','active')
                """,
                (DIVISION_ID, ENTITY_ID),
            )
            cur.execute(
                """
                INSERT INTO finance.brands(
                    id,legal_entity_id,division_id,code,name,status
                )
                VALUES(%s,%s,%s,'DS','PAY10C Brand','active')
                """,
                (BRAND_ID, ENTITY_ID, DIVISION_ID),
            )
            cur.execute(
                """
                INSERT INTO finance.billing_parties(
                    id,organization_id,buyer_kind,billing_name,party_type,
                    gst_treatment,billing_address,
                    place_of_supply_state_code,status
                )
                VALUES(
                    %s,%s,'organization','PAY10C Buyer','business',
                    'b2c','PAY10C Buyer Address','33','active'
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
                    'draft','INR','PAY10C Seller','33ABCDE1234F1Z5',
                    'PAY10C Seller Address','33',
                    'PAY10C Buyer','PAY10C Buyer Address',
                    '33','b2c','intra_state',100,100,0,100
                )
                """,
                (
                    INVOICE_ID, ORG_ID, BILLING_ID, ENTITY_ID, GST_ID,
                    DIVISION_ID, BRAND_ID,
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
                    PAYMENT_ID, ORG_ID, ENTITY_ID, GST_ID,
                    DIVISION_ID, BRAND_ID, PROVIDER_CODE,
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
                    %s,%s,%s,%s,%s,%s,25,'INR','approved','pay10c'
                )
                """,
                (
                    REFUND_ID, ORG_ID, PAYMENT_ID, ENTITY_ID,
                    DIVISION_ID, BRAND_ID,
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
                    %s,%s,25,'INR','pending'
                )
                """,
                (
                    COMMAND_ID, REFUND_ID, PAYMENT_ID, ORG_ID,
                    ENTITY_ID, DIVISION_ID, BRAND_ID,
                    uuid.UUID("c1000000-0000-4000-8000-000000000014"),
                    f"finance-refund/{REFUND_ID}",
                ),
            )
        conn.commit()


@pytest.fixture(autouse=True)
def fresh_pay10c_state():
    _reset_state()


def _claim(worker_id: uuid.UUID = WORKER_A):
    with _connect(REFUND_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM app_secure.claim_pay10_refund_provider_execution(%s,1)
                """,
                (worker_id,),
            )
            row = cur.fetchone()
        conn.commit()
        return row


def _bind(
    fence: int,
    *,
    worker_id: uuid.UUID = WORKER_A,
    request_hash: str = REQUEST_HASH,
    provider_code: str = PROVIDER_CODE,
    provider_payment_ref: str = PROVIDER_PAYMENT_REF,
    amount: Decimal = Decimal("25.00"),
    currency: str = "INR",
):
    with _connect(REFUND_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM app_secure.bind_pay10_refund_provider_request(
                    %s,%s,%s,%s,%s,%s,%s,%s
                )
                """,
                (
                    COMMAND_ID, worker_id, fence, provider_code,
                    provider_payment_ref, amount, currency, request_hash,
                ),
            )
            row = cur.fetchone()
        conn.commit()
        return row


def _unknown(fence: int, *, worker_id: uuid.UUID = WORKER_A):
    with _connect(REFUND_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT app_secure.record_pay10_refund_provider_unknown(
                    %s,%s,%s,'razorpay_timeout'
                )
                """,
                (COMMAND_ID, worker_id, fence),
            )
            row = cur.fetchone()
        conn.commit()
        return row[0]


def test_pay10c_exact_execute_acl_and_direct_table_blindness():
    expected = {
        CLAIM: {"refund"},
        BIND: {"refund"},
        OUTCOME: {"refund"},
        UNKNOWN: {"refund"},
        FAILURE: {"refund"},
        EXTERNAL: {"recon"},
    }
    identities = {
        "refund": REFUND_URL,
        "recon": RECON_URL,
        "app": APP_URL,
        "worker": WORKER_URL,
    }

    for label, url in identities.items():
        with _connect(url, autocommit=True) as conn:
            with conn.cursor() as cur:
                for signature, allowed in expected.items():
                    cur.execute(
                        """
                        SELECT pg_catalog.has_function_privilege(
                            current_user,%s,'EXECUTE'
                        )
                        """,
                        (signature,),
                    )
                    assert cur.fetchone()[0] is (label in allowed)

                for relation in (
                    "finance.refund_execution_commands",
                    "finance.refunds",
                    "finance.payments",
                    "finance.payment_allocations",
                    "finance.refund_provider_evidence",
                ):
                    for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                        cur.execute(
                            """
                            SELECT pg_catalog.has_table_privilege(
                                current_user,%s,%s
                            )
                            """,
                            (relation, privilege),
                        )
                        assert cur.fetchone()[0] is False


def test_pay10c_claim_derives_provider_authority_and_reclaims_expired_fence():
    first = _claim()
    assert first is not None
    assert first[:4] == (COMMAND_ID, REFUND_ID, PAYMENT_ID, ORG_ID)
    assert first[4:8] == (
        PROVIDER_CODE,
        PROVIDER_PAYMENT_REF,
        Decimal("25.00"),
        "INR",
    )
    assert first[8] == 1
    assert first[9] == 1
    assert first[10] is False

    _admin_execute(
        """
        UPDATE finance.refund_execution_commands
        SET leased_until=clock_timestamp()-interval '1 second'
        WHERE command_id=%s
        """,
        (COMMAND_ID,),
    )
    reclaimed = _claim(WORKER_B)
    assert reclaimed is not None
    assert reclaimed[8] == 1
    assert reclaimed[9] == 2
    assert reclaimed[10] is True


def test_pay10c_bind_is_fenced_server_authoritative_and_stable():
    claim = _claim()
    assert claim is not None
    fence = claim[9]

    bound = _bind(fence)
    assert bound[:8] == (
        COMMAND_ID,
        REFUND_ID,
        PAYMENT_ID,
        ORG_ID,
        PROVIDER_CODE,
        PROVIDER_PAYMENT_REF,
        Decimal("25.00"),
        "INR",
    )
    assert bound[8:] == (REQUEST_HASH, "processing")
    assert _admin_row(
        """
        SELECT request_sha256,provider_code,first_attempted_at,
               status,leased_by,lease_fence
        FROM finance.refund_execution_commands
        WHERE command_id=%s
        """,
        (COMMAND_ID,),
    )[:2] == (REQUEST_HASH, PROVIDER_CODE)
    assert _admin_scalar(
        "SELECT status FROM finance.refunds WHERE id=%s",
        (REFUND_ID,),
    ) == "processing"

    for kwargs in (
        {"provider_code": "other_provider"},
        {"provider_payment_ref": "pay_wrong"},
        {"amount": Decimal("24.99")},
        {"currency": "USD"},
    ):
        _reset_state()
        fresh = _claim()
        assert fresh is not None
        with pytest.raises(CheckViolation):
            _bind(fresh[9], **kwargs)


def test_pay10c_stale_fence_cannot_bind_or_record_outcome():
    claim = _claim()
    assert claim is not None
    fence = claim[9]
    with pytest.raises(SerializationFailure):
        _bind(fence + 1)

    _bind(fence)
    _admin_execute(
        """
        UPDATE finance.refund_execution_commands
        SET leased_until=clock_timestamp()-interval '1 second'
        WHERE command_id=%s
        """,
        (COMMAND_ID,),
    )
    with _connect(REFUND_URL) as conn:
        with pytest.raises(SerializationFailure):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT *
                    FROM app_secure.record_pay10_refund_provider_outcome(
                        %s,%s,%s,%s,'pending',%s,NULL
                    )
                    """,
                    (
                        COMMAND_ID, WORKER_A, fence,
                        PROVIDER_REFUND_REF, EVIDENCE_HASH,
                    ),
                )
        conn.rollback()


def test_pay10c_known_nonacceptance_retries_with_same_request_hash_only():
    claim = _claim()
    assert claim is not None
    fence = claim[9]
    _bind(fence)

    with _connect(REFUND_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT app_secure.record_pay10_refund_provider_failure(
                    %s,%s,%s,'connect_failed',false
                )
                """,
                (COMMAND_ID, WORKER_A, fence),
            )
            assert cur.fetchone()[0] == "retry_pending"
        conn.commit()

    _admin_execute(
        """
        UPDATE finance.refund_execution_commands
        SET process_after=clock_timestamp()-interval '1 second'
        WHERE command_id=%s
        """,
        (COMMAND_ID,),
    )
    second = _claim(WORKER_B)
    assert second is not None
    assert second[8] == 2

    rebound = _bind(second[9], worker_id=WORKER_B)
    assert rebound[8] == REQUEST_HASH

    _admin_execute(
        """
        UPDATE finance.refund_execution_commands
        SET leased_until=clock_timestamp()-interval '1 second'
        WHERE command_id=%s
        """,
        (COMMAND_ID,),
    )
    third = _claim(WORKER_A)
    assert third is not None
    with pytest.raises(CheckViolation):
        _bind(
            third[9],
            worker_id=WORKER_A,
            request_hash="c" * 64,
        )


def test_pay10c_unknown_outcome_enters_reconciliation_and_is_not_reclaimed():
    claim = _claim()
    assert claim is not None
    fence = claim[9]
    _bind(fence)

    assert _unknown(fence) == "reconciliation_pending"
    assert _claim(WORKER_B) is None
    assert _admin_row(
        """
        SELECT status,leased_by,leased_until,last_error_code
        FROM finance.refund_execution_commands
        WHERE command_id=%s
        """,
        (COMMAND_ID,),
    ) == ("reconciliation_pending", None, None, "razorpay_timeout")


def test_pay10c_submission_evidence_is_immutable_replay_safe_and_not_final():
    claim = _claim()
    assert claim is not None
    fence = claim[9]
    _bind(fence)

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
                    COMMAND_ID, WORKER_A, fence,
                    PROVIDER_REFUND_REF, EVIDENCE_HASH,
                ),
            )
            first = cur.fetchone()
        conn.commit()

    assert first is not None
    assert first[2:] == ("processed", "reconciliation_pending", False)
    assert _admin_scalar(
        "SELECT status FROM finance.refund_execution_commands WHERE command_id=%s",
        (COMMAND_ID,),
    ) == "reconciliation_pending"
    assert _admin_scalar(
        "SELECT status FROM finance.refunds WHERE id=%s",
        (REFUND_ID,),
    ) == "processing"
    assert _admin_scalar(
        "SELECT status FROM finance.payments WHERE id=%s",
        (PAYMENT_ID,),
    ) == "captured"

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
                    COMMAND_ID, WORKER_A, fence,
                    PROVIDER_REFUND_REF, EVIDENCE_HASH,
                ),
            )
            replay = cur.fetchone()
        conn.commit()
    assert replay is not None
    assert replay[0] == first[0]
    assert replay[-1] is True

    with _connect(REFUND_URL) as conn:
        with pytest.raises(UniqueViolation):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT *
                    FROM app_secure.record_pay10_refund_provider_outcome(
                        %s,%s,%s,%s,'pending',%s,NULL
                    )
                    """,
                    (
                        COMMAND_ID, WORKER_A, fence,
                        PROVIDER_REFUND_REF, EVIDENCE_HASH,
                    ),
                )
        conn.rollback()

    with _connect(ADMIN_URL) as conn:
        with pytest.raises(psycopg.Error):
            with conn.cursor() as cur:
                cur.execute("SET LOCAL ROLE app_security_owner")
                cur.execute(
                    """
                    UPDATE finance.refund_provider_evidence
                    SET normalized_status='failed'
                    WHERE id=%s
                    """,
                    (first[0],),
                )
        conn.rollback()


def test_pay10c_active_lease_blocks_external_reconciliation():
    claim = _claim()
    assert claim is not None
    fence = claim[9]
    _bind(fence)

    with _connect(RECON_URL) as conn:
        with pytest.raises(SerializationFailure):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT *
                    FROM app_secure.record_pay10_refund_external_evidence(
                        %s,%s,NULL,%s,'reconciliation','processed',%s,NULL
                    )
                    """,
                    (
                        COMMAND_ID, PROVIDER_PAYMENT_REF,
                        PROVIDER_REFUND_REF, "d" * 64,
                    ),
                )
        conn.rollback()


def test_pay10c_reconciliation_replay_and_out_of_order_evidence_do_not_regress():
    claim = _claim()
    assert claim is not None
    fence = claim[9]
    _bind(fence)
    _unknown(fence)

    processed_hash = "d" * 64
    pending_hash = "e" * 64
    failed_hash = "f" * 64
    event_id = "evt_pay10c_processed"

    with _connect(RECON_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM app_secure.record_pay10_refund_external_evidence(
                    %s,%s,%s,%s,'webhook','processed',%s,NULL
                )
                """,
                (
                    COMMAND_ID, PROVIDER_PAYMENT_REF, event_id,
                    PROVIDER_REFUND_REF, processed_hash,
                ),
            )
            processed = cur.fetchone()
        conn.commit()

    assert processed is not None
    assert processed[2:] == ("processed", "reconciliation_pending", False)

    with _connect(RECON_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM app_secure.record_pay10_refund_external_evidence(
                    %s,%s,%s,%s,'webhook','processed',%s,NULL
                )
                """,
                (
                    COMMAND_ID, PROVIDER_PAYMENT_REF, event_id,
                    PROVIDER_REFUND_REF, processed_hash,
                ),
            )
            replay = cur.fetchone()
        conn.commit()
    assert replay is not None
    assert replay[0] == processed[0]
    assert replay[-1] is True

    with _connect(RECON_URL) as conn:
        with pytest.raises(UniqueViolation):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT *
                    FROM app_secure.record_pay10_refund_external_evidence(
                        %s,%s,%s,%s,'webhook','pending',%s,NULL
                    )
                    """,
                    (
                        COMMAND_ID, PROVIDER_PAYMENT_REF, event_id,
                        PROVIDER_REFUND_REF, pending_hash,
                    ),
                )
        conn.rollback()

    for event, status, evidence_hash in (
        ("evt_pay10c_pending", "pending", pending_hash),
        ("evt_pay10c_failed", "failed", failed_hash),
    ):
        with _connect(RECON_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT command_status
                    FROM app_secure.record_pay10_refund_external_evidence(
                        %s,%s,%s,%s,'webhook',%s,%s,NULL
                    )
                    """,
                    (
                        COMMAND_ID, PROVIDER_PAYMENT_REF, event,
                        PROVIDER_REFUND_REF, status, evidence_hash,
                    ),
                )
                assert cur.fetchone()[0] == "reconciliation_pending"
            conn.commit()

    assert _admin_scalar(
        "SELECT status FROM finance.refund_execution_commands WHERE command_id=%s",
        (COMMAND_ID,),
    ) == "reconciliation_pending"


def test_pay10c_reconciliation_rejects_provider_payment_substitution():
    claim = _claim()
    assert claim is not None
    fence = claim[9]
    _bind(fence)
    _unknown(fence)

    with _connect(RECON_URL) as conn:
        with pytest.raises(CheckViolation):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT *
                    FROM app_secure.record_pay10_refund_external_evidence(
                        %s,'pay_substituted',NULL,%s,
                        'reconciliation','processed',%s,NULL
                    )
                    """,
                    (COMMAND_ID, PROVIDER_REFUND_REF, "1" * 64),
                )
        conn.rollback()


def test_pay10c_has_no_financial_finalization_side_effects():
    before = _admin_row(
        """
        SELECT
            (SELECT count(*) FROM finance.ledger_entries),
            (SELECT count(*) FROM finance.ledger_entry_lines),
            (SELECT count(*) FROM finance.outbox_events),
            (SELECT count(*) FROM finance.credit_notes)
        """
    )
    claim = _claim()
    assert claim is not None
    fence = claim[9]
    _bind(fence)

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
                    COMMAND_ID, WORKER_A, fence,
                    PROVIDER_REFUND_REF, EVIDENCE_HASH,
                ),
            )
            cur.fetchone()
        conn.commit()

    after = _admin_row(
        """
        SELECT
            (SELECT count(*) FROM finance.ledger_entries),
            (SELECT count(*) FROM finance.ledger_entry_lines),
            (SELECT count(*) FROM finance.outbox_events),
            (SELECT count(*) FROM finance.credit_notes)
        """
    )
    assert after == before
    assert _admin_scalar(
        "SELECT status FROM finance.refunds WHERE id=%s",
        (REFUND_ID,),
    ) == "processing"
    assert _admin_scalar(
        "SELECT status FROM finance.payments WHERE id=%s",
        (PAYMENT_ID,),
    ) == "captured"
