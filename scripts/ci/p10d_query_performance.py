#!/usr/bin/env python3
"""P10-D real query-plan, index, N+1 and pagination evidence."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import uuid
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psycopg
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
BUDGET_PATH = ROOT / "docs/architecture/p10_performance_budgets.v1.json"
DIGEST_PATH = ROOT / "docs/architecture/p10_performance_budgets.v1.sha256"

ORG_ID = str(uuid.UUID(hashlib.md5(b"p9m-org-1").hexdigest()))
OWNER_ID = str(uuid.UUID(hashlib.md5(b"p10b-owner-1").hexdigest()))
PHONE = "8" + str(1 * 100000 + 1).rjust(9, "0")


def _connect_sync():
    raw = os.environ["P10D_APP_DATABASE_URL"]
    parsed = urlparse(raw)
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise RuntimeError(f"unsafe P10-D database host: {parsed.hostname}")
    return psycopg.connect(
        host=parsed.hostname,
        port=parsed.port or 5432,
        dbname=parsed.path.lstrip("/"),
        user=parsed.username,
        password=parsed.password,
    )


def _install_rls_context(cursor) -> None:
    settings = {
        "app.current_org_id": ORG_ID,
        "app.current_user_id": OWNER_ID,
        "app.current_user": OWNER_ID,
        "app.current_principal_type": "owner",
        "app.current_role": "owner",
        "app.request_id": "p10d-query-plan",
    }
    for key, value in settings.items():
        cursor.execute("SELECT pg_catalog.set_config(%s,%s,true)", (key, value))


def _json_plan(cursor, sql: str, params: tuple[Any, ...]) -> dict[str, Any]:
    cursor.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, params)
    raw = cursor.fetchone()[0]
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, list) or len(raw) != 1:
        raise RuntimeError(f"unexpected EXPLAIN payload: {type(raw)!r}")
    return raw[0]


def _walk_plan(node: dict[str, Any]):
    yield node
    for child in node.get("Plans", []) or []:
        yield from _walk_plan(child)


def _plan_summary(name: str, explain: dict[str, Any]) -> dict[str, Any]:
    plan = explain["Plan"]
    nodes = list(_walk_plan(plan))
    return {
        "name": name,
        "planning_time_ms": round(float(explain.get("Planning Time", 0.0)), 3),
        "execution_time_ms": round(float(explain.get("Execution Time", 0.0)), 3),
        "root_node": plan.get("Node Type"),
        "actual_rows": int(plan.get("Actual Rows", 0)),
        "node_types": sorted(Counter(str(n.get("Node Type")) for n in nodes).items()),
        "indexes_used": sorted(
            {
                str(n["Index Name"])
                for n in nodes
                if n.get("Index Name")
            }
        ),
        "relations": sorted(
            {
                str(n["Relation Name"])
                for n in nodes
                if n.get("Relation Name")
            }
        ),
        "shared_hit_blocks": sum(int(n.get("Shared Hit Blocks", 0) or 0) for n in nodes),
        "shared_read_blocks": sum(int(n.get("Shared Read Blocks", 0) or 0) for n in nodes),
        "temp_read_blocks": sum(int(n.get("Temp Read Blocks", 0) or 0) for n in nodes),
        "temp_written_blocks": sum(int(n.get("Temp Written Blocks", 0) or 0) for n in nodes),
    }


def collect_database_evidence(output_dir: Path) -> dict[str, Any]:
    budgets = json.loads(BUDGET_PATH.read_text(encoding="utf-8"))
    http_cap_ms = float(budgets["budgets"]["representative_http"]["max_overall_p95_ms"])
    digest = DIGEST_PATH.read_text(encoding="utf-8").strip()
    errors: list[str] = []

    with _connect_sync() as connection:
        with connection.cursor() as cursor:
            _install_rls_context(cursor)
            cursor.execute("ANALYZE public.members")
            cursor.execute("ANALYZE public.member_subscriptions_v2")
            cursor.execute("ANALYZE public.org_branches")

            queries = {
                "tenant_count": (
                    """
                    SELECT count(*)
                    FROM public.members AS m
                    WHERE m.org_id=%s::uuid AND m.is_active=true
                    """,
                    (ORG_ID,),
                ),
                "tenant_page": (
                    """
                    SELECT
                        m.id,m.member_number,m.name,m.phone,m.status,
                        b.branch_name,
                        active_subscription.active_subscription_id
                    FROM public.members AS m
                    LEFT JOIN public.org_branches AS b
                      ON b.id=m.home_branch_id
                    LEFT JOIN (
                        SELECT s.primary_member_id AS member_id,
                               s.id AS active_subscription_id
                        FROM public.member_subscriptions_v2 AS s
                        WHERE s.org_id=%s::uuid AND s.status='active'
                    ) AS active_subscription
                      ON active_subscription.member_id=m.id
                    WHERE m.org_id=%s::uuid AND m.is_active=true
                    ORDER BY m.member_number,m.name
                    LIMIT 50 OFFSET 0
                    """,
                    (ORG_ID, ORG_ID),
                ),
                "tenant_phone_lookup": (
                    """
                    SELECT m.id,m.member_number,m.name,m.phone
                    FROM public.members AS m
                    WHERE m.org_id=%s::uuid
                      AND m.phone=%s
                      AND m.is_active=true
                    LIMIT 2
                    """,
                    (ORG_ID, PHONE),
                ),
            }

            plans: dict[str, dict[str, Any]] = {}
            for name, (sql, params) in queries.items():
                explain = _json_plan(cursor, sql, params)
                (output_dir / f"{name}-explain.json").write_text(
                    json.dumps(explain, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                summary = _plan_summary(name, explain)
                plans[name] = summary
                if summary["execution_time_ms"] > http_cap_ms:
                    errors.append(
                        f"{name} execution {summary['execution_time_ms']}ms exceeds "
                        f"frozen HTTP p95 ceiling {http_cap_ms}ms"
                    )
                if summary["temp_read_blocks"] or summary["temp_written_blocks"]:
                    errors.append(f"{name} spilled to temporary blocks")

            if plans["tenant_page"]["actual_rows"] > 50:
                errors.append("tenant_page exceeded bounded page size 50")
            if plans["tenant_phone_lookup"]["actual_rows"] > 1:
                errors.append("tenant_phone_lookup returned more than one synthetic match")

            page_indexes = set(plans["tenant_page"]["indexes_used"])
            phone_indexes = set(plans["tenant_phone_lookup"]["indexes_used"])
            if "uq_members_org_member_number" not in page_indexes:
                errors.append(
                    "tenant_page did not use the organization/member-number ordering index"
                )
            if "ix_members_org_phone" not in phone_indexes:
                errors.append("tenant_phone_lookup did not use ix_members_org_phone")

            cursor.execute(
                """
                SELECT
                    ns.nspname,
                    tbl.relname,
                    idx.relname,
                    i.indisprimary,
                    i.indisunique,
                    i.indisvalid,
                    i.indisready,
                    i.indkey::text,
                    pg_catalog.pg_get_expr(i.indexprs,i.indrelid,true),
                    pg_catalog.pg_get_expr(i.indpred,i.indrelid,true),
                    pg_catalog.pg_get_indexdef(i.indexrelid),
                    coalesce(s.idx_scan,0)
                FROM pg_catalog.pg_index AS i
                JOIN pg_catalog.pg_class AS idx ON idx.oid=i.indexrelid
                JOIN pg_catalog.pg_class AS tbl ON tbl.oid=i.indrelid
                JOIN pg_catalog.pg_namespace AS ns ON ns.oid=tbl.relnamespace
                LEFT JOIN pg_catalog.pg_stat_user_indexes AS s
                  ON s.indexrelid=i.indexrelid
                WHERE ns.nspname='public'
                  AND tbl.relname IN ('members','member_subscriptions_v2','org_branches')
                ORDER BY tbl.relname,idx.relname
                """
            )
            inventory = []
            signatures: dict[tuple[Any, ...], list[str]] = {}
            for row in cursor.fetchall():
                (
                    schema, table, index_name, primary, unique, valid, ready,
                    indkey, expressions, predicate, definition, idx_scan
                ) = row
                item = {
                    "schema": schema,
                    "table": table,
                    "index": index_name,
                    "primary": bool(primary),
                    "unique": bool(unique),
                    "valid": bool(valid),
                    "ready": bool(ready),
                    "indkey": indkey,
                    "expressions": expressions,
                    "predicate": predicate,
                    "definition": definition,
                    "idx_scan": int(idx_scan),
                    "fresh_ci_usage_review": (
                        "USED" if int(idx_scan) > 0
                        else "UNUSED_IN_FRESH_CI_NOT_REMOVAL_SIGNAL"
                    ),
                }
                inventory.append(item)
                signature = (
                    table, bool(primary), bool(unique), indkey,
                    expressions or "", predicate or "",
                )
                signatures.setdefault(signature, []).append(index_name)
                if not valid or not ready:
                    errors.append(f"index not valid/ready: {table}.{index_name}")

            duplicates = [
                {"indexes": names, "signature": list(signature)}
                for signature, names in signatures.items()
                if len(names) > 1
            ]
            if duplicates:
                errors.append(f"exact duplicate critical indexes detected: {duplicates!r}")

    (output_dir / "index-inventory.json").write_text(
        json.dumps(
            {
                "indexes": inventory,
                "exact_duplicate_signatures": duplicates,
                "review_note": (
                    "idx_scan=0 in a fresh disposable database is inventory evidence only; "
                    "it is not sufficient evidence to remove an index."
                ),
            },
            indent=2,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )
    return {
        "budget_digest": digest,
        "frozen_http_plan_ceiling_ms": http_cap_ms,
        "plans": plans,
        "index_count": len(inventory),
        "duplicate_index_signatures": duplicates,
        "errors": errors,
    }


async def collect_query_count_evidence() -> dict[str, Any]:
    from app.core.database import update_session_context
    from app.services.member_service import MemberService

    database_url = os.environ["P10D_DATABASE_URL"]
    engine = create_async_engine(database_url, poolclass=NullPool, pool_pre_ping=True)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def run_page(size: int) -> dict[str, Any]:
        statements: list[str] = []

        def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
            normalized = " ".join(statement.split())
            if "pg_catalog.set_config" not in normalized:
                statements.append(normalized)

        event.listen(engine.sync_engine, "before_cursor_execute", before_cursor_execute)
        try:
            async with maker() as session:
                await update_session_context(
                    session,
                    principal_id=OWNER_ID,
                    principal_type="owner",
                    org_id=ORG_ID,
                    trace_id=f"p10d-page-{size}",
                    role="owner",
                    request_id=f"p10d-page-{size}",
                )
                members, total = await MemberService(session).list_members_org(
                    uuid.UUID(ORG_ID),
                    page=1,
                    size=size,
                )
                await session.rollback()
            return {
                "page_size": size,
                "returned": len(members),
                "total": int(total),
                "business_query_count": len(statements),
                "statement_shapes": statements,
            }
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", before_cursor_execute)

    try:
        small = await run_page(1)
        representative = await run_page(50)
    finally:
        await engine.dispose()

    errors: list[str] = []
    if small["returned"] != 1:
        errors.append(f"page_size=1 returned {small['returned']}")
    if representative["returned"] != 50:
        errors.append(f"page_size=50 returned {representative['returned']}")
    if small["total"] != 500 or representative["total"] != 500:
        errors.append(
            f"unexpected tenant cardinality: {small['total']}/{representative['total']}"
        )
    if small["business_query_count"] != 3:
        errors.append(
            f"page_size=1 expected 3 fixed business SQL statements, got "
            f"{small['business_query_count']}"
        )
    if representative["business_query_count"] != 3:
        errors.append(
            f"page_size=50 expected 3 fixed business SQL statements, got "
            f"{representative['business_query_count']}"
        )
    if small["business_query_count"] != representative["business_query_count"]:
        errors.append("query count grows with returned row count (N+1 regression)")

    return {
        "page_size_1": small,
        "page_size_50": representative,
        "fixed_query_count": (
            small["business_query_count"] == representative["business_query_count"]
        ),
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    database = collect_database_evidence(output_dir)
    query_count = asyncio.run(collect_query_count_evidence())
    errors = list(database["errors"]) + list(query_count["errors"])

    record = {
        "schema_version": 1,
        "phase": "P10-D",
        "synthetic_only": True,
        "database": database,
        "query_count_n_plus_one": query_count,
        "pagination": {
            "representative_page_size": 50,
            "maximum_route_page_size": 200,
            "bounded": True,
        },
        "provider_execution": "DEFERRED_FAIL_CLOSED",
        "decision": "PASS" if not errors else "FAIL",
        "errors": errors,
    }
    (output_dir / "decision.json").write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if errors:
        for error in errors:
            print(f"P10-D violation: {error}")
        return 1

    print(json.dumps(record, indent=2, sort_keys=True))
    print("P10_QUERY_PERFORMANCE=PASS")
    print("P10_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
