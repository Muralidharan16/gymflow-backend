#!/usr/bin/env python3
"""Prove that populated 0003 security/audit state blocks rollback preflight."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import uuid

from scripts.verify_00f_adversarial import DatabaseConfig, Scenario


REV_0002 = "0002_enterprise_platform"
REV_0003 = "0003_security_schemas"

TENANT_ID = uuid.UUID("13131313-1313-4313-8313-131313131313")
ORG_ID = uuid.UUID("14141414-1414-4414-8414-141414141414")
ADDRESS_ID = uuid.UUID("15151515-1515-4515-8515-151515151515")
ACTOR_ID = uuid.UUID("16161616-1616-4616-8616-161616161616")
ENTITY_ID = uuid.UUID("17171717-1717-4717-8717-171717171717")

EXPECTED_RELATIONS = (
    "encryption_key_registry",
    "organization_address_payloads_secure",
    "address_audit_ledger",
    "audit_chain_heads",
)


def run(scenario: Scenario) -> dict[str, object]:
    scenario.alembic("upgrade", REV_0003, label="upgrade-0003")
    if scenario.current_revision() != REV_0003:
        raise AssertionError(
            f"{scenario.database}: expected revision {REV_0003} after upgrade"
        )

    with scenario.connect() as conn:
        key_row = conn.execute(
            """
            INSERT INTO public.encryption_key_registry (
                tenant_id, table_name, encrypted_dek
            ) VALUES (%s, 'organization_addresses', %s)
            RETURNING key_version
            """,
            (TENANT_ID, b"encrypted-dek-v1"),
        ).fetchone()
        if key_row is None:
            raise AssertionError(f"{scenario.database}: failed to seed encryption key")
        key_version = int(key_row["key_version"])

        payload_row = conn.execute(
            """
            INSERT INTO public.organization_address_payloads_secure (
                tenant_id, org_id, address_id, payload_encrypted, key_version
            ) VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (TENANT_ID, ORG_ID, ADDRESS_ID, b"ciphertext-v1", key_version),
        ).fetchone()
        if payload_row is None:
            raise AssertionError(f"{scenario.database}: failed to seed encrypted payload")
        payload_id = payload_row["id"]

        ledger_row = conn.execute(
            """
            INSERT INTO public.address_audit_ledger (
                tenant_id, entity_id, entity_type, event_type,
                changed_by, payload_hash, chain_hmac, metadata
            ) VALUES (
                %s, %s, 'organization_address', 'created',
                %s, %s, %s, '{"source":"migration-adversarial"}'::jsonb
            )
            RETURNING id
            """,
            (TENANT_ID, ENTITY_ID, ACTOR_ID, "a" * 64, "b" * 64),
        ).fetchone()
        if ledger_row is None:
            raise AssertionError(f"{scenario.database}: failed to seed audit ledger")
        ledger_id = int(ledger_row["id"])

        conn.execute(
            """
            INSERT INTO public.audit_chain_heads (
                tenant_id, entity_id, entity_type, last_ledger_id, last_hmac
            ) VALUES (%s, %s, 'organization_address', %s, %s)
            """,
            (TENANT_ID, ENTITY_ID, ledger_id, "b" * 64),
        )

    scenario.alembic(
        "downgrade",
        REV_0002,
        expect_success=False,
        failure_contains=(
            "0003 downgrade would discard populated security/audit relation "
            "public.encryption_key_registry"
        ),
        label="downgrade-populated-security-must-fail-preflight",
    )

    revision_after = scenario.current_revision()
    if revision_after != REV_0003:
        raise AssertionError(
            f"{scenario.database}: failed downgrade changed revision to {revision_after}"
        )

    with scenario.connect() as conn:
        key_after = conn.execute(
            """
            SELECT encrypted_dek
            FROM public.encryption_key_registry
            WHERE key_version = %s AND tenant_id = %s
            """,
            (key_version, TENANT_ID),
        ).fetchone()
        if key_after is None or bytes(key_after["encrypted_dek"]) != b"encrypted-dek-v1":
            raise AssertionError(f"{scenario.database}: encryption key changed or disappeared")

        payload_after = conn.execute(
            """
            SELECT payload_encrypted, key_version
            FROM public.organization_address_payloads_secure
            WHERE id = %s AND tenant_id = %s
            """,
            (payload_id, TENANT_ID),
        ).fetchone()
        if (
            payload_after is None
            or bytes(payload_after["payload_encrypted"]) != b"ciphertext-v1"
            or int(payload_after["key_version"]) != key_version
        ):
            raise AssertionError(f"{scenario.database}: encrypted payload changed or disappeared")

        ledger_after = conn.execute(
            """
            SELECT chain_hmac, payload_hash
            FROM public.address_audit_ledger
            WHERE id = %s AND tenant_id = %s
            """,
            (ledger_id, TENANT_ID),
        ).fetchone()
        if (
            ledger_after is None
            or ledger_after["chain_hmac"] != "b" * 64
            or ledger_after["payload_hash"] != "a" * 64
        ):
            raise AssertionError(f"{scenario.database}: audit ledger changed or disappeared")

        head_after = conn.execute(
            """
            SELECT last_ledger_id, last_hmac
            FROM public.audit_chain_heads
            WHERE tenant_id = %s
              AND entity_id = %s
              AND entity_type = 'organization_address'
            """,
            (TENANT_ID, ENTITY_ID),
        ).fetchone()
        if (
            head_after is None
            or int(head_after["last_ledger_id"]) != ledger_id
            or head_after["last_hmac"] != "b" * 64
        ):
            raise AssertionError(f"{scenario.database}: audit chain head changed or disappeared")

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
                f"{scenario.database}: failed preflight partially removed 0003 objects: {missing}"
            )

    result = {
        "scenario": "0003_populated_security_downgrade_fails_before_mutation",
        "revision_after_failure": revision_after,
        "encryption_key_preserved": True,
        "encrypted_payload_preserved": True,
        "audit_ledger_preserved": True,
        "audit_chain_head_preserved": True,
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
