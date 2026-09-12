"""Read-only verification of the canonical PostgreSQL cluster-role contract."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import os

from sqlalchemy import create_engine

from app.core.cluster_role_contract import load_contract_bundle
from app.core.cluster_role_preflight import (
    capture_external_role_catalog,
    evaluate_cluster_role_catalog,
)


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
            catalog = capture_external_role_catalog(connection, bundle)
            violations = evaluate_cluster_role_catalog(catalog, bundle)
    finally:
        engine.dispose()

    if violations:
        print("Canonical PostgreSQL cluster-role verification failed:", file=sys.stderr)
        for violation in violations:
            print(
                f" - [{violation.code}] {violation.subject}: {violation.message}",
                file=sys.stderr,
            )
        return 1

    print("Canonical PostgreSQL cluster-role contract verified read-only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
