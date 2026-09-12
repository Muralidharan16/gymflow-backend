#!/usr/bin/env python3
"""Run the production-safety adversarial 0009 <-> 00f scenarios.

The base harness contains the shared seed/Alembic helpers and the ambiguity and
rollback-refusal scenarios.  This runner makes the multi-tenant case precise:
the cross-tenant attack deliberately changes the source row to ``billing`` in
the same failing statement so the partial ``one physical address per branch``
unique index cannot reject first.  The validated composite tenant FK must be
the invariant that rejects the attack.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from psycopg import errors

from scripts import verify_00f_adversarial as base


def scenario_multi_tenant_roundtrip(scenario: base.Scenario) -> dict[str, object]:
    scenario.alembic("upgrade", base.REV_0004, label="01-upgrade-0004")
    with scenario.connect() as conn:
        base._seed_organization(
            conn,
            base.ORG_A,
            name="Adversarial Multi Address Gym",
            slug="adv-multi-a",
        )
        base._seed_address(
            conn,
            address_id=base.ADDR_A1,
            org_id=base.ORG_A,
            label="A Primary",
            line1="enc:10 Alpha Road",
            city="Puducherry",
            postal_code="605001",
            longitude=79.8083,
            latitude=11.9416,
            is_primary=True,
        )
        base._seed_address(
            conn,
            address_id=base.ADDR_A2,
            org_id=base.ORG_A,
            label="A Secondary",
            line1="enc:11 Alpha Road",
            city="Puducherry",
            postal_code="605002",
            longitude=79.8093,
            latitude=11.9426,
            is_primary=False,
        )
        base._seed_organization(
            conn,
            base.ORG_B,
            name="Existing Branch Gym",
            slug="adv-existing-b",
        )
        base._seed_address(
            conn,
            address_id=base.ADDR_B,
            org_id=base.ORG_B,
            label="B Existing",
            line1="enc:20 Beta Road",
            city="Puducherry",
            postal_code="605003",
            longitude=79.8103,
            latitude=11.9436,
            is_primary=True,
        )

    scenario.alembic("upgrade", base.REV_0009, label="02-upgrade-0009")
    with scenario.connect() as conn:
        base._seed_existing_branch(
            conn,
            branch_id=base.BRANCH_B,
            org_id=base.ORG_B,
            branch_name="Existing Beta Branch",
            branch_code="BETA",
            slug="beta",
            address_id=base.ADDR_B,
            is_primary=True,
        )
        existing_before = base._branch_snapshot(conn, base.BRANCH_B)

    scenario.alembic("upgrade", base.REV_00F, label="03-upgrade-00f")

    expected_a1 = base._deterministic_branch_id(base.ORG_A, base.ADDR_A1)
    expected_a2 = base._deterministic_branch_id(base.ORG_A, base.ADDR_A2)

    with scenario.connect() as conn:
        # Missing tenant context must fail closed and must not crash on an empty
        # custom GUC placeholder.
        assert conn.execute(
            "SELECT count(*) AS n FROM public.organization_addresses"
        ).fetchone()["n"] == 0
        assert conn.execute(
            "SELECT count(*) AS n FROM public.org_branches"
        ).fetchone()["n"] == 0

        base._set_tenant(conn, base.ORG_A)
        a_addresses = conn.execute(
            "SELECT id, address_type, branch_id FROM public.organization_addresses ORDER BY id"
        ).fetchall()
        assert len(a_addresses) == 2
        assert {row["address_type"] for row in a_addresses} == {"physical"}
        assert {row["id"]: row["branch_id"] for row in a_addresses} == {
            base.ADDR_A1: expected_a1,
            base.ADDR_A2: expected_a2,
        }

        a_branches = conn.execute(
            """
            SELECT b.id, s.is_primary
            FROM public.org_branches AS b
            JOIN public.org_branch_state AS s ON s.branch_id = b.id
            ORDER BY b.id
            """
        ).fetchall()
        assert {row["id"] for row in a_branches} == {expected_a1, expected_a2}
        assert sum(bool(row["is_primary"]) for row in a_branches) == 1
        assert conn.execute(
            "SELECT count(*) AS n FROM public.organization_addresses WHERE id = %s",
            (base.ADDR_B,),
        ).fetchone()["n"] == 0
        assert conn.execute(
            "SELECT count(*) AS n FROM public.org_branches WHERE id = %s",
            (base.BRANCH_B,),
        ).fetchone()["n"] == 0

        fk = conn.execute(
            """
            SELECT c.convalidated,
                   pg_catalog.pg_get_constraintdef(c.oid, true) AS definition
            FROM pg_catalog.pg_constraint AS c
            JOIN pg_catalog.pg_class AS rel ON rel.oid = c.conrelid
            JOIN pg_catalog.pg_namespace AS ns ON ns.oid = rel.relnamespace
            WHERE ns.nspname = 'public'
              AND rel.relname = 'organization_addresses'
              AND c.conname = 'fk_org_addresses_branch_org'
              AND c.contype = 'f'
            """
        ).fetchone()
        assert fk is not None
        assert fk["convalidated"] is True
        assert fk["definition"] == (
            "FOREIGN KEY (branch_id, org_id) "
            "REFERENCES org_branches(id, org_id) ON DELETE RESTRICT"
        )

        # A plain physical->physical cross-tenant assignment is already denied
        # by uq_one_physical_per_branch because ORG_B's branch has a physical
        # address.  Change the source row to billing in the same statement so
        # that partial index is not applicable.  The composite tenant FK must
        # now be the rejecting invariant.  The failed statement is atomic.
        try:
            conn.execute(
                """
                UPDATE public.organization_addresses
                SET address_type = 'billing', branch_id = %s
                WHERE id = %s
                """,
                (base.BRANCH_B, base.ADDR_A1),
            )
        except errors.ForeignKeyViolation:
            pass
        else:
            raise AssertionError(
                "validated composite tenant FK did not reject cross-tenant address->branch assignment"
            )

        unchanged = conn.execute(
            "SELECT address_type, branch_id FROM public.organization_addresses WHERE id = %s",
            (base.ADDR_A1,),
        ).fetchone()
        assert unchanged is not None
        assert unchanged["address_type"] == "physical"
        assert unchanged["branch_id"] == expected_a1

        base._set_tenant(conn, base.ORG_B)
        b_address = conn.execute(
            "SELECT branch_id FROM public.organization_addresses WHERE id = %s",
            (base.ADDR_B,),
        ).fetchone()
        assert b_address is not None and b_address["branch_id"] == base.BRANCH_B
        assert conn.execute(
            "SELECT count(*) AS n FROM public.org_branches"
        ).fetchone()["n"] == 1
        assert conn.execute(
            """
            SELECT count(*) AS n
            FROM public.org_branches
            WHERE branch_metadata->>'migration_00f_legacy_backfill' = 'true'
            """
        ).fetchone()["n"] == 0

    scenario.alembic("downgrade", base.REV_0009, label="04-downgrade-0009")
    assert scenario.current_revision() == base.REV_0009

    with scenario.connect() as conn:
        assert conn.execute(
            """
            SELECT count(*) AS n
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'organization_addresses'
              AND column_name = 'branch_id'
            """
        ).fetchone()["n"] == 0
        restored = conn.execute(
            "SELECT id, address_type FROM public.organization_addresses ORDER BY id"
        ).fetchall()
        assert len(restored) == 3
        assert {row["address_type"] for row in restored} == {"operational"}

        base._set_tenant(conn, base.ORG_A)
        assert conn.execute(
            "SELECT count(*) AS n FROM public.org_branches"
        ).fetchone()["n"] == 0

        base._set_tenant(conn, base.ORG_B)
        assert conn.execute(
            "SELECT count(*) AS n FROM public.org_branches"
        ).fetchone()["n"] == 1
        assert base._branch_snapshot(conn, base.BRANCH_B) == existing_before

    scenario.alembic("upgrade", base.REV_00F, label="05-reupgrade-00f")
    with scenario.connect() as conn:
        base._set_tenant(conn, base.ORG_A)
        remapped = conn.execute(
            "SELECT id, branch_id FROM public.organization_addresses ORDER BY id"
        ).fetchall()
        assert {row["id"]: row["branch_id"] for row in remapped} == {
            base.ADDR_A1: expected_a1,
            base.ADDR_A2: expected_a2,
        }

        base._set_tenant(conn, base.ORG_B)
        assert conn.execute(
            "SELECT branch_id FROM public.organization_addresses WHERE id = %s",
            (base.ADDR_B,),
        ).fetchone()["branch_id"] == base.BRANCH_B
        assert base._branch_snapshot(conn, base.BRANCH_B) == existing_before

    return {
        "scenario": "multi_tenant_roundtrip",
        "org_a_branch_ids": sorted([str(expected_a1), str(expected_a2)]),
        "preexisting_branch_preserved": str(base.BRANCH_B),
        "composite_fk_validated": True,
        "cross_tenant_fk_rejected": True,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--multi-db", required=True)
    parser.add_argument("--ambiguous-db", required=True)
    parser.add_argument("--evolved-db", required=True)
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

    results = [
        scenario_multi_tenant_roundtrip(
            base.Scenario(config, args.multi_db, args.evidence_dir)
        ),
        base.scenario_ambiguous_mapping_fails_closed(
            base.Scenario(config, args.ambiguous_db, args.evidence_dir)
        ),
        base.scenario_evolved_state_blocks_downgrade(
            base.Scenario(config, args.evolved_db, args.evidence_dir)
        ),
    ]
    (args.evidence_dir / "summary.json").write_text(
        json.dumps(results, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(results, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
