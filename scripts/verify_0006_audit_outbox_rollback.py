#!/usr/bin/env python3
"""Prove revision 0006 refuses audit/outbox loss and restores 0005 exactly."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import uuid

from scripts.verify_00f_adversarial import (
    DatabaseConfig,
    Scenario,
    _seed_existing_branch,
    _seed_organization,
)


REV_0005 = "0005_enterprise_branches"
REV_0006 = "0006_branch_security_audit"

ORG_ID = uuid.UUID("20202020-2020-4020-8020-202020202020")
BRANCH_ID = uuid.UUID("21212121-2121-4121-8121-212121212121")
ACTOR_ID = uuid.UUID("22222222-aaaa-4222-8222-222222222222")
AUDIT_ID = uuid.UUID("23232323-2323-4323-8323-232323232323")
EVENT_ID = uuid.UUID("24242424-2424-4424-8424-242424242424")


def run(scenario: Scenario) -> dict[str, object]:
    scenario.alembic("upgrade", REV_0006, label="upgrade-0006")
    if scenario.current_revision() != REV_0006:
        raise AssertionError(
            f"{scenario.database}: expected revision {REV_0006} after upgrade"
        )

    with scenario.connect() as conn:
        _seed_organization(
            conn,
            ORG_ID,
            name="Audit Outbox Rollback Gym",
            slug="audit-outbox-rollback-gym",
        )
        _seed_existing_branch(
            conn,
            branch_id=BRANCH_ID,
            org_id=ORG_ID,
            branch_name="Audit Branch",
            branch_code="AUDIT",
            slug="audit",
            address_id=None,
            is_primary=True,
        )
        conn.execute(
            """
            INSERT INTO public.branch_audit_log (
                id, branch_id, org_id, actor_id,
                action, reason, diff, created_at
            ) VALUES (
                %s, %s, %s, %s,
                'updated', NULL, '{"field":"branch_name"}'::jsonb,
                '2026-05-20T10:00:00+00'::timestamptz
            )
            """,
            (AUDIT_ID, BRANCH_ID, ORG_ID, ACTOR_ID),
        )
        conn.execute(
            """
            INSERT INTO public.outbox_events (
                event_id, aggregate_type, aggregate_id, payload, created_at
            ) VALUES (
                %s, 'branch', %s, '{"event":"branch.updated"}'::jsonb,
                '2026-05-20T10:05:00+00'::timestamptz
            )
            """,
            (EVENT_ID, BRANCH_ID),
        )

    scenario.alembic(
        "downgrade",
        REV_0005,
        expect_success=False,
        failure_contains=(
            "0006 downgrade would discard populated audit/outbox relation "
            "public.branch_audit_log"
        ),
        label="downgrade-audit-must-fail-preflight",
    )
    if scenario.current_revision() != REV_0006:
        raise AssertionError(f"{scenario.database}: audit failure changed revision")

    with scenario.connect() as conn:
        if conn.execute(
            "SELECT count(*) AS n FROM public.branch_audit_log WHERE id = %s",
            (AUDIT_ID,),
        ).fetchone()["n"] != 1:
            raise AssertionError(f"{scenario.database}: audit row was lost on rejected rollback")
        if conn.execute(
            "SELECT count(*) AS n FROM public.outbox_events WHERE event_id = %s",
            (EVENT_ID,),
        ).fetchone()["n"] != 1:
            raise AssertionError(f"{scenario.database}: outbox row was lost on rejected rollback")

        # Test-only state arrangement: remove only the audit seed so the outbox
        # guard is exercised independently on the same isolated database.
        conn.execute("DELETE FROM public.branch_audit_log WHERE id = %s", (AUDIT_ID,))

    scenario.alembic(
        "downgrade",
        REV_0005,
        expect_success=False,
        failure_contains=(
            "0006 downgrade would discard populated audit/outbox relation "
            "public.outbox_events"
        ),
        label="downgrade-outbox-must-fail-preflight",
    )
    if scenario.current_revision() != REV_0006:
        raise AssertionError(f"{scenario.database}: outbox failure changed revision")

    with scenario.connect() as conn:
        if conn.execute(
            "SELECT count(*) AS n FROM public.outbox_events WHERE event_id = %s",
            (EVENT_ID,),
        ).fetchone()["n"] != 1:
            raise AssertionError(f"{scenario.database}: outbox row was lost on rejected rollback")
        if conn.execute(
            "SELECT to_regprocedure('public.create_next_month_partition(text,text[])') AS procedure"
        ).fetchone()["procedure"] is None:
            raise AssertionError(f"{scenario.database}: partition helper disappeared before teardown")

        # Test-only drain proves the empty inverse after both independent
        # populated blockers have been exercised.
        conn.execute("DELETE FROM public.outbox_events WHERE event_id = %s", (EVENT_ID,))

    scenario.alembic("downgrade", REV_0005, label="downgrade-drained-0006")
    if scenario.current_revision() != REV_0005:
        raise AssertionError(
            f"{scenario.database}: drained rollback did not reach {REV_0005}"
        )

    with scenario.connect() as conn:
        if conn.execute(
            "SELECT count(*) AS n FROM public.org_branches WHERE id = %s",
            (BRANCH_ID,),
        ).fetchone()["n"] != 1:
            raise AssertionError(f"{scenario.database}: predecessor branch was not preserved")

        for relation in ("branch_audit_log", "outbox_events"):
            if conn.execute(
                "SELECT to_regclass(%s) AS relation",
                (f"public.{relation}",),
            ).fetchone()["relation"] is not None:
                raise AssertionError(
                    f"{scenario.database}: 0006-owned relation {relation} survived downgrade"
                )

        if conn.execute(
            "SELECT to_regprocedure('public.create_next_month_partition(text,text[])') AS procedure"
        ).fetchone()["procedure"] is not None:
            raise AssertionError(f"{scenario.database}: 0006 partition helper survived downgrade")

        rls_rows = conn.execute(
            """
            SELECT relname, relrowsecurity, relforcerowsecurity
            FROM pg_catalog.pg_class AS relation_data
            JOIN pg_catalog.pg_namespace AS namespace_data
              ON namespace_data.oid = relation_data.relnamespace
            WHERE namespace_data.nspname = 'public'
              AND relation_data.relname IN ('org_branches', 'org_branch_state')
            ORDER BY relname
            """
        ).fetchall()
        if len(rls_rows) != 2 or any(
            row["relrowsecurity"] or row["relforcerowsecurity"] for row in rls_rows
        ):
            raise AssertionError(
                f"{scenario.database}: 0005 RLS posture was not restored: {rls_rows!r}"
            )

    result = {
        "scenario": "0006_audit_outbox_fail_closed_then_restore_0005",
        "audit_history_blocked_before_mutation": True,
        "outbox_state_blocked_before_mutation": True,
        "partition_helper_removed_only_after_safe_drain": True,
        "predecessor_branch_preserved": True,
        "predecessor_rls_posture_restored": True,
    }
    (scenario.evidence_dir / "result.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    args = parser.parse_args()

    config = DatabaseConfig(
        host=os.environ.get("PGHOST", "127.0.0.1"),
        port=int(os.environ.get("PGPORT", "5432")),
        user=os.environ.get("MIGRATION_USER", "migration_owner"),
        password=os.environ["MIGRATION_PASSWORD"],
    )
    result = run(Scenario(config, args.database, args.evidence_dir))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
