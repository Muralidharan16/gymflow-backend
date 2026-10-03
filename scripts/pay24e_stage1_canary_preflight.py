#!/usr/bin/env python3
"""Read-only PAY-24-E activation preflight.

No transition/binding/provider operation is available from this script.
"""

from __future__ import annotations

import json
import os
import uuid

import psycopg


EXPECTED_CAPABILITIES = [
    "checkout",
    "webhooks",
    "payment_application",
    "subscription_activation",
]


def _required(name: str) -> str:
    value = str(os.environ.get(name, "") or "").strip()
    if not value:
        raise SystemExit(f"missing required environment variable: {name}")
    return value


def _psycopg_url(raw: str) -> str:
    return raw.replace("postgresql+psycopg://", "postgresql://", 1)


def main() -> int:
    url = _psycopg_url(_required("FINANCE_CONFIG_DATABASE_URL"))
    expected_sha = _required("PAY24E_EXPECTED_SHA")
    if len(expected_sha) != 40 or any(ch not in "0123456789abcdef" for ch in expected_sha):
        raise SystemExit("PAY24E_EXPECTED_SHA must be a lowercase 40-character Git SHA")

    expected_stage = int(os.environ.get("PAY24E_EXPECTED_STAGE", "0"))
    if expected_stage not in {0, 1}:
        raise SystemExit("PAY24E_EXPECTED_STAGE must be 0 or 1")

    expected_org = None
    if expected_stage == 1:
        expected_org = uuid.UUID(_required("PAY24E_INTERNAL_ORGANIZATION_ID"))

    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT session_user::text,current_user::text")
            principal = cur.fetchone()
            cur.execute("SELECT * FROM app_secure.pay24a_activation_snapshot()")
            snapshot = cur.fetchone()
            cur.execute("SELECT * FROM app_secure.pay24d_entitlement_readiness_snapshot()")
            readiness = cur.fetchone()
        conn.rollback()

    stage, generation, egress, capabilities, internal_org = snapshot[:5]
    authorization_id, authorized_stage, certified_sha, deployed_sha = snapshot[5:9]

    if expected_stage == 0:
        if not (
            stage == 0
            and egress == "blocked"
            and capabilities == []
            and internal_org is None
        ):
            raise SystemExit("PAY-24-E Stage-0 preflight failed")
    else:
        if not (
            stage == 1
            and egress == "open"
            and capabilities == EXPECTED_CAPABILITIES
            and internal_org == expected_org
            and authorization_id is not None
            and authorized_stage == 1
            and certified_sha == expected_sha
            and deployed_sha == expected_sha
        ):
            raise SystemExit("PAY-24-E Stage-1 exact-posture preflight failed")

    print(
        json.dumps(
            {
                "pay24e_preflight": "PASS",
                "database_principal": principal[0],
                "stage": stage,
                "generation": generation,
                "provider_egress": egress,
                "enabled_capabilities": capabilities,
                "internal_organization_id": str(internal_org) if internal_org else None,
                "certified_sha": certified_sha,
                "deployed_sha": deployed_sha,
                "entitlement_backlog": {
                    "pending": readiness[0],
                    "processing": readiness[1],
                    "failed": readiness[2],
                    "review_required": readiness[3],
                    "oldest_pending_age_seconds": readiness[4],
                    "expired_processing_leases": readiness[5],
                },
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
