from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.cluster_identity_graph import (
    capture_identity_graph_catalog,
    evaluate_identity_graph_catalog,
    load_identity_transition_policy,
)
from app.core.cluster_role_contract import load_contract_bundle
from app.core.cluster_role_preflight import (
    capture_external_role_catalog,
    evaluate_cluster_role_catalog,
)
from app.core.pay2_finance_roles import (
    PAY2_FINANCE_CAPABILITY_ROLES,
    predecessor_contract_bundle,
    predecessor_identity_policy,
)

URL_ENV = "DOERS_PAY2_CLUSTER_ADMIN_DATABASE_URL"


def _sync_url(raw: str) -> str:
    if raw.startswith("postgresql+asyncpg://"):
        return "postgresql+psycopg://" + raw.removeprefix("postgresql+asyncpg://")
    if raw.startswith("postgresql://"):
        return "postgresql+psycopg://" + raw.removeprefix("postgresql://")
    return raw


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("state", choices=("predecessor", "full"))
    args = parser.parse_args()
    raw = os.environ.get(URL_ENV, "").strip()
    if not raw:
        print(f"ERROR: {URL_ENV} is required", file=sys.stderr)
        return 2

    full_bundle = load_contract_bundle()
    full_policy = load_identity_transition_policy()
    if args.state == "predecessor":
        bundle = predecessor_contract_bundle(full_bundle)
        policy = predecessor_identity_policy(full_policy)
        expected = False
    else:
        bundle = full_bundle
        policy = full_policy
        expected = True

    engine = create_engine(_sync_url(raw), pool_pre_ping=False)
    try:
        with engine.connect() as connection:
            identity = connection.execute(text(
                "SELECT current_user::text,session_user::text,rolsuper "
                "FROM pg_catalog.pg_roles WHERE rolname=current_user"
            )).one()
            if identity[0] != "postgres" or identity[1] != "postgres" or not bool(identity[2]):
                raise RuntimeError("PAY-2 role expansion verification requires postgres SUPERUSER")

            catalog = capture_external_role_catalog(connection, bundle)
            violations = list(evaluate_cluster_role_catalog(catalog, bundle))
            graph = capture_identity_graph_catalog(connection, bundle, policy)
            violations.extend(evaluate_identity_graph_catalog(graph, bundle, policy))
            for role in PAY2_FINANCE_CAPABILITY_ROLES:
                present = bool(connection.execute(
                    text("SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_roles WHERE rolname=:role)"),
                    {"role": role},
                ).scalar_one())
                if present is not expected:
                    raise RuntimeError(
                        f"PAY-2 role presence mismatch for {role}: expected={expected}, found={present}"
                    )
            if violations:
                rendered = "; ".join(
                    f"[{v.code}] {v.subject}: {v.message}" for v in sorted(set(violations))
                )
                raise RuntimeError("PAY-2 cluster role state rejected: " + rendered)
    finally:
        engine.dispose()

    print(f"PAY2_CLUSTER_ROLE_STATE={args.state.upper()}_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
