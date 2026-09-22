#!/usr/bin/env python3
"""PAY-20 real Finance dispatcher burst-capacity certification."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import psycopg

from app.tasks import finance_event_dispatcher as dispatcher
from tests import test_pay4_member_finance_binding_runtime as pay4
from tests import test_pay5_finance_event_delivery_runtime as pay5


def _id(kind: str, index: int) -> uuid.UUID:
    return uuid.uuid5(
        uuid.UUID("20000000-0000-4000-8000-000000000020"),
        f"pay20-dispatch:{kind}:{index}",
    )


def _seed_extra(index: int) -> None:
    sub = _id("subscription", index)
    invoice = _id("invoice", index)
    payment = _id("payment", index)
    event = _id("event", index)
    start = date.today()
    end = start + timedelta(days=30)

    assert pay4.APP_URL and pay4.ADMIN_URL

    with psycopg.connect(pay4.APP_URL) as conn:
        with conn.cursor() as cur:
            pay4._set_org(cur)
            cur.execute(
                """
                INSERT INTO public.member_subscriptions_v2(
                    id,org_id,branch_id,membership_plan_id,primary_member_id,
                    subscription_code,start_date,end_date,status,price_snapshot,
                    currency_code,duration_value_snapshot,duration_unit_snapshot,
                    max_members_snapshot
                ) VALUES(
                    %s,%s,%s,%s,%s,%s,%s,%s,'pending',100,'INR',1,'months',1
                )
                """,
                (
                    sub,
                    pay4.ORG,
                    pay4.BRANCH,
                    pay4.PLAN,
                    pay4.MEMBER,
                    f"P20-DISP-{index:05d}",
                    start,
                    end,
                ),
            )
            cur.execute(
                """
                INSERT INTO public.subscription_members(
                    id,org_id,subscription_id,member_id,slot_number,role,is_active
                ) VALUES(
                    pg_catalog.gen_random_uuid(),%s,%s,%s,1,'primary',true
                )
                """,
                (pay4.ORG, sub, pay4.MEMBER),
            )
            cur.execute(
                "SELECT * FROM app_secure.create_member_subscription_pending_term(%s)",
                (sub,),
            )
            term = cur.fetchone()[0]
        conn.commit()

    with psycopg.connect(pay4.ADMIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO finance.invoices(
                    id,organization_id,billing_party_id,legal_entity_id,
                    gst_registration_id,division_id,brand_id,financial_year,
                    official_invoice_number,brand_reference,status,currency_code,
                    seller_legal_name,seller_gstin,seller_registered_address,
                    seller_state_code,buyer_billing_name,buyer_address,
                    buyer_place_of_supply_state_code,buyer_gst_treatment,
                    gst_supply_type,subtotal_amount,discount_amount,taxable_amount,
                    total_tax_amount,grand_total_amount,issued_at
                ) VALUES(
                    %s,%s,%s,%s,%s,%s,%s,'2627',%s,%s,
                    'issued','INR','PAY4 Entity','33ABCDE1234F1Z5','Chennai','33',
                    'PAY4 Member','Chennai','33','b2c','intra_state',
                    100,0,100,0,100,pg_catalog.clock_timestamp()
                )
                """,
                (
                    invoice,
                    pay4.ORG,
                    pay4.PARTY,
                    pay4.ENTITY,
                    pay4.GST,
                    pay4.DIVISION,
                    pay4.BRAND,
                    f"P20D{index:06d}",
                    f"P20B{index:06d}",
                ),
            )
            # PAY-4 binding authority requires the authoritative invoice plan
            # snapshot to be represented by exactly one matching invoice line.
            cur.execute(
                """
                INSERT INTO finance.invoice_lines(
                    id,invoice_id,line_number,description,hsn_sac,quantity,
                    unit_amount,discount_amount,taxable_amount,
                    gst_rate_basis_points,cgst_amount,sgst_amount,igst_amount,
                    total_tax_amount,line_total_amount,pricing_mode
                ) VALUES(
                    pg_catalog.gen_random_uuid(),%s,1,%s,'9999',1,
                    100,0,100,0,0,0,0,0,100,'tax_inclusive'
                )
                """,
                (
                    invoice,
                    f"Membership subscription P20-DISP-{index:05d}",
                ),
            )
        conn.commit()

    with psycopg.connect(pay4.APP_URL) as conn:
        with conn.cursor() as cur:
            pay4._set_org(cur)
            cur.execute(
                "SELECT * FROM app_secure.record_member_subscription_finance_binding(%s,%s)",
                (sub, invoice),
            )
            binding = cur.fetchone()
            if binding is None or binding[1] != term:
                raise RuntimeError("PAY-20 dispatcher binding seed failed")
        conn.commit()

    with psycopg.connect(pay4.ADMIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE finance.invoices SET status='paid' WHERE id=%s",
                (invoice,),
            )
            cur.execute(
                """
                INSERT INTO finance.payments(
                    id,organization_id,legal_entity_id,provider_code,
                    provider_payment_ref,amount,currency_code,status
                ) VALUES(
                    %s,%s,%s,'pay20_capacity',%s,100,'INR','captured'
                )
                """,
                (payment, pay4.ORG, pay4.ENTITY, f"pay20-cap-{index}"),
            )
            cur.execute(
                """
                INSERT INTO finance.payment_allocations(
                    payment_id,invoice_id,allocated_amount
                ) VALUES(%s,%s,100)
                """,
                (payment, invoice),
            )
            cur.execute(
                """
                INSERT INTO finance.outbox_events(
                    id,organization_id,aggregate_type,aggregate_id,event_type,
                    idempotency_key,payload_json,payload_sha256,status,attempt_count
                ) VALUES(
                    %s,%s,'invoice',%s,'finance.invoice.paid',%s,
                    pg_catalog.jsonb_build_object(
                        'invoice_id',%s::text,'status','paid'
                    ),
                    %s,'pending',0
                )
                """,
                (
                    event,
                    pay4.ORG,
                    invoice,
                    f"pay20:dispatch:{index}",
                    invoice,
                    "d" * 64,
                ),
            )
        conn.commit()


def _eligible_state() -> tuple[int, float, int, int, int]:
    assert pay4.ADMIN_URL
    with psycopg.connect(pay4.ADMIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    count(*) FILTER (
                        WHERE e.status IN ('pending','processing','failed')
                    ),
                    COALESCE(
                        extract(epoch FROM (
                            pg_catalog.clock_timestamp()
                            - min(e.created_at) FILTER (
                                WHERE e.status IN (
                                    'pending','processing','failed'
                                )
                            )
                        )),
                        0
                    ),
                    count(*) FILTER (WHERE e.status='published'),
                    (
                        SELECT count(*)
                        FROM public.member_subscription_finance_event_consumptions c
                        WHERE c.org_id=%s
                    ),
                    (
                        SELECT count(*)
                        FROM public.subscription_terms t
                        WHERE t.org_id=%s
                          AND t.status='active'
                    )
                FROM finance.outbox_events e
                WHERE e.organization_id=%s
                  AND e.aggregate_type='invoice'
                  AND e.event_type='finance.invoice.paid'
                  AND EXISTS (
                      SELECT 1
                      FROM finance.member_subscription_finance_bindings b
                      WHERE b.organization_id=e.organization_id
                        AND b.finance_invoice_id=e.aggregate_id
                  )
                """,
                (pay4.ORG, pay4.ORG, pay4.ORG),
            )
            row = cur.fetchone()
    return (
        int(row[0]),
        float(row[1]),
        int(row[2]),
        int(row[3]),
        int(row[4]),
    )


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", type=int, default=500)
    parser.add_argument("--max-drain-seconds", type=float, default=60.0)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    if not 100 <= args.events <= 1000:
        raise SystemExit("PAY-20 dispatcher events must be in [100,1000]")

    pay5._cleanup_pay5()
    term = pay4._seed_pending()
    pay4._seed_invoice_and_binding(term)
    pay4._finance_event(payment_status="captured", allocated="100.00")

    seed_started = time.perf_counter()
    for index in range(2, args.events + 1):
        _seed_extra(index)
    seed_seconds = time.perf_counter() - seed_started

    before = _eligible_state()
    if before[0] != args.events:
        raise RuntimeError(
            f"PAY-20 eligible backlog seed drift: {before[0]} != {args.events}"
        )

    started = time.perf_counter()
    summary = await dispatcher._poll_finance_events()
    drain_seconds = time.perf_counter() - started
    after = _eligible_state()

    errors: list[str] = []
    if summary["claimed"] != args.events:
        errors.append(
            f"claimed={summary['claimed']} expected={args.events}"
        )
    if summary["delivered"] != args.events:
        errors.append(
            f"delivered={summary['delivered']} expected={args.events}"
        )
    for key in ("retry", "failed", "ack_pending", "lease_lost"):
        if summary[key] != 0:
            errors.append(f"{key}={summary[key]}")
    if after[0] != 0:
        errors.append(f"eligible_backlog_after={after[0]}")
    if after[1] != 0:
        errors.append(f"eligible_oldest_age_after={after[1]}")
    if after[2] != args.events:
        errors.append(f"published={after[2]} expected={args.events}")
    if after[3] != args.events:
        errors.append(f"consumptions={after[3]} expected={args.events}")
    if after[4] != args.events:
        errors.append(f"active_terms={after[4]} expected={args.events}")
    if drain_seconds > args.max_drain_seconds:
        errors.append(
            f"drain_seconds={drain_seconds:.3f} "
            f"exceeds={args.max_drain_seconds:.3f}"
        )

    result = {
        "schema_version": 1,
        "phase": "PAY-20",
        "events": args.events,
        "seed_seconds": round(seed_seconds, 3),
        "drain_seconds": round(drain_seconds, 3),
        "throughput_events_per_second": round(
            args.events / max(drain_seconds, 0.000001),
            3,
        ),
        "max_drain_seconds": args.max_drain_seconds,
        "before": {
            "eligible_backlog": before[0],
            "oldest_age_seconds": round(before[1], 3),
        },
        "after": {
            "eligible_backlog": after[0],
            "oldest_age_seconds": round(after[1], 3),
            "published": after[2],
            "consumptions": after[3],
            "active_terms": after[4],
        },
        "dispatcher_summary": summary,
        "errors": errors,
        "decision": "PASS" if not errors else "FAIL",
    }
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, indent=2, sort_keys=True))

    if errors:
        for error in errors:
            print("PAY-20 dispatcher violation: " + error)
        return 1

    print("PAY20_FINANCE_OUTBOX_DRAIN=PASS")
    print("PAY20_FINANCE_OUTBOX_BACKLOG_AFTER=0")
    print("PAY20_FINANCE_OUTBOX_OLDEST_AGE_AFTER=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
