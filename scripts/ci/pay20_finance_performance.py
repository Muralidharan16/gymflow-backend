#!/usr/bin/env python3
"""PAY-20 Finance-path load/soak certification.

Synthetic-only. No live provider network is used. The harness drives the same
Finance services/capabilities already certified by PAY-7..PAY-19 and records
per-surface latency plus correctness invariants under bounded concurrency.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import resource
import statistics
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import redis

from app.core.database import AsyncSessionLocal
from tests import test_pay4_member_finance_binding_runtime as pay4
from tests import test_pay5_finance_event_delivery_runtime as pay5
from tests.finance_core.test_phase5c_invoice_engine import (
    fetch_scalar,
    seed_master_data,
)
from tests.finance_core.test_phase5d_payment_ledger import issued_invoice
from tests.finance_core.test_phase5i_settlement_reconciliation import (
    reconcile_payment,
)
from tests.finance_core.test_phase5j_refund_credit_note_reversal import (
    create_refund_intent,
)
from app.finance_core.domain.razorpay_sandbox import (
    RazorpayOrderCreateRequest,
    RazorpayOrderCreateResponse,
)
from tests.finance_core.test_phase6c_checkout_orchestration import (
    command,
    orchestrate,
)
from tests.finance_core.test_phase6d_razorpay_webhook_normalization import (
    razorpay_payload,
    signed_webhook,
)
from tests.finance_core.test_phase6e_payment_application_gate import (
    apply_gate,
    seed_ledger_accounts_only,
)
from tests.finance_core.test_pay17_fault_injection_concurrency import (
    _webhook_service,
)


class Pay20RazorpayClient:
    def __init__(self, prefix: str):
        self.prefix = prefix.replace("-", "_")
        self.requests: list[RazorpayOrderCreateRequest] = []

    async def create_order(
        self,
        request: RazorpayOrderCreateRequest,
    ) -> RazorpayOrderCreateResponse:
        self.requests.append(request)
        return RazorpayOrderCreateResponse(
            order_id=f"order_{self.prefix}_{len(self.requests)}",
            amount_subunits=request.amount_subunits,
            currency_code=request.currency_code,
            receipt=request.receipt,
            status="created",
        )


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    weight = position - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def latency_summary(values: list[float]) -> dict[str, float | int]:
    return {
        "count": len(values),
        "p50_ms": round(percentile(values, 0.50), 3),
        "p95_ms": round(percentile(values, 0.95), 3),
        "p99_ms": round(percentile(values, 0.99), 3),
        "max_ms": round(max(values), 3) if values else 0.0,
    }


async def timed(bucket: dict[str, list[float]], name: str, awaitable):
    started = time.perf_counter()
    result = await awaitable
    bucket[name].append((time.perf_counter() - started) * 1000.0)
    return result


async def finance_cycle(
    sequence: int,
    *,
    prefix: str,
    provider_client: Pay20RazorpayClient,
    latencies: dict[str, list[float]],
) -> None:
    checkout, _ = await timed(
        latencies,
        "checkout",
        orchestrate(
            command(idempotency_key=f"{prefix}-checkout-{sequence}"),
            client=provider_client,
        ),
    )

    raw = razorpay_payload(
        event_id=f"evt_{prefix}_{sequence}",
        event_type="payment.captured",
        payment_id=f"pay_{prefix}_{sequence}",
        order_id=checkout.provider_order_id,
        status="captured",
    )
    webhook = signed_webhook(
        raw,
        idempotency_key=f"{prefix}-webhook-{sequence}",
    )

    webhook_started = time.perf_counter()
    async with AsyncSessionLocal() as session:
        service = _webhook_service(session)
        receipt = await service.record_verified_webhook(webhook)
        await session.commit()
        owner = uuid.uuid4()
        claimed = await service.claim_recorded_webhook(
            inbox_id=receipt.inbox_id,
            lease_owner=owner,
        )
        await session.commit()
        if not claimed.claimed:
            raise RuntimeError("PAY-20 fresh webhook was not claimable")

        result = await service.process_claimed_webhook(claimed)
        await service.complete_claimed_webhook(
            claimed=claimed,
            lease_owner=owner,
            payment_event_id=result.payment_event_id,
        )
        await session.commit()

    latencies["webhook"].append(
        (time.perf_counter() - webhook_started) * 1000.0
    )

    # A generic checkout is intentionally not a member-subscription entitlement
    # binding, so verified provider evidence may remain unapplied. PAY-20 must
    # load the certified explicit PAY-9 application boundary rather than
    # weakening settlement to accept an unapplied payment.
    await timed(
        latencies,
        "payment_application_ledger",
        apply_gate(
            checkout.finance_checkout_intent_id,
            checkout.finance_invoice_id,
            idempotency_key=f"{prefix}-apply-{sequence}",
        ),
    )

    await timed(
        latencies,
        "settlement_reconciliation",
        reconcile_payment(
            checkout.finance_checkout_intent_id,
            settlement_ref=f"SETTLE-{prefix}-{sequence}",
            idempotency_key=f"{prefix}-settlement-{sequence}",
        ),
    )

    await timed(
        latencies,
        "refund_creation",
        create_refund_intent(
            checkout.finance_checkout_intent_id,
            refund_ref=f"RF-{prefix}-{sequence}",
            amount="100.00",
            idempotency_key=f"{prefix}-refund-{sequence}",
        ),
    )


async def run_cycles(
    *,
    prefix: str,
    cycles: int,
    concurrency: int,
) -> dict:
    provider_client = Pay20RazorpayClient(prefix)
    latencies: dict[str, list[float]] = defaultdict(list)
    errors: list[str] = []
    semaphore = asyncio.Semaphore(concurrency)

    deadlocks_before = int(
        await fetch_scalar(
            "SELECT deadlocks FROM pg_stat_database "
            "WHERE datname=current_database()"
        )
        or 0
    )
    started = time.perf_counter()

    async def one(index: int) -> None:
        async with semaphore:
            try:
                await finance_cycle(
                    index,
                    prefix=prefix,
                    provider_client=provider_client,
                    latencies=latencies,
                )
            except Exception as exc:
                errors.append(f"{type(exc).__name__}:{exc}")

    await asyncio.gather(*(one(i) for i in range(1, cycles + 1)))
    elapsed = time.perf_counter() - started

    deadlocks_after = int(
        await fetch_scalar(
            "SELECT deadlocks FROM pg_stat_database "
            "WHERE datname=current_database()"
        )
        or 0
    )

    if errors:
        raise RuntimeError(
            f"PAY-20 Finance load had {len(errors)} errors; first={errors[:3]}"
        )
    if len(provider_client.requests) != cycles:
        raise RuntimeError(
            "PAY-20 provider-call cardinality drift: "
            f"{len(provider_client.requests)} != {cycles}"
        )

    for operation in (
        "checkout",
        "webhook",
        "payment_application_ledger",
        "settlement_reconciliation",
        "refund_creation",
    ):
        if len(latencies[operation]) != cycles:
            raise RuntimeError(
                f"PAY-20 missing latency samples for {operation}: "
                f"{len(latencies[operation])} != {cycles}"
            )

    return {
        "cycles": cycles,
        "concurrency": concurrency,
        "elapsed_seconds": round(elapsed, 6),
        "throughput_cycles_per_second": round(cycles / elapsed, 3),
        "deadlocks_before": deadlocks_before,
        "deadlocks_after": deadlocks_after,
        "deadlocks_delta": deadlocks_after - deadlocks_before,
        "provider_calls": len(provider_client.requests),
        "latency": {
            name: latency_summary(values)
            for name, values in sorted(latencies.items())
        },
    }


async def run_duration(
    *,
    prefix: str,
    duration_seconds: int,
    concurrency: int,
) -> dict:
    if duration_seconds < 300:
        raise RuntimeError("PAY-20 Finance soak requires at least 300 seconds")

    provider_client = Pay20RazorpayClient(prefix)
    latencies: dict[str, list[float]] = defaultdict(list)
    errors: list[str] = []
    sequence = 0
    sequence_lock = asyncio.Lock()
    deadlocks_before = int(
        await fetch_scalar(
            "SELECT deadlocks FROM pg_stat_database "
            "WHERE datname=current_database()"
        )
        or 0
    )
    started = time.perf_counter()
    deadline = started + duration_seconds

    async def next_sequence() -> int:
        nonlocal sequence
        async with sequence_lock:
            sequence += 1
            return sequence

    async def worker() -> None:
        while time.perf_counter() < deadline:
            index = await next_sequence()
            try:
                await finance_cycle(
                    index,
                    prefix=prefix,
                    provider_client=provider_client,
                    latencies=latencies,
                )
            except Exception as exc:
                errors.append(f"{type(exc).__name__}:{exc}")
                return

    await asyncio.gather(*(worker() for _ in range(concurrency)))
    elapsed = time.perf_counter() - started
    deadlocks_after = int(
        await fetch_scalar(
            "SELECT deadlocks FROM pg_stat_database "
            "WHERE datname=current_database()"
        )
        or 0
    )

    cycles = sequence
    if errors:
        raise RuntimeError(
            f"PAY-20 Finance soak had {len(errors)} errors; first={errors[:3]}"
        )
    if len(provider_client.requests) != cycles:
        raise RuntimeError(
            "PAY-20 soak provider-call cardinality drift: "
            f"{len(provider_client.requests)} != {cycles}"
        )
    for operation in (
        "checkout",
        "webhook",
        "payment_application_ledger",
        "settlement_reconciliation",
        "refund_creation",
    ):
        if len(latencies[operation]) != cycles:
            raise RuntimeError(
                f"PAY-20 soak missing {operation} samples: "
                f"{len(latencies[operation])} != {cycles}"
            )

    return {
        "cycles": cycles,
        "concurrency": concurrency,
        "duration_seconds": round(elapsed, 3),
        "throughput_cycles_per_second": round(cycles / elapsed, 3),
        "deadlocks_before": deadlocks_before,
        "deadlocks_after": deadlocks_after,
        "deadlocks_delta": deadlocks_after - deadlocks_before,
        "provider_calls": len(provider_client.requests),
        "latency": {
            name: latency_summary(values)
            for name, values in sorted(latencies.items())
        },
    }


async def run_invoice_generation(
    *,
    prefix: str,
    items: int,
    concurrency: int,
) -> dict:
    latencies: list[float] = []
    semaphore = asyncio.Semaphore(concurrency)
    started = time.perf_counter()

    async def one(index: int) -> None:
        async with semaphore:
            t0 = time.perf_counter()
            await issued_invoice(
                idempotency_key=f"{prefix}-invoice-only-{index}"
            )
            latencies.append((time.perf_counter() - t0) * 1000.0)

    await asyncio.gather(*(one(i) for i in range(1, items + 1)))
    elapsed = time.perf_counter() - started
    return {
        "items": items,
        "concurrency": concurrency,
        "elapsed_seconds": round(elapsed, 6),
        "throughput_invoices_per_second": round(items / elapsed, 3),
        "latency": latency_summary(latencies),
    }


def run_hot_subscription_activation(iterations: int) -> dict:
    """Race two PAY-5 consumers on the exact same leased Finance event."""
    latencies: list[float] = []
    exact_once = 0
    worker_id = pay5.WORKER_A

    for _ in range(iterations):
        pay5._cleanup_pay5()
        term = pay5._seed_ready()

        # PAY-5 claims are global batches and may legitimately distribute
        # unrelated pending events across workers. This disposable-test admin
        # injection isolates the one target event at the post-claim lease/fence
        # boundary so the performance proof stresses same-event consumption,
        # not global queue scheduling.
        with psycopg.connect(pay5.ADMIN_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE finance.outbox_events
                       SET status='processing',
                           attempt_count=attempt_count+1,
                           claimed_at=pg_catalog.clock_timestamp(),
                           leased_by=%s,
                           leased_until=pg_catalog.clock_timestamp()
                               + INTERVAL '60 seconds',
                           lease_fence=lease_fence+1,
                           last_error_code=NULL
                     WHERE id=%s
                       AND status='pending'
                    RETURNING lease_fence
                    """,
                    (worker_id, pay4.EVENT),
                )
                row = cur.fetchone()
                if row is None:
                    raise RuntimeError(
                        "PAY-20 target Finance event was not pending"
                    )
                fence = int(row[0])
            conn.commit()

        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=2) as pool:
            rows = list(
                pool.map(
                    lambda _: pay5._consume(worker_id, fence),
                    range(2),
                )
            )
        acknowledged = pay5._ack(worker_id, fence)
        latencies.append((time.perf_counter() - started) * 1000.0)

        applied = sum(row[2] is True and row[3] is False for row in rows)
        replayed = sum(row[2] is False and row[3] is True for row in rows)
        if (
            acknowledged is True
            and applied == 1
            and replayed == 1
            and all(row[0] == term for row in rows)
            and pay4._status(term) == "active"
            and pay5._consumption_count() == 1
            and pay5._finance_subscription_event_count() == 1
        ):
            exact_once += 1

    pay5._cleanup_pay5()
    if exact_once != iterations:
        raise RuntimeError(
            f"PAY-20 subscription exact-once drift: {exact_once}/{iterations}"
        )
    return {
        "iterations": iterations,
        "contenders_per_iteration": 2,
        "exact_once": exact_once,
        "latency": latency_summary(latencies),
    }


async def assert_financial_integrity(prefix: str, cycles: int) -> dict:
    event_prefix = f"evt_{prefix}_%"
    provider_ref_prefix = f"pay_{prefix}_%"
    settlement_prefix = f"SETTLE-{prefix}-%"
    refund_prefix = f"RF-{prefix}-%"

    checks = {
        "captured_or_settled_payments": int(
            await fetch_scalar(
                "SELECT count(*) FROM finance.payments "
                "WHERE provider_payment_ref LIKE :prefix "
                "AND status IN ('captured','settled','partially_refunded','refunded')",
                {"prefix": provider_ref_prefix},
            )
            or 0
        ),
        "payment_events": int(
            await fetch_scalar(
                "SELECT count(*) FROM finance.payment_events "
                "WHERE provider_event_id LIKE :prefix",
                {"prefix": event_prefix},
            )
            or 0
        ),
        "payment_applications": int(
            await fetch_scalar(
                "SELECT count(*) "
                "FROM finance.payment_application_records a "
                "JOIN finance.payment_events e ON e.id=a.payment_event_id "
                "WHERE e.provider_event_id LIKE :prefix",
                {"prefix": event_prefix},
            )
            or 0
        ),
        "allocations": int(
            await fetch_scalar(
                "SELECT count(*) FROM finance.payment_allocations a "
                "JOIN finance.payments p ON p.id=a.payment_id "
                "WHERE p.provider_payment_ref LIKE :prefix",
                {"prefix": provider_ref_prefix},
            )
            or 0
        ),
        "settlements": int(
            await fetch_scalar(
                "SELECT count(*) FROM finance.outbox_events "
                "WHERE event_type='finance.payment.reconciled' "
                "AND payload_json->>'settlement_ref' LIKE :prefix",
                {"prefix": settlement_prefix},
            )
            or 0
        ),
        "refunds": int(
            await fetch_scalar(
                "SELECT count(*) FROM finance.refunds "
                "WHERE reason_code LIKE :prefix",
                {"prefix": refund_prefix},
            )
            or 0
        ),
        "unbalanced_posted_ledgers": int(
            await fetch_scalar(
                "SELECT count(*) FROM ("
                " SELECT e.id "
                " FROM finance.ledger_entries e "
                " JOIN finance.ledger_entry_lines l "
                "   ON l.ledger_entry_id=e.id "
                " WHERE e.status='posted' "
                " GROUP BY e.id "
                " HAVING sum(l.debit_amount)<>sum(l.credit_amount)"
                ") q"
            )
            or 0
        ),
        "duplicate_provider_payment_refs": int(
            await fetch_scalar(
                "SELECT count(*) FROM ("
                " SELECT provider_code,provider_payment_ref "
                " FROM finance.payments "
                " WHERE provider_payment_ref IS NOT NULL "
                " GROUP BY 1,2 HAVING count(*)>1"
                ") q"
            )
            or 0
        ),
        "duplicate_invoice_numbers": int(
            await fetch_scalar(
                "SELECT count(*) FROM ("
                " SELECT legal_entity_id,gst_registration_id,financial_year,"
                "        official_invoice_number "
                " FROM finance.invoices "
                " WHERE official_invoice_number IS NOT NULL "
                " GROUP BY 1,2,3,4 HAVING count(*)>1"
                ") q"
            )
            or 0
        ),
        "unknown_payments": int(
            await fetch_scalar(
                "SELECT count(*) FROM finance.payments WHERE status='unknown'"
            )
            or 0
        ),
    }

    expected_equal = (
        "captured_or_settled_payments",
        "payment_events",
        "payment_applications",
        "allocations",
        "settlements",
        "refunds",
    )
    for key in expected_equal:
        if checks[key] != cycles:
            raise RuntimeError(
                f"PAY-20 integrity {key}={checks[key]} expected={cycles}"
            )
    for key in (
        "unbalanced_posted_ledgers",
        "duplicate_provider_payment_refs",
        "duplicate_invoice_numbers",
        "unknown_payments",
    ):
        if checks[key] != 0:
            raise RuntimeError(f"PAY-20 integrity {key}={checks[key]}")

    return checks


def memory_rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports KiB.
    return int(value * 1024)


async def sample_resources() -> dict:
    redis_client = redis.Redis.from_url(
        os.environ["REDIS_URL"],
        decode_responses=True,
    )
    try:
        clients = redis_client.info("clients")
        memory = redis_client.info("memory")
        rejected = int(redis_client.info("stats").get("rejected_connections", 0))
        ping = redis_client.ping()
    finally:
        redis_client.close()

    return {
        "rss_bytes": memory_rss_bytes(),
        "db_connections": int(
            await fetch_scalar(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname=current_database()"
            )
            or 0
        ),
        "redis_connected_clients": int(clients.get("connected_clients", 0)),
        "redis_used_memory": int(memory.get("used_memory", 0)),
        "redis_rejected_connections": rejected,
        "redis_ping": bool(ping),
        "payment_unknown_total": int(
            await fetch_scalar(
                "SELECT count(*) FROM finance.payments WHERE status='unknown'"
            )
            or 0
        ),
        "reconciliation_open_total": int(
            await fetch_scalar(
                "SELECT count(*) "
                "FROM public.platform_accounting_reconciliation_items "
                "WHERE resolution_status<>'resolved'"
            )
            or 0
        ),
        "finance_outbox_backlog": int(
            await fetch_scalar(
                "SELECT count(*) FROM finance.outbox_events "
                "WHERE status IN ('pending','processing','failed')"
            )
            or 0
        ),
        "oldest_finance_outbox_age_seconds": float(
            await fetch_scalar(
                "SELECT coalesce(extract(epoch FROM "
                "(clock_timestamp()-min(created_at))),0) "
                "FROM finance.outbox_events "
                "WHERE status IN ('pending','processing','failed')"
            )
            or 0
        ),
    }


def verify_against_baseline(
    baseline: dict,
    candidate: dict,
) -> list[str]:
    errors: list[str] = []
    base_rate = float(baseline["finance"]["throughput_cycles_per_second"])
    candidate_rate = float(candidate["finance"]["throughput_cycles_per_second"])
    if candidate_rate < base_rate * 0.60:
        errors.append("Finance throughput below 60% of calibration")

    p10 = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "docs/architecture/p10_performance_budgets.v1.json"
        ).read_text(encoding="utf-8")
    )["budgets"]["representative_http"]
    max_p95 = float(p10["max_write_p95_ms"])
    max_p99 = float(p10["max_write_p99_ms"])

    for operation, summary in candidate["finance"]["latency"].items():
        if float(summary["p95_ms"]) > max_p95:
            errors.append(f"{operation} p95 exceeds inherited P10 write budget")
        if float(summary["p99_ms"]) > max_p99:
            errors.append(f"{operation} p99 exceeds inherited P10 write budget")

        base_summary = baseline["finance"]["latency"].get(operation)
        if base_summary:
            base_p95 = float(base_summary["p95_ms"])
            if base_p95 > 0 and float(summary["p95_ms"]) > base_p95 * 2.0:
                errors.append(f"{operation} p95 exceeds 2x calibration")

    return errors


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("calibration", "load", "soak"), required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--cycles", type=int, default=24)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--invoice-items", type=int, default=16)
    parser.add_argument("--activation-iterations", type=int, default=4)
    parser.add_argument("--baseline")
    parser.add_argument("--duration-seconds", type=int, default=0)
    parser.add_argument("--sample-interval-seconds", type=int, default=5)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    if args.cycles < 4:
        raise SystemExit("PAY-20 cycles must be >= 4")
    if not 1 <= args.concurrency <= 32:
        raise SystemExit("PAY-20 concurrency must be in [1,32]")

    await seed_master_data()
    await seed_ledger_accounts_only()

    resource_before = await sample_resources()
    resource_samples: list[dict] = [dict(resource_before, elapsed_seconds=0.0)]
    sampler_stop = asyncio.Event()
    sampler_started = time.perf_counter()

    async def sampler() -> None:
        while not sampler_stop.is_set():
            try:
                await asyncio.wait_for(
                    sampler_stop.wait(),
                    timeout=max(1, args.sample_interval_seconds),
                )
            except asyncio.TimeoutError:
                row = await sample_resources()
                row["elapsed_seconds"] = round(
                    time.perf_counter() - sampler_started,
                    3,
                )
                resource_samples.append(row)

    sampler_task = asyncio.create_task(sampler())
    try:
        if args.mode == "soak":
            finance = await run_duration(
                prefix=args.prefix,
                duration_seconds=args.duration_seconds,
                concurrency=args.concurrency,
            )
            actual_cycles = int(finance["cycles"])
        else:
            finance = await run_cycles(
                prefix=args.prefix,
                cycles=args.cycles,
                concurrency=args.concurrency,
            )
            actual_cycles = args.cycles
    finally:
        sampler_stop.set()
        await sampler_task

    invoice = await run_invoice_generation(
        prefix=args.prefix,
        items=args.invoice_items,
        concurrency=min(args.concurrency, 12),
    )
    activation = run_hot_subscription_activation(args.activation_iterations)
    integrity = await assert_financial_integrity(args.prefix, actual_cycles)
    resource_after = await sample_resources()
    resource_after["elapsed_seconds"] = round(
        time.perf_counter() - sampler_started,
        3,
    )
    resource_samples.append(resource_after)

    record = {
        "schema_version": 1,
        "phase": "PAY-20",
        "mode": args.mode,
        "synthetic_only": True,
        "live_provider": False,
        "finance": finance,
        "invoice_generation": invoice,
        "subscription_activation_hot_account": activation,
        "integrity": integrity,
        "resources": {
            "before": resource_before,
            "after": resource_after,
            "rss_growth_bytes": max(
                0,
                resource_after["rss_bytes"] - resource_before["rss_bytes"],
            ),
            "db_connection_growth": (
                resource_after["db_connections"]
                - resource_before["db_connections"]
            ),
            "redis_memory_growth_bytes": (
                resource_after["redis_used_memory"]
                - resource_before["redis_used_memory"]
            ),
            "samples": resource_samples,
            "max_db_connections": max(
                int(row["db_connections"]) for row in resource_samples
            ),
            "max_finance_outbox_backlog": max(
                int(row["finance_outbox_backlog"]) for row in resource_samples
            ),
            "max_oldest_finance_outbox_age_seconds": max(
                float(row["oldest_finance_outbox_age_seconds"])
                for row in resource_samples
            ),
            "max_payment_unknown_total": max(
                int(row["payment_unknown_total"]) for row in resource_samples
            ),
            "max_reconciliation_open_total": max(
                int(row["reconciliation_open_total"]) for row in resource_samples
            ),
        },
        "errors": [],
    }

    if finance["deadlocks_delta"] != 0:
        record["errors"].append("unexpected PostgreSQL deadlocks")
    if not resource_after["redis_ping"]:
        record["errors"].append("Redis PING failed")
    if resource_after["redis_rejected_connections"] != 0:
        record["errors"].append("Redis rejected connections")
    if record["resources"]["max_payment_unknown_total"] != 0:
        record["errors"].append("payment unknown state appeared under load")
    if record["resources"]["max_reconciliation_open_total"] != 0:
        record["errors"].append("open accounting reconciliation appeared under load")
    if args.mode == "soak" and args.duration_seconds < 300:
        record["errors"].append("Finance soak duration below 300 seconds")

    if args.baseline:
        baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        record["errors"].extend(verify_against_baseline(baseline, record))

    record["decision"] = "PASS" if not record["errors"] else "FAIL"
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    print(json.dumps(record, indent=2, sort_keys=True))
    if record["errors"]:
        for error in record["errors"]:
            print("PAY-20 violation: " + error)
        return 1

    marker = {
        "calibration": "PAY20_FINANCE_CALIBRATION=PASS",
        "load": "PAY20_FINANCE_LOAD=PASS",
        "soak": "PAY20_FINANCE_SOAK=PASS",
    }[args.mode]
    print(marker)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
