from __future__ import annotations

import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
MIGRATION=ROOT/"alembic/versions/zp07d8e9f0a50_pay5_finance_event_delivery.py"
WORKER=ROOT/"app/tasks/finance_event_dispatcher.py"
CELERY=ROOT/"app/core/celery_app.py"
CONTRACT=ROOT/"docs/architecture/pay5_finance_event_delivery_v1.json"


def test_pay5_is_additive_over_certified_pay4():
    source=MIGRATION.read_text(encoding="utf-8")
    assert 'revision = "zp07d8e9f0a50"' in source
    assert 'down_revision = "zo07d8e9f0a49"' in source
    assert "ADD COLUMN max_attempts" in source
    assert "ADD COLUMN leased_by" in source
    assert "ADD COLUMN leased_until" in source
    assert "ADD COLUMN lease_fence" in source
    assert "CREATE TABLE public.member_subscription_finance_event_consumptions" in source
    for forbidden in (
        "DROP TABLE finance.outbox_events",
        "ALTER TABLE finance.invoices",
        "UPDATE finance.payments SET",
        "DELETE FROM finance.payments",
        "TRUNCATE TABLE",
    ):
        assert forbidden not in source


def test_pay5_product_consumption_has_exactly_once_keys():
    source=MIGRATION.read_text(encoding="utf-8")
    for token in (
        "finance_event_id UUID NOT NULL",
        "idempotency_key VARCHAR(200) NOT NULL",
        "finance_payload_sha256 CHAR(64) NOT NULL",
        "subscription_term_id UUID NOT NULL",
        "uq_pay5_consumption_finance_event",
        "UNIQUE (finance_event_id)",
        "uq_pay5_consumption_idempotency",
        "UNIQUE (idempotency_key)",
    ):
        assert token in source


def test_pay5_claim_is_skip_locked_leased_and_monotonic_fenced():
    source=MIGRATION.read_text(encoding="utf-8")
    body=source.split(
        "CREATE FUNCTION app_secure.claim_member_subscription_finance_events",1
    )[1].split("$function$",2)[1]
    for token in (
        "FOR UPDATE SKIP LOCKED",
        "e.status='pending'",
        "e.status='processing'",
        "e.leased_until <= pg_catalog.clock_timestamp()",
        "WHEN c.reclaiming THEN e.attempt_count",
        "ELSE e.attempt_count+1",
        "lease_fence=e.lease_fence+1",
        "leased_by=p_worker_id",
    ):
        assert token in body


def test_pay5_consume_and_ack_are_separate_transactions_by_worker_contract():
    worker=WORKER.read_text(encoding="utf-8")
    consume=worker.split("async def _consume_business_effect",1)[1].split(
        "async def _acknowledge_finance",1
    )[0]
    ack=worker.split("async def _acknowledge_finance",1)[1].split(
        "async def _release_failed_delivery",1
    )[0]
    assert "consume_member_subscription_finance_event" in consume
    assert "await session.commit()" in consume
    assert "acknowledge_member_subscription_finance_event" in ack
    assert "await session.commit()" in ack
    assert worker.index("_consume_business_effect") < worker.index("_acknowledge_finance")
    assert "Lease expiry/reclaim will replay the consumption record and then ack" in worker


def test_pay5_worker_never_directly_mutates_finance_tables():
    worker=WORKER.read_text(encoding="utf-8")
    assert "UPDATE finance.outbox_events" not in worker
    assert "INSERT INTO finance.outbox_events" not in worker
    assert "member_subscription_finance_event_consumptions" not in worker
    for capability in (
        "claim_member_subscription_finance_events",
        "consume_member_subscription_finance_event",
        "acknowledge_member_subscription_finance_event",
        "release_member_subscription_finance_event",
    ):
        assert capability in worker


def test_pay5_does_not_grant_direct_pay4_activation_to_worker():
    source=MIGRATION.read_text(encoding="utf-8")
    assert (
        "GRANT EXECUTE ON FUNCTION "
        "app_secure.apply_member_subscription_finance_event"
    ) not in source
    assert "PAY-5 refuses direct worker EXECUTE on PAY-4 activation capability" in source


def test_pay5_canonical_celery_runtime_registers_dispatcher():
    celery=CELERY.read_text(encoding="utf-8")
    assert '"app.tasks.finance_event_dispatcher"' in celery
    assert '"poll-finance-events"' in celery
    assert '"task": "app.tasks.finance_event_dispatcher.run"' in celery
    assert "task_acks_late=True" in celery
    assert "task_reject_on_worker_lost=True" in celery
    assert "worker_prefetch_multiplier=1" in celery


def test_pay5_populated_downgrade_is_fail_closed():
    source=MIGRATION.read_text(encoding="utf-8")
    assert "PAY-5 downgrade blocked: product Finance-event consumption evidence exists" in source
    assert "PAY-5 downgrade blocked: Finance-event delivery evidence exists" in source


def test_pay5_machine_contract_is_bound_to_certified_pay4():
    data=json.loads(CONTRACT.read_text(encoding="utf-8"))
    assert data["phase"]=="PAY-5"
    assert data["inherited_pay4"]["commit"]=="bde31b620b1619c85e581933da1c8de465a6aa00"
    assert data["inherited_pay4"]["tree"]=="4ff5e1176657dc96643d7da01f3d7f91c564fb78"
    assert data["alembic"]=={"predecessor":"zo07d8e9f0a49","head":"zp07d8e9f0a50"}
    assert data["delivery"]["transport_semantics"]=="at_least_once"
    assert data["delivery"]["business_semantics"]=="effectively_once"
    assert data["delivery"]["admitted_event"]=="finance.invoice.paid"
    assert data["runtime_authority"]["direct_worker_finance_outbox_dml"] is False
    assert data["runtime_authority"]["direct_worker_pay4_activation_execute"] is False
    assert data["live_money_movement"] is False
    assert data["refund_provider_execution"]=="DEFERRED_FAIL_CLOSED"
    assert data["next_phase"]=="PAY-6"
