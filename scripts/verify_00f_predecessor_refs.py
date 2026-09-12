#!/usr/bin/env python3
"""Prove 00f rollback refuses predecessor-owned references before cleanup.

``branch_audit_log`` belongs to revision 0006 and has a composite foreign key to
``org_branches``.  If an audit row references a branch synthesized by 00f, a
rollback to 0009 cannot remove that branch without invalidating predecessor-owned
data.  The migration should detect that condition in its preflight and fail with
an explicit reason before any cleanup mutation is attempted.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import uuid

from scripts import verify_00f_adversarial as base


ORG = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
ADDRESS = uuid.UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
ACTOR = uuid.UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
AUDIT = uuid.UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")


def run(scenario: base.Scenario) -> dict[str, object]:
    scenario.alembic("upgrade", base.REV_0004, label="01-upgrade-0004")
    with scenario.connect() as conn:
        base._seed_organization(
            conn,
            ORG,
            name="Predecessor Reference Gym",
            slug="adv-predecessor-reference",
        )
        base._seed_address(
            conn,
            address_id=ADDRESS,
            org_id=ORG,
            label="Reference Legacy",
            line1="enc:50 Epsilon Road",
            city="Puducherry",
            postal_code="605006",
            longitude=79.8133,
            latitude=11.9466,
            is_primary=True,
        )

    scenario.alembic("upgrade", base.REV_00F, label="02-upgrade-00f")
    branch_id = base._deterministic_branch_id(ORG, ADDRESS)

    with scenario.connect() as conn:
        base._set_tenant(conn, ORG)
        branch = conn.execute(
            "SELECT id, branch_metadata FROM public.org_branches WHERE id = %s",
            (branch_id,),
        ).fetchone()
        assert branch is not None
        assert branch["branch_metadata"] ["migration_00f_legacy_backfill"] is True

        # The predecessor audit table is partitioned; use the bootstrap May-2026
        # partition deliberately so this tests rollback ownership, not partition
        # availability.
        conn.execute(
            """
            INSERT INTO public.branch_audit_log (
                id, branch_id, org_id, actor_id, action, reason, diff, created_at
            ) VALUES (
                %s, %s, %s, %s, 'updated', NULL, %s::jsonb, %s
            )
            """,
            (
                AUDIT,
                branch_id,
                ORG,
                ACTOR,
                json.dumps({"source": "adversarial-predecessor-reference"}),
                datetime(2026, 5, 15, 12, tzinfo=timezone.utc),
            ),
        )
        assert conn.execute(
            "SELECT count(*) AS n FROM public.branch_audit_log WHERE id = %s",
            (AUDIT,),
        ).fetchone()["n"] == 1

    scenario.alembic(
        "downgrade",
        base.REV_0009,
        expect_success=False,
        failure_contains="predecessor-owned branch_audit_log",
        label="03-expected-predecessor-reference-downgrade-failure",
    )
    assert scenario.current_revision() == base.REV_00F

    # Failure must be atomic: the predecessor audit row, synthesized branch,
    # branch state, and 00f address mapping must all still exist afterward.
    with scenario.connect() as conn:
        base._set_tenant(conn, ORG)
        assert conn.execute(
            "SELECT count(*) AS n FROM public.branch_audit_log WHERE id = %s",
            (AUDIT,),
        ).fetchone()["n"] == 1
        assert conn.execute(
            "SELECT count(*) AS n FROM public.org_branches WHERE id = %s",
            (branch_id,),
        ).fetchone()["n"] == 1
        assert conn.execute(
            "SELECT count(*) AS n FROM public.org_branch_state WHERE branch_id = %s",
            (branch_id,),
        ).fetchone()["n"] == 1
        address = conn.execute(
            "SELECT address_type, branch_id FROM public.organization_addresses WHERE id = %s",
            (ADDRESS,),
        ).fetchone()
        assert address is not None
        assert address["address_type"] == "physical"
        assert address["branch_id"] == branch_id

    return {
        "scenario": "predecessor_reference_blocks_rollback",
        "revision_after_failure": base.REV_00F,
        "predecessor_audit_preserved": True,
        "synthesized_branch_preserved": True,
        "failure_was_atomic": True,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=Path("migration-adversarial-evidence"),
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    config = base.DatabaseConfig(
        host=os.environ.get("PGHOST", "127.0.0.1"),
        port=int(os.environ.get("PGPORT", "5432")),
        user=os.environ.get("MIGRATION_USER", "migration_owner"),
        password=os.environ["MIGRATION_PASSWORD"],
    )
    result = run(base.Scenario(config, args.database, args.evidence_dir))
    (args.evidence_dir / "predecessor-reference-summary.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
