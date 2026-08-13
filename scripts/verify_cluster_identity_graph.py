"""Read-only verification of the P2C PostgreSQL identity non-escalation graph."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from sqlalchemy import create_engine

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.cluster_identity_graph import (
    capture_identity_graph_catalog,
    evaluate_identity_graph_catalog,
    load_identity_transition_policy,
)
from app.core.cluster_role_contract import load_contract_bundle


VERIFY_URL_ENV = "DOERS_CLUSTER_VERIFY_DATABASE_URL"


def _sync_url(raw: str) -> str:
    if raw.startswith("postgresql+asyncpg://"):
        return "postgresql+psycopg://" + raw.removeprefix("postgresql+asyncpg://")
    if raw.startswith("postgresql://"):
        return "postgresql+psycopg://" + raw.removeprefix("postgresql://")
    return raw


def main() -> int:
    raw_url = os.environ.get(VERIFY_URL_ENV)
    if not raw_url:
        print(f"ERROR: {VERIFY_URL_ENV} is required", file=sys.stderr)
        return 2

    engine = create_engine(_sync_url(raw_url), pool_pre_ping=False)
    try:
        with engine.connect() as connection:
            bundle = load_contract_bundle()
            policy = load_identity_transition_policy()
            catalog = capture_identity_graph_catalog(connection, bundle, policy)
            violations = evaluate_identity_graph_catalog(catalog, bundle, policy)
            if connection.in_transaction():
                connection.rollback()
    finally:
        engine.dispose()

    if violations:
        print("P2C PostgreSQL identity non-escalation verification failed:", file=sys.stderr)
        for violation in violations:
            print(
                f" - [{violation.code}] {violation.subject}: {violation.message}",
                file=sys.stderr,
            )
        return 1

    print("P2C PostgreSQL identity non-escalation graph verified read-only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
