#!/usr/bin/env python3
"""Prove 0004 blocks durable Maps loss while allowing cache-only rollback."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import uuid

from scripts.verify_00f_adversarial import (
    DatabaseConfig,
    Scenario,
    _seed_address,
    _seed_organization,
)


REV_0003 = "0003_security_schemas"
REV_0004 = "0004_google_maps_integration"

ORG_ID = uuid.UUID("18181818-1818-4818-8818-181818181818")
ADDRESS_ID = uuid.UUID("19191919-1919-4919-8919-191919191919")
CACHE_PLACE_ID = "maps-cache-only-place"


def run(scenario: Scenario) -> dict[str, object]:
    scenario.alembic("upgrade", REV_0004, label="upgrade-0004")
    if scenario.current_revision() != REV_0004:
        raise AssertionError(
            f"{scenario.database}: expected revision {REV_0004} after upgrade"
        )

    with scenario.connect() as conn:
        _seed_organization(
            conn,
            ORG_ID,
            name="Maps Rollback Gym",
            slug="maps-rollback-gym",
        )
        _seed_address(
            conn,
            address_id=ADDRESS_ID,
            org_id=ORG_ID,
            label="Maps Verified Address",
            line1="enc:40 Maps Road",
            city="Puducherry",
            postal_code="605005",
            longitude=79.8123,
            latitude=11.9456,
            is_primary=True,
        )
        conn.execute(
            """
            INSERT INTO public.google_places_cache (
                place_id, latitude, longitude,
                formatted_address, place_name, place_types
            ) VALUES (
                %s, 11.9456, 79.8123,
                'Cache only, Puducherry', 'Cache only', '["gym"]'::jsonb
            )
            """,
            (CACHE_PLACE_ID,),
        )
        maps_before = conn.execute(
            """
            SELECT latitude, longitude, maps_verification_status,
                   maps_verification_source, maps_retry_count
            FROM public.organization_addresses
            WHERE id = %s
            """,
            (ADDRESS_ID,),
        ).fetchone()
        if maps_before is None:
            raise AssertionError(f"{scenario.database}: Maps seed was not persisted")

    scenario.alembic(
        "downgrade",
        REV_0003,
        expect_success=False,
        failure_contains=(
            "0004 downgrade would discard populated durable Google Maps state "
            "from organization_addresses"
        ),
        label="downgrade-durable-maps-must-fail-preflight",
    )

    if scenario.current_revision() != REV_0004:
        raise AssertionError(f"{scenario.database}: failed downgrade changed revision")

    with scenario.connect() as conn:
        maps_after_failure = conn.execute(
            """
            SELECT latitude, longitude, maps_verification_status,
                   maps_verification_source, maps_retry_count
            FROM public.organization_addresses
            WHERE id = %s
            """,
            (ADDRESS_ID,),
        ).fetchone()
        if maps_after_failure != maps_before:
            raise AssertionError(
                f"{scenario.database}: rejected rollback mutated durable Maps state"
            )
        cache_count = conn.execute(
            "SELECT count(*) AS n FROM public.google_places_cache WHERE place_id = %s",
            (CACHE_PLACE_ID,),
        ).fetchone()["n"]
        if cache_count != 1:
            raise AssertionError(
                f"{scenario.database}: rejected rollback mutated cache before preflight exit"
            )

        # Reset only revision-0004 durable columns to the exact predecessor-
        # representable defaults. Cache remains populated intentionally.
        conn.execute(
            """
            UPDATE public.organization_addresses
            SET google_place_id = NULL,
                latitude = NULL,
                longitude = NULL,
                maps_embed_allowed = TRUE,
                maps_verification_status = 'pending',
                maps_last_verified_at = NULL,
                maps_verification_error = NULL,
                maps_verification_source = NULL,
                maps_updated_at = NULL,
                maps_next_retry_at = NULL,
                maps_retry_count = 0
            WHERE id = %s
            """,
            (ADDRESS_ID,),
        )

    scenario.alembic(
        "downgrade",
        REV_0003,
        label="downgrade-cache-only-may-succeed",
    )
    if scenario.current_revision() != REV_0003:
        raise AssertionError(
            f"{scenario.database}: cache-only downgrade did not reach {REV_0003}"
        )

    with scenario.connect() as conn:
        address_count = conn.execute(
            "SELECT count(*) AS n FROM public.organization_addresses WHERE id = %s",
            (ADDRESS_ID,),
        ).fetchone()["n"]
        if address_count != 1:
            raise AssertionError(
                f"{scenario.database}: predecessor address row was not preserved"
            )
        if conn.execute(
            "SELECT to_regclass('public.google_places_cache') AS relation"
        ).fetchone()["relation"] is not None:
            raise AssertionError(
                f"{scenario.database}: rebuildable cache relation survived 0004 downgrade"
            )
        maps_columns = conn.execute(
            """
            SELECT count(*) AS n
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'organization_addresses'
              AND column_name IN (
                  'google_place_id', 'latitude', 'longitude',
                  'maps_embed_allowed', 'maps_verification_status',
                  'maps_last_verified_at', 'maps_verification_error',
                  'maps_verification_source', 'maps_updated_at',
                  'maps_next_retry_at', 'maps_retry_count'
              )
            """
        ).fetchone()["n"]
        if maps_columns != 0:
            raise AssertionError(
                f"{scenario.database}: 0004-owned Maps columns remain after downgrade"
            )

    result = {
        "scenario": "0004_durable_maps_block_cache_only_allows_rollback",
        "durable_maps_blocked_before_mutation": True,
        "predecessor_address_preserved": True,
        "cache_only_state_discarded_as_rebuildable": True,
        "revision_after_successful_cache_only_rollback": REV_0003,
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
