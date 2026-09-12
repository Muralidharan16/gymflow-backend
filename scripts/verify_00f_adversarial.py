#!/usr/bin/env python3
"""Adversarial runtime verification for the 0009 <-> 00f migration boundary.

This harness intentionally exercises cases that a happy-path lineage cannot
prove: multiple tenants/addresses, a pre-existing branch, cross-tenant FK
rejection, ambiguous branch mapping, and fail-closed rollback after 00f-only
state changes.  It runs Alembic as the reduced ``migration_owner`` identity and
never disables RLS or grants BYPASSRLS.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import psycopg
from psycopg import errors
from psycopg.rows import dict_row


REV_0004 = "0004_google_maps_integration"
REV_0009 = "0009_view_security_invoker"
REV_00F = "00f277c748ea"

ORG_A = uuid.UUID("11111111-1111-4111-8111-111111111111")
ORG_B = uuid.UUID("33333333-3333-4333-8333-333333333333")
ORG_C = uuid.UUID("55555555-5555-4555-8555-555555555555")
ORG_D = uuid.UUID("77777777-7777-4777-8777-777777777777")

ADDR_A1 = uuid.UUID("22222222-2222-4222-8222-222222222221")
ADDR_A2 = uuid.UUID("22222222-2222-4222-8222-222222222222")
ADDR_B = uuid.UUID("44444444-4444-4444-8444-444444444444")
ADDR_C = uuid.UUID("66666666-6666-4666-8666-666666666666")
ADDR_D = uuid.UUID("88888888-8888-4888-8888-888888888888")

BRANCH_B = uuid.UUID("99999999-9999-4999-8999-999999999999")
BRANCH_C1 = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1")
BRANCH_C2 = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2")


@dataclass(frozen=True)
class DatabaseConfig:
    host: str
    port: int
    user: str
    password: str

    def sync_dsn(self, database: str) -> str:
        return (
            f"postgresql://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{database}"
        )

    def async_url(self, database: str) -> str:
        return (
            f"postgresql+asyncpg://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{database}"
        )


class Scenario:
    def __init__(self, config: DatabaseConfig, database: str, evidence_dir: Path) -> None:
        self.config = config
        self.database = database
        self.evidence_dir = evidence_dir / database
        self.evidence_dir.mkdir(parents=True, exist_ok=True)

    def connect(self):
        return psycopg.connect(
            self.config.sync_dsn(self.database),
            autocommit=True,
            row_factory=dict_row,
        )

    def alembic(
        self,
        command: str,
        target: str,
        *,
        expect_success: bool = True,
        failure_contains: str | None = None,
        label: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["DATABASE_URL"] = self.config.async_url(self.database)
        proc = subprocess.run(
            [sys.executable, "-s", "-m", "alembic", "-c", "alembic.ini", command, target],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            check=False,
        )
        name = label or f"{command}-{target}"
        (self.evidence_dir / f"{name}.log").write_text(proc.stdout, encoding="utf-8")

        if expect_success and proc.returncode != 0:
            raise AssertionError(
                f"{self.database}: Alembic {command} {target} failed unexpectedly\n{proc.stdout}"
            )
        if not expect_success:
            if proc.returncode == 0:
                raise AssertionError(
                    f"{self.database}: Alembic {command} {target} unexpectedly succeeded"
                )
            if failure_contains and failure_contains not in proc.stdout:
                raise AssertionError(
                    f"{self.database}: expected failure containing {failure_contains!r}\n{proc.stdout}"
                )
        return proc

    def current_revision(self) -> str:
        with self.connect() as conn:
            row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
            if row is None:
                raise AssertionError(f"{self.database}: alembic_version is empty")
            return str(row["version_num"])


def _set_tenant(conn, org_id: uuid.UUID) -> None:
    conn.execute(
        "SELECT pg_catalog.set_config('app.current_org_id', %s, false)",
        (str(org_id),),
    )


def _deterministic_branch_id(org_id: uuid.UUID, address_id: uuid.UUID) -> uuid.UUID:
    digest = hashlib.md5(
        f"{org_id}:{address_id}-00f-legacy-branch".encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()
    return uuid.UUID(hex=digest)


def _seed_organization(conn, org_id: uuid.UUID, *, name: str, slug: str) -> None:
    conn.execute(
        """
        INSERT INTO public.organizations (
            id, name, tier, is_active, country, profile_completed, slug, business_type
        ) VALUES (%s, %s, 'basic', TRUE, 'India', TRUE, %s, 'gym')
        """,
        (org_id, name, slug),
    )


def _seed_address(
    conn,
    *,
    address_id: uuid.UUID,
    org_id: uuid.UUID,
    label: str,
    line1: str,
    city: str,
    postal_code: str,
    longitude: float,
    latitude: float,
    is_primary: bool,
) -> None:
    conn.execute(
        """
        INSERT INTO public.organization_addresses (
            id, org_id, address_type,
            address_line1, address_line2, city, state_province,
            postal_code, country_code, label,
            effective_from, effective_until,
            is_verified, verified_at, verification_source,
            coordinates, coordinates_source,
            latitude, longitude,
            maps_embed_allowed,
            maps_verification_status, maps_verification_source,
            maps_last_verified_at, maps_updated_at,
            maps_next_retry_at, maps_retry_count,
            is_primary, formatted_address
        ) VALUES (
            %s, %s, 'operational',
            %s, NULL, %s, 'Puducherry',
            %s, 'IN', %s,
            %s, %s,
            TRUE, %s, 'google',
            ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
            'google',
            %s, %s,
            FALSE,
            'verified', 'google_places_api',
            %s, %s,
            %s, 2,
            %s, %s
        )
        """,
        (
            address_id,
            org_id,
            line1,
            city,
            postal_code,
            label,
            datetime(2026, 5, 1, tzinfo=timezone.utc),
            datetime(2027, 5, 1, tzinfo=timezone.utc),
            datetime(2026, 5, 18, 8, tzinfo=timezone.utc),
            longitude,
            latitude,
            latitude,
            longitude,
            datetime(2026, 5, 18, 10, tzinfo=timezone.utc),
            datetime(2026, 5, 18, 9, tzinfo=timezone.utc),
            datetime(2026, 5, 19, 9, tzinfo=timezone.utc),
            is_primary,
            f"{label}, {city} {postal_code}",
        ),
    )


def _seed_existing_branch(
    conn,
    *,
    branch_id: uuid.UUID,
    org_id: uuid.UUID,
    branch_name: str,
    branch_code: str,
    slug: str,
    address_id: uuid.UUID | None,
    is_primary: bool,
) -> None:
    _set_tenant(conn, org_id)
    conn.execute(
        """
        INSERT INTO public.org_branches (
            id, org_id, branch_name, branch_code, internal_slug,
            country_code, address_id, branch_metadata
        ) VALUES (%s, %s, %s, %s, %s, 'IN', %s, %s::jsonb)
        """,
        (
            branch_id,
            org_id,
            branch_name,
            branch_code,
            slug,
            address_id,
            json.dumps({"preexisting": True}),
        ),
    )
    conn.execute(
        """
        INSERT INTO public.org_branch_state (
            branch_id, org_id, branch_status, is_primary,
            is_active, is_public, search_epoch_ulid
        ) VALUES (%s, %s, 'active', %s, TRUE, TRUE, '00000000000000000000000000')
        """,
        (branch_id, org_id, is_primary),
    )


def _branch_snapshot(conn, branch_id: uuid.UUID) -> dict[str, object]:
    row = conn.execute(
        """
        SELECT id, org_id, branch_name, branch_code, internal_slug::text AS internal_slug,
               timezone, currency_code, region_code, country_code, address_id,
               branch_metadata, created_by, created_at, updated_at
        FROM public.org_branches
        WHERE id = %s
        """,
        (branch_id,),
    ).fetchone()
    if row is None:
        raise AssertionError(f"missing branch {branch_id}")
    return dict(row)


def scenario_multi_tenant_roundtrip(scenario: Scenario) -> dict[str, object]:
    scenario.alembic("upgrade", REV_0004, label="01-upgrade-0004")
    with scenario.connect() as conn:
        _seed_organization(conn, ORG_A, name="Adversarial Multi Address Gym", slug="adv-multi-a")
        _seed_address(
            conn,
            address_id=ADDR_A1,
            org_id=ORG_A,
            label="A Primary",
            line1="enc:10 Alpha Road",
            city="Puducherry",
            postal_code="605001",
            longitude=79.8083,
            latitude=11.9416,
            is_primary=True,
        )
        _seed_address(
            conn,
            address_id=ADDR_A2,
            org_id=ORG_A,
            label="A Secondary",
            line1="enc:11 Alpha Road",
            city="Puducherry",
            postal_code="605002",
            longitude=79.8093,
            latitude=11.9426,
            is_primary=False,
        )
        _seed_organization(conn, ORG_B, name="Existing Branch Gym", slug="adv-existing-b")
        _seed_address(
            conn,
            address_id=ADDR_B,
            org_id=ORG_B,
            label="B Existing",
            line1="enc:20 Beta Road",
            city="Puducherry",
            postal_code="605003",
            longitude=79.8103,
            latitude=11.9436,
            is_primary=True,
        )

    scenario.alembic("upgrade", REV_0009, label="02-upgrade-0009")
    with scenario.connect() as conn:
        _seed_existing_branch(
            conn,
            branch_id=BRANCH_B,
            org_id=ORG_B,
            branch_name="Existing Beta Branch",
            branch_code="BETA",
            slug="beta",
            address_id=ADDR_B,
            is_primary=True,
        )
        existing_before = _branch_snapshot(conn, BRANCH_B)

    scenario.alembic("upgrade", REV_00F, label="03-upgrade-00f")

    expected_a1 = _deterministic_branch_id(ORG_A, ADDR_A1)
    expected_a2 = _deterministic_branch_id(ORG_A, ADDR_A2)

    with scenario.connect() as conn:
        # Missing tenant context must fail closed without crashing on empty GUC.
        assert conn.execute("SELECT count(*) AS n FROM public.organization_addresses").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM public.org_branches").fetchone()["n"] == 0

        _set_tenant(conn, ORG_A)
        a_addresses = conn.execute(
            "SELECT id, address_type, branch_id FROM public.organization_addresses ORDER BY id"
        ).fetchall()
        assert len(a_addresses) == 2
        assert {row["address_type"] for row in a_addresses} == {"physical"}
        mapping = {row["id"]: row["branch_id"] for row in a_addresses}
        assert mapping == {ADDR_A1: expected_a1, ADDR_A2: expected_a2}

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
            (ADDR_B,),
        ).fetchone()["n"] == 0
        assert conn.execute(
            "SELECT count(*) AS n FROM public.org_branches WHERE id = %s",
            (BRANCH_B,),
        ).fetchone()["n"] == 0

        # The composite FK must reject a cross-tenant branch assignment even
        # when the caller can update its own tenant's address row.
        try:
            conn.execute(
                "UPDATE public.organization_addresses SET branch_id = %s WHERE id = %s",
                (BRANCH_B, ADDR_A1),
            )
        except errors.ForeignKeyViolation:
            pass
        else:
            raise AssertionError("cross-tenant address->branch assignment unexpectedly succeeded")
        assert conn.execute(
            "SELECT branch_id FROM public.organization_addresses WHERE id = %s",
            (ADDR_A1,),
        ).fetchone()["branch_id"] == expected_a1

        _set_tenant(conn, ORG_B)
        b_address = conn.execute(
            "SELECT branch_id FROM public.organization_addresses WHERE id = %s",
            (ADDR_B,),
        ).fetchone()
        assert b_address is not None and b_address["branch_id"] == BRANCH_B
        assert conn.execute("SELECT count(*) AS n FROM public.org_branches").fetchone()["n"] == 1
        assert conn.execute(
            """
            SELECT count(*) AS n FROM public.org_branches
            WHERE branch_metadata->>'migration_00f_legacy_backfill' = 'true'
            """
        ).fetchone()["n"] == 0

    scenario.alembic("downgrade", REV_0009, label="04-downgrade-0009")
    assert scenario.current_revision() == REV_0009

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

        _set_tenant(conn, ORG_A)
        assert conn.execute("SELECT count(*) AS n FROM public.org_branches").fetchone()["n"] == 0

        _set_tenant(conn, ORG_B)
        assert conn.execute("SELECT count(*) AS n FROM public.org_branches").fetchone()["n"] == 1
        assert _branch_snapshot(conn, BRANCH_B) == existing_before

    scenario.alembic("upgrade", REV_00F, label="05-reupgrade-00f")
    with scenario.connect() as conn:
        _set_tenant(conn, ORG_A)
        remapped = conn.execute(
            "SELECT id, branch_id FROM public.organization_addresses ORDER BY id"
        ).fetchall()
        assert {row["id"]: row["branch_id"] for row in remapped} == {
            ADDR_A1: expected_a1,
            ADDR_A2: expected_a2,
        }
        _set_tenant(conn, ORG_B)
        assert conn.execute(
            "SELECT branch_id FROM public.organization_addresses WHERE id = %s",
            (ADDR_B,),
        ).fetchone()["branch_id"] == BRANCH_B

    return {
        "scenario": "multi_tenant_roundtrip",
        "org_a_branch_ids": sorted([str(expected_a1), str(expected_a2)]),
        "preexisting_branch_preserved": str(BRANCH_B),
        "cross_tenant_fk_rejected": True,
    }


def scenario_ambiguous_mapping_fails_closed(scenario: Scenario) -> dict[str, object]:
    scenario.alembic("upgrade", REV_0004, label="01-upgrade-0004")
    with scenario.connect() as conn:
        _seed_organization(conn, ORG_C, name="Ambiguous Branch Gym", slug="adv-ambiguous-c")
        _seed_address(
            conn,
            address_id=ADDR_C,
            org_id=ORG_C,
            label="Ambiguous Legacy",
            line1="enc:30 Gamma Road",
            city="Puducherry",
            postal_code="605004",
            longitude=79.8113,
            latitude=11.9446,
            is_primary=True,
        )

    scenario.alembic("upgrade", REV_0009, label="02-upgrade-0009")
    with scenario.connect() as conn:
        _seed_existing_branch(
            conn,
            branch_id=BRANCH_C1,
            org_id=ORG_C,
            branch_name="Gamma One",
            branch_code="G1",
            slug="gamma-one",
            address_id=None,
            is_primary=False,
        )
        _seed_existing_branch(
            conn,
            branch_id=BRANCH_C2,
            org_id=ORG_C,
            branch_name="Gamma Two",
            branch_code="G2",
            slug="gamma-two",
            address_id=None,
            is_primary=False,
        )

    scenario.alembic(
        "upgrade",
        REV_00F,
        expect_success=False,
        failure_contains="cannot determine an unambiguous target branch",
        label="03-expected-ambiguous-upgrade-failure",
    )
    assert scenario.current_revision() == REV_0009

    with scenario.connect() as conn:
        assert conn.execute(
            "SELECT address_type FROM public.organization_addresses WHERE id = %s",
            (ADDR_C,),
        ).fetchone()["address_type"] == "operational"
        assert conn.execute(
            "SELECT to_regclass('public.branch_geolocation_state') AS relation_name"
        ).fetchone()["relation_name"] is None
        assert conn.execute(
            """
            SELECT count(*) AS n
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'organization_addresses'
              AND column_name = 'branch_id'
            """
        ).fetchone()["n"] == 0
        _set_tenant(conn, ORG_C)
        branches = conn.execute("SELECT id FROM public.org_branches ORDER BY id").fetchall()
        assert {row["id"] for row in branches} == {BRANCH_C1, BRANCH_C2}

    return {
        "scenario": "ambiguous_mapping_fails_closed",
        "revision_after_failure": REV_0009,
        "partial_schema_changes": False,
    }


def scenario_evolved_state_blocks_downgrade(scenario: Scenario) -> dict[str, object]:
    scenario.alembic("upgrade", REV_0004, label="01-upgrade-0004")
    with scenario.connect() as conn:
        _seed_organization(conn, ORG_D, name="Evolved State Gym", slug="adv-evolved-d")
        _seed_address(
            conn,
            address_id=ADDR_D,
            org_id=ORG_D,
            label="Evolved Legacy",
            line1="enc:40 Delta Road",
            city="Puducherry",
            postal_code="605005",
            longitude=79.8123,
            latitude=11.9456,
            is_primary=True,
        )

    scenario.alembic("upgrade", REV_00F, label="02-upgrade-00f")
    expected_branch = _deterministic_branch_id(ORG_D, ADDR_D)

    with scenario.connect() as conn:
        _set_tenant(conn, ORG_D)
        assert conn.execute(
            "SELECT id FROM public.org_branches WHERE id = %s",
            (expected_branch,),
        ).fetchone() is not None
        conn.execute(
            """
            INSERT INTO public.branch_name_translations (
                branch_id, locale, branch_name, is_default
            ) VALUES (%s, 'en', 'Post migration operator name', TRUE)
            """,
            (expected_branch,),
        )

    scenario.alembic(
        "downgrade",
        REV_0009,
        expect_success=False,
        failure_contains="branch_name_translations",
        label="03-expected-00f-data-downgrade-failure",
    )
    assert scenario.current_revision() == REV_00F

    with scenario.connect() as conn:
        _set_tenant(conn, ORG_D)
        assert conn.execute(
            "SELECT count(*) AS n FROM public.branch_name_translations WHERE branch_id = %s",
            (expected_branch,),
        ).fetchone()["n"] == 1
        assert conn.execute(
            "SELECT id FROM public.org_branches WHERE id = %s",
            (expected_branch,),
        ).fetchone() is not None
        assert conn.execute(
            "SELECT branch_id FROM public.organization_addresses WHERE id = %s",
            (ADDR_D,),
        ).fetchone()["branch_id"] == expected_branch

        conn.execute(
            "DELETE FROM public.branch_name_translations WHERE branch_id = %s",
            (expected_branch,),
        )
        conn.execute(
            "UPDATE public.org_branches SET branch_name = 'Operator Renamed Branch' WHERE id = %s",
            (expected_branch,),
        )

    scenario.alembic(
        "downgrade",
        REV_0009,
        expect_success=False,
        failure_contains="changed after migration",
        label="04-expected-mutated-synthetic-branch-failure",
    )
    assert scenario.current_revision() == REV_00F

    with scenario.connect() as conn:
        _set_tenant(conn, ORG_D)
        row = conn.execute(
            "SELECT branch_name FROM public.org_branches WHERE id = %s",
            (expected_branch,),
        ).fetchone()
        assert row is not None and row["branch_name"] == "Operator Renamed Branch"
        assert conn.execute(
            "SELECT branch_id FROM public.organization_addresses WHERE id = %s",
            (ADDR_D,),
        ).fetchone()["branch_id"] == expected_branch

    return {
        "scenario": "evolved_state_blocks_downgrade",
        "00f_only_data_blocked_before_mutation": True,
        "mutated_synthetic_branch_blocked_before_mutation": True,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--multi-db", required=True)
    parser.add_argument("--ambiguous-db", required=True)
    parser.add_argument("--evolved-db", required=True)
    parser.add_argument("--evidence-dir", type=Path, default=Path("migration-adversarial-evidence"))
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    args.evidence_dir.mkdir(parents=True, exist_ok=True)

    config = DatabaseConfig(
        host=os.environ.get("PGHOST", "127.0.0.1"),
        port=int(os.environ.get("PGPORT", "5432")),
        user=os.environ.get("MIGRATION_USER", "migration_owner"),
        password=os.environ["MIGRATION_PASSWORD"],
    )

    results = [
        scenario_multi_tenant_roundtrip(Scenario(config, args.multi_db, args.evidence_dir)),
        scenario_ambiguous_mapping_fails_closed(Scenario(config, args.ambiguous_db, args.evidence_dir)),
        scenario_evolved_state_blocks_downgrade(Scenario(config, args.evolved_db, args.evidence_dir)),
    ]
    (args.evidence_dir / "summary.json").write_text(
        json.dumps(results, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(results, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
