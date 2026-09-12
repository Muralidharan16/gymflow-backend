#!/usr/bin/env python3
"""Runtime proof that revision 0002 never destroys populated platform state.

The predecessor cannot represent 0002's idempotency, key-rotation, quota, or
outbox data.  This verifier therefore proves that a populated downgrade fails
inside 0002's preflight and leaves both the Alembic revision and seeded data
unchanged.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import uuid

from scripts.verify_00f_adversarial import DatabaseConfig, Scenario


REV_PREDECESSOR = "371b1a44a334"
REV_0002 = "0002_enterprise_platform"
TENANT_ID = uuid.UUID("12121212-1212-4212-8212-121212121212")

EXPECTED_RELATIONS = (
    "active_idempotency_keys",
    "idempotency_store",
    "key_rotation_progress",
    "tenant_resource_quotas",
    "event_outbox",
    "event_outbox_delivery_state",
)


def run(scenario: Scenario) -> dict[str, object]:
    scenario.alembic("upgrade", REV_0002, label="upgrade-0002")
    if scenario.current_revision() != REV_0002:
        raise AssertionError(
            f"{scenario.database}: expected revision {REV_0002} after upgrade"
        )

    with scenario.connect() as conn:
        conn.execute(
            """
            INSERT INTO public.tenant_resource_quotas (
                tenant_id,
                max_writes_per_minute,
                max_outbox_events_per_hour,
                max_storage_bytes
            ) VALUES (%s, 321, 6543, 987654321)
            """,
            (TENANT_ID,),
        )
        before = conn.execute(
            """
            SELECT tenant_id, max_writes_per_minute,
                   max_outbox_events_per_hour, max_storage_bytes
            FROM public.tenant_resource_quotas
            WHERE tenant_id = %s
            """,
            (TENANT_ID,),
        ).fetchone()
        if before is None:
            raise AssertionError(f"{scenario.database}: quota seed was not persisted")

    scenario.alembic(
        "downgrade",
        REV_PREDECESSOR,
        expect_success=False,
        failure_contains=(
            "0002 downgrade would discard populated revision-owned relation "
            "public.tenant_resource_quotas"
        ),
        label="downgrade-populated-must-fail-preflight",
    )

    revision_after = scenario.current_revision()
    if revision_after != REV_0002:
        raise AssertionError(
            f"{scenario.database}: failed downgrade changed revision to {revision_after}"
        )

    with scenario.connect() as conn:
        after = conn.execute(
            """
            SELECT tenant_id, max_writes_per_minute,
                   max_outbox_events_per_hour, max_storage_bytes
            FROM public.tenant_resource_quotas
            WHERE tenant_id = %s
            """,
            (TENANT_ID,),
        ).fetchone()
        if after != before:
            raise AssertionError(
                f"{scenario.database}: populated rollback mutated quota state: "
                f"before={before!r} after={after!r}"
            )

        missing = [
            relation
            for relation in EXPECTED_RELATIONS
            if conn.execute(
                "SELECT to_regclass(%s) AS relation",
                (f"public.{relation}",),
            ).fetchone()["relation"]
            is None
        ]
        if missing:
            raise AssertionError(
                f"{scenario.database}: failed preflight partially removed 0002 objects: {missing}"
            )

    result = {
        "scenario": "0002_populated_downgrade_fails_before_mutation",
        "revision_after_failure": revision_after,
        "seeded_relation": "public.tenant_resource_quotas",
        "seeded_tenant_id": str(TENANT_ID),
        "row_preserved": True,
        "revision_owned_relations_preserved": True,
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
