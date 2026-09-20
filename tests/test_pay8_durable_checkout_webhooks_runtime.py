from __future__ import annotations

import os
import uuid

import psycopg
import pytest


APP_URL = os.environ.get("PAY8_APP_DATABASE_URL")
PAYMENT_URL = os.environ.get("PAY8_PAYMENT_DATABASE_URL")
RECON_URL = os.environ.get("PAY8_RECON_DATABASE_URL")
ADMIN_URL = os.environ.get("PAY8_ADMIN_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not (APP_URL and PAYMENT_URL and RECON_URL and ADMIN_URL),
    reason="PAY-8 isolated PG16 harness is not configured",
)


ORG_A = uuid.UUID("88000000-0000-4000-8000-000000000001")
ORG_B = uuid.UUID("88000000-0000-4000-8000-000000000002")
ENTITY = uuid.UUID("88000000-0000-4000-8000-000000000010")
PAYMENT_A = uuid.UUID("88000000-0000-4000-8000-000000000101")
PAYMENT_B = uuid.UUID("88000000-0000-4000-8000-000000000102")
PAYMENT_OTHER = uuid.UUID("88000000-0000-4000-8000-000000000201")
EVENT_ROW = uuid.UUID("88000000-0000-4000-8000-000000000301")

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
SIG_HASH = "d" * 64
SUCCESS_HASH = "e" * 64


def _set_org(cur, org_id: uuid.UUID) -> None:
    cur.execute(
        "SELECT pg_catalog.set_config('app.current_org_id', %s, true)",
        (str(org_id),),
    )


def _cleanup_and_seed() -> None:
    assert ADMIN_URL
    with psycopg.connect(ADMIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                TRUNCATE
                    finance.provider_webhook_inbox,
                    finance.provider_operations,
                    finance.payment_events,
                    finance.payments,
                    finance.legal_entities,
                    public.organizations
                CASCADE
                """
            )
            cur.execute(
                """
                INSERT INTO public.organizations(
                    id,name,slug,tier,is_active,max_branches,
                    default_currency_code
                ) VALUES
                    (%s,'PAY8 Org A','pay8-org-a','basic',true,10,'INR'),
                    (%s,'PAY8 Org B','pay8-org-b','basic',true,10,'INR')
                """,
                (ORG_A, ORG_B),
            )
            cur.execute(
                """
                INSERT INTO finance.legal_entities(
                    id,code,legal_name,pan,registered_address,status
                ) VALUES (
                    %s,'PAY8_ENTITY','PAY8 Test Entity',
                    'ABCDE1234F','PAY8 Test Address','active'
                )
                """,
                (ENTITY,),
            )
            cur.execute(
                """
                INSERT INTO finance.payments(
                    id,organization_id,legal_entity_id,provider_code,
                    provider_order_ref,amount,currency_code,status,raw_status
                ) VALUES
                    (%s,%s,%s,'razorpay_sandbox','intent_pay8_a',100,'INR','created','created'),
                    (%s,%s,%s,'razorpay_sandbox','intent_pay8_b',100,'INR','created','created'),
                    (%s,%s,%s,'razorpay_sandbox','intent_pay8_other',100,'INR','created','created')
                """,
                (
                    PAYMENT_A, ORG_A, ENTITY,
                    PAYMENT_B, ORG_A, ENTITY,
                    PAYMENT_OTHER, ORG_B, ENTITY,
                ),
            )
        conn.commit()


@pytest.fixture(autouse=True)
def _fresh_database():
    _cleanup_and_seed()
    yield
    _cleanup_and_seed()


def _reserve(
    payment_id: uuid.UUID,
    *,
    org_id: uuid.UUID = ORG_A,
    key: str = "pay8:create:1",
    request_hash: str = HASH_A,
):
    assert APP_URL
    with psycopg.connect(APP_URL) as conn:
        with conn.cursor() as cur:
            _set_org(cur, org_id)
            cur.execute(
                """
                SELECT *
                FROM app_secure.reserve_finance_provider_operation(
                    %s,'razorpay_sandbox','test','create_checkout',%s,%s
                )
                """,
                (payment_id, key, request_hash),
            )
            row = cur.fetchone()
        conn.commit()
    return row


def _claim(
    operation_id: uuid.UUID,
    owner: uuid.UUID,
    *,
    org_id: uuid.UUID = ORG_A,
):
    assert APP_URL
    with psycopg.connect(APP_URL) as conn:
        with conn.cursor() as cur:
            _set_org(cur, org_id)
            cur.execute(
                "SELECT * FROM app_secure.claim_finance_provider_operation(%s,%s)",
                (operation_id, owner),
            )
            row = cur.fetchone()
        conn.commit()
    return row


def _finish(
    operation_id: uuid.UUID,
    owner: uuid.UUID,
    fence: int,
    *,
    outcome: str,
    provider_object_id: str | None = None,
    error_code: str | None = None,
    evidence_sha256: str | None = None,
    org_id: uuid.UUID = ORG_A,
):
    assert APP_URL
    with psycopg.connect(APP_URL) as conn:
        with conn.cursor() as cur:
            _set_org(cur, org_id)
            cur.execute(
                """
                SELECT *
                FROM app_secure.finish_finance_provider_operation(
                    %s,%s,%s,%s,%s,%s,%s
                )
                """,
                (
                    operation_id,
                    owner,
                    fence,
                    outcome,
                    provider_object_id,
                    error_code,
                    evidence_sha256,
                ),
            )
            row = cur.fetchone()
        conn.commit()
    return row


def _reconcile(
    operation_id: uuid.UUID,
    *,
    outcome: str,
    provider_object_id: str | None,
    error_code: str | None,
    evidence_sha256: str,
    org_id: uuid.UUID = ORG_A,
):
    assert RECON_URL
    with psycopg.connect(RECON_URL) as conn:
        with conn.cursor() as cur:
            _set_org(cur, org_id)
            cur.execute(
                """
                SELECT *
                FROM app_secure.reconcile_finance_provider_operation(
                    %s,%s,%s,%s,%s
                )
                """,
                (
                    operation_id,
                    outcome,
                    provider_object_id,
                    error_code,
                    evidence_sha256,
                ),
            )
            row = cur.fetchone()
        conn.commit()
    return row


def _record_webhook(
    *,
    event_id: str = "evt_pay8_1",
    payload_hash: str = HASH_A,
    signature_hash: str = SIG_HASH,
    event_type: str = "payment.captured",
    order_ref: str = "order_pay8_webhook",
    payment_ref: str = "pay_pay8_webhook",
    captured: bool | None = True,
):
    assert APP_URL
    with psycopg.connect(APP_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM app_secure.record_finance_provider_webhook(
                    'razorpay_sandbox','test',%s,%s,%s,%s,%s,%s,
                    10000,'INR','captured',%s,%s,NULL,NULL,1784100000
                )
                """,
                (
                    event_id,
                    payload_hash,
                    signature_hash,
                    event_type,
                    order_ref,
                    payment_ref,
                    captured,
                    order_ref,
                ),
            )
            row = cur.fetchone()
        conn.commit()
    return row


def _claim_webhook(
    inbox_id: uuid.UUID,
    owner: uuid.UUID,
    *,
    payment_runtime: bool = False,
):
    url = PAYMENT_URL if payment_runtime else APP_URL
    assert url
    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM app_secure.claim_finance_provider_webhook(%s,%s)",
                (inbox_id, owner),
            )
            row = cur.fetchone()
        conn.commit()
    return row


def _claim_next_webhook(owner: uuid.UUID):
    assert PAYMENT_URL
    with psycopg.connect(PAYMENT_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM app_secure.claim_next_finance_provider_webhook(%s)",
                (owner,),
            )
            row = cur.fetchone()
        conn.commit()
    return row


def _complete_webhook(
    inbox_id: uuid.UUID,
    owner: uuid.UUID,
    fence: int,
    payment_event_id: uuid.UUID,
):
    assert PAYMENT_URL
    with psycopg.connect(PAYMENT_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM app_secure.complete_finance_provider_webhook(
                    %s,%s,%s,%s
                )
                """,
                (inbox_id, owner, fence, payment_event_id),
            )
            row = cur.fetchone()
        conn.commit()
    return row


def _fail_webhook(
    inbox_id: uuid.UUID,
    owner: uuid.UUID,
    fence: int,
    *,
    error_code: str,
    retryable: bool,
):
    assert PAYMENT_URL
    with psycopg.connect(PAYMENT_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM app_secure.fail_finance_provider_webhook(
                    %s,%s,%s,%s,%s
                )
                """,
                (inbox_id, owner, fence, error_code, retryable),
            )
            row = cur.fetchone()
        conn.commit()
    return row


def _admin_scalar(sql: str, params=()):
    assert ADMIN_URL
    with psycopg.connect(ADMIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()[0]


def _admin_execute(sql: str, params=()) -> None:
    assert ADMIN_URL
    with psycopg.connect(ADMIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()


def test_provider_operation_reservation_is_idempotent_and_tenant_bound():
    first = _reserve(PAYMENT_A)
    replay = _reserve(PAYMENT_A)

    assert first[0] == replay[0]
    assert first[1] == "reserved"
    assert first[3] == 0
    assert first[4] is False
    assert replay[4] is True

    with pytest.raises(psycopg.Error):
        _reserve(PAYMENT_A, request_hash=HASH_B)

    with pytest.raises(psycopg.Error):
        _reserve(
            PAYMENT_A,
            org_id=ORG_B,
            key="pay8:cross-tenant",
        )

    assert _admin_scalar(
        "SELECT count(*) FROM finance.provider_operations"
    ) == 1


def test_retryable_reclaim_uses_new_fence_and_stale_worker_cannot_ack():
    operation_id = _reserve(PAYMENT_A)[0]
    owner_a = uuid.uuid4()
    owner_b = uuid.uuid4()

    first_claim = _claim(operation_id, owner_a)
    assert first_claim[1] == "in_flight"
    assert first_claim[2] == 1
    assert first_claim[4] == 1
    assert first_claim[5] is True

    retryable = _finish(
        operation_id,
        owner_a,
        first_claim[2],
        outcome="failed_retryable",
        error_code="connect_failed",
    )
    assert retryable[1] == "failed_retryable"

    second_claim = _claim(operation_id, owner_b)
    assert second_claim[1] == "in_flight"
    assert second_claim[2] == 2
    assert second_claim[4] == 2
    assert second_claim[5] is True

    with pytest.raises(psycopg.Error):
        _finish(
            operation_id,
            owner_a,
            first_claim[2],
            outcome="unknown",
            error_code="stale_worker",
        )

    success = _finish(
        operation_id,
        owner_b,
        second_claim[2],
        outcome="succeeded",
        provider_object_id="order_pay8_success",
        evidence_sha256=SUCCESS_HASH,
    )
    assert success[1] == "succeeded"
    assert success[2] == "order_pay8_success"
    assert _admin_scalar(
        "SELECT provider_order_ref FROM finance.payments WHERE id=%s",
        (PAYMENT_A,),
    ) == "order_pay8_success"

    terminal = _claim(operation_id, uuid.uuid4())
    assert terminal[1] == "succeeded"
    assert terminal[5] is False


def test_expired_in_flight_provider_operation_becomes_unknown_not_retryable():
    operation_id = _reserve(PAYMENT_A)[0]
    owner = uuid.uuid4()
    claimed = _claim(operation_id, owner)
    assert claimed[5] is True

    _admin_execute(
        """
        UPDATE finance.provider_operations
        SET lease_until=pg_catalog.clock_timestamp()-interval '1 second'
        WHERE id=%s
        """,
        (operation_id,),
    )

    recovered = _claim(operation_id, uuid.uuid4())
    assert recovered[1] == "unknown"
    assert recovered[4] == 1
    assert recovered[5] is False
    assert _admin_scalar(
        "SELECT last_error_code FROM finance.provider_operations WHERE id=%s",
        (operation_id,),
    ) == "lease_expired_unknown"


def test_unknown_provider_operation_requires_reconciliation_before_success():
    operation_id = _reserve(PAYMENT_A)[0]
    owner = uuid.uuid4()
    claimed = _claim(operation_id, owner)
    _finish(
        operation_id,
        owner,
        claimed[2],
        outcome="unknown",
        error_code="timeout_unknown",
    )

    blocked = _claim(operation_id, uuid.uuid4())
    assert blocked[1] == "unknown"
    assert blocked[5] is False

    reconciled = _reconcile(
        operation_id,
        outcome="succeeded",
        provider_object_id="order_pay8_reconciled",
        error_code=None,
        evidence_sha256=HASH_C,
    )
    assert reconciled == (
        operation_id,
        "succeeded",
        "order_pay8_reconciled",
    )
    assert _admin_scalar(
        "SELECT provider_order_ref FROM finance.payments WHERE id=%s",
        (PAYMENT_A,),
    ) == "order_pay8_reconciled"


def test_app_runtime_cannot_reconcile_unknown_or_run_global_webhook_recovery():
    operation_id = _reserve(PAYMENT_A)[0]
    owner = uuid.uuid4()
    claimed = _claim(operation_id, owner)
    _finish(
        operation_id,
        owner,
        claimed[2],
        outcome="unknown",
        error_code="timeout_unknown",
    )

    assert APP_URL
    with psycopg.connect(APP_URL) as conn:
        with conn.cursor() as cur:
            _set_org(cur, ORG_A)
            with pytest.raises(psycopg.Error):
                cur.execute(
                    """
                    SELECT *
                    FROM app_secure.reconcile_finance_provider_operation(
                        %s,'failed_final',NULL,'not_found',%s
                    )
                    """,
                    (operation_id, HASH_C),
                )
        conn.rollback()

    with psycopg.connect(APP_URL) as conn:
        with conn.cursor() as cur:
            with pytest.raises(psycopg.Error):
                cur.execute(
                    "SELECT * FROM app_secure.claim_next_finance_provider_webhook(%s)",
                    (uuid.uuid4(),),
                )
        conn.rollback()


def test_same_provider_object_cannot_be_bound_to_two_payments():
    first_id = _reserve(
        PAYMENT_A,
        key="pay8:object:first",
    )[0]
    first_owner = uuid.uuid4()
    first_claim = _claim(first_id, first_owner)
    _finish(
        first_id,
        first_owner,
        first_claim[2],
        outcome="succeeded",
        provider_object_id="order_pay8_unique",
        evidence_sha256=SUCCESS_HASH,
    )

    second_id = _reserve(
        PAYMENT_B,
        key="pay8:object:second",
        request_hash=HASH_B,
    )[0]
    second_owner = uuid.uuid4()
    second_claim = _claim(second_id, second_owner)

    with pytest.raises(psycopg.Error):
        _finish(
            second_id,
            second_owner,
            second_claim[2],
            outcome="succeeded",
            provider_object_id="order_pay8_unique",
            evidence_sha256=HASH_C,
        )

    assert _admin_scalar(
        "SELECT count(*) FROM finance.provider_operations "
        "WHERE provider_object_id='order_pay8_unique'"
    ) == 1


def test_webhook_inbox_preserves_optional_captured_evidence():
    row = _record_webhook(
        event_id="evt_pay8_captured_optional",
        captured=None,
    )
    assert row is not None
    inbox_id = row[0]

    owner = uuid.uuid4()
    claimed = _claim_webhook(inbox_id, owner)
    assert claimed is not None
    assert claimed[4] is True
    # Missing captured evidence remains NULL. PAY-8 must preserve signed
    # normalized uncertainty rather than fabricate provider truth.
    assert claimed[15] is None


def test_webhook_inbox_exact_replay_is_idempotent_and_changed_replay_conflicts():
    first = _record_webhook()
    replay = _record_webhook()

    assert first[0] == replay[0]
    assert first[1] == "received"
    assert first[3] is False
    assert replay[3] is True

    with pytest.raises(psycopg.Error):
        _record_webhook(payload_hash=HASH_B)

    assert _admin_scalar(
        "SELECT count(*) FROM finance.provider_webhook_inbox"
    ) == 1


def test_webhook_expired_lease_is_reclaimed_by_payment_runtime_with_new_fence():
    inbox_id = _record_webhook(event_id="evt_pay8_recover")[0]
    owner_a = uuid.uuid4()
    owner_b = uuid.uuid4()

    first = _claim_webhook(inbox_id, owner_a)
    assert first[1] == "processing"
    assert first[2] == 1
    assert first[3] == 1
    assert first[4] is True

    _admin_execute(
        """
        UPDATE finance.provider_webhook_inbox
        SET lease_until=pg_catalog.clock_timestamp()-interval '1 second'
        WHERE id=%s
        """,
        (inbox_id,),
    )

    recovered = _claim_next_webhook(owner_b)
    assert recovered is not None
    assert recovered[0] == inbox_id
    assert recovered[1] == "processing"
    assert recovered[2] == 2
    assert recovered[3] == 2
    assert recovered[4] is True

    with pytest.raises(psycopg.Error):
        _fail_webhook(
            inbox_id,
            owner_a,
            first[2],
            error_code="stale_worker",
            retryable=True,
        )


def test_webhook_completion_binds_exact_payment_event_and_is_terminal():
    event_id = "evt_pay8_complete"
    inbox_id = _record_webhook(event_id=event_id)[0]
    owner = uuid.uuid4()
    claimed = _claim_webhook(inbox_id, owner, payment_runtime=True)

    _admin_execute(
        """
        UPDATE finance.payments
        SET provider_order_ref='order_pay8_webhook'
        WHERE id=%s
        """,
        (PAYMENT_A,),
    )
    _admin_execute(
        """
        INSERT INTO finance.payment_events(
            id,payment_id,provider_code,provider_event_id,
            event_type,event_payload_sha256
        ) VALUES (
            %s,%s,'razorpay_sandbox',%s,'payment.captured',%s
        )
        """,
        (EVENT_ROW, PAYMENT_A, event_id, HASH_A),
    )

    completed = _complete_webhook(
        inbox_id,
        owner,
        claimed[2],
        EVENT_ROW,
    )
    assert completed == (
        inbox_id,
        "processed",
        EVENT_ROW,
        PAYMENT_A,
        ORG_A,
    )

    replay_claim = _claim_webhook(
        inbox_id,
        uuid.uuid4(),
        payment_runtime=True,
    )
    assert replay_claim[1] == "processed"
    assert replay_claim[4] is False


def test_webhook_wrong_payment_event_is_rejected_without_completion():
    inbox_id = _record_webhook(event_id="evt_pay8_expected")[0]
    owner = uuid.uuid4()
    claimed = _claim_webhook(inbox_id, owner, payment_runtime=True)

    _admin_execute(
        """
        INSERT INTO finance.payment_events(
            id,payment_id,provider_code,provider_event_id,
            event_type,event_payload_sha256
        ) VALUES (
            %s,%s,'razorpay_sandbox','evt_pay8_other',
            'payment.captured',%s
        )
        """,
        (EVENT_ROW, PAYMENT_A, HASH_A),
    )

    with pytest.raises(psycopg.Error):
        _complete_webhook(
            inbox_id,
            owner,
            claimed[2],
            EVENT_ROW,
        )

    assert _admin_scalar(
        "SELECT status FROM finance.provider_webhook_inbox WHERE id=%s",
        (inbox_id,),
    ) == "processing"


def test_retry_exhausted_webhook_dead_letters_instead_of_looping_forever():
    inbox_id = _record_webhook(event_id="evt_pay8_dead")[0]
    owner = uuid.uuid4()
    claimed = _claim_webhook(inbox_id, owner, payment_runtime=True)

    _admin_execute(
        "UPDATE finance.provider_webhook_inbox SET max_attempts=1 WHERE id=%s",
        (inbox_id,),
    )

    failed = _fail_webhook(
        inbox_id,
        owner,
        claimed[2],
        error_code="provider_processing_retry",
        retryable=True,
    )
    assert failed[1] == "dead_letter"

    next_claim = _claim_webhook(
        inbox_id,
        uuid.uuid4(),
        payment_runtime=True,
    )
    assert next_claim[1] == "dead_letter"
    assert next_claim[4] is False


def test_runtime_roles_have_no_direct_provider_operation_or_webhook_table_dml():
    for url in (APP_URL, PAYMENT_URL, RECON_URL):
        assert url
        with psycopg.connect(url) as conn:
            with conn.cursor() as cur:
                with pytest.raises(psycopg.Error):
                    cur.execute(
                        "SELECT count(*) FROM finance.provider_operations"
                    )
            conn.rollback()
        with psycopg.connect(url) as conn:
            with conn.cursor() as cur:
                with pytest.raises(psycopg.Error):
                    cur.execute(
                        "SELECT count(*) FROM finance.provider_webhook_inbox"
                    )
            conn.rollback()
