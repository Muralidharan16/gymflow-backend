from __future__ import annotations

import argparse
import os
import uuid

import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError


ORG_ID = uuid.UUID("10000000-0000-0000-0000-000000000091")
BRANCH_ID = uuid.UUID("20000000-0000-0000-0000-000000000091")
CLAIM_ID = uuid.UUID("30000000-0000-0000-0000-000000000091")

API_COLUMNS = {
    "branch_status",
    "deleted_at",
    "is_active",
    "is_operational",
    "lifecycle_transition_in_progress",
    "saga_compensation_strategy",
    "saga_last_checkpoint",
    "status",
    "status_changed_at",
    "status_changed_by",
    "status_reason",
    "transition_source",
}
MAINTENANCE_COLUMNS = {
    "reconciliation_claimed_at",
    "reconciliation_claimed_by",
    "search_last_synced_at",
    "search_sync_failed_at",
    "search_visibility_version",
}

STAGES = (
    "login_posture",
    "acl_contract",
    "context_gate",
    "positive_maintenance",
    "negative_boundaries",
)


def _sync_url(env_name: str) -> str:
    value = os.environ[env_name]
    return value.replace("postgresql+asyncpg://", "postgresql+psycopg://")


def _engine(env_name: str) -> sa.Engine:
    return sa.create_engine(_sync_url(env_name), pool_pre_ping=True)


def _engines() -> dict[str, sa.Engine]:
    return {
        "api": _engine("API_DATABASE_URL"),
        "auth": _engine("AUTH_DATABASE_URL"),
        "worker": _engine("WORKER_DATABASE_URL"),
        "maintenance": _engine("MAINTENANCE_DATABASE_URL"),
    }


def _set_context(conn: sa.Connection, *, org_id: uuid.UUID | None = None) -> None:
    conn.execute(
        sa.text("SELECT pg_catalog.set_config('app.internal_maintenance', 'lifecycle', true)")
    )
    if org_id is not None:
        conn.execute(
            sa.text("SELECT pg_catalog.set_config('app.current_org_id', :value, true)"),
            {"value": str(org_id)},
        )
        conn.execute(
            sa.text("SELECT pg_catalog.set_config('app.current_role', 'owner', true)")
        )


def _expect_db_denial(
    engine: sa.Engine,
    statement: str,
    params: dict | None = None,
) -> None:
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(statement), params or {})
    except DBAPIError:
        return
    raise AssertionError(f"database operation unexpectedly succeeded: {statement}")


def _column_updates(conn: sa.Connection, grantee: str) -> set[str]:
    return set(
        conn.execute(
            sa.text(
                """
                SELECT column_name::text
                FROM information_schema.column_privileges
                WHERE table_schema = 'public'
                  AND table_name = 'org_branch_state'
                  AND grantee = :grantee
                  AND privilege_type = 'UPDATE'
                ORDER BY column_name
                """
            ),
            {"grantee": grantee},
        ).scalars().all()
    )


def _assert_login_posture(engine: sa.Engine, expected_user: str) -> None:
    with engine.begin() as conn:
        row = conn.execute(
            sa.text(
                """
                SELECT current_user::text, rolsuper, rolcreatedb, rolcreaterole,
                       rolreplication, rolbypassrls
                FROM pg_catalog.pg_roles
                WHERE rolname = current_user
                """
            )
        ).one()
        assert row[0] == expected_user
        assert not any(bool(value) for value in row[1:])
        assert not conn.execute(
            sa.text(
                "SELECT pg_catalog.has_database_privilege("
                "current_user, current_database(), 'CREATE')"
            )
        ).scalar_one()
        assert not conn.execute(
            sa.text(
                "SELECT pg_catalog.has_schema_privilege("
                "current_user, 'public', 'CREATE')"
            )
        ).scalar_one()


def stage_login_posture(engines: dict[str, sa.Engine]) -> None:
    _assert_login_posture(engines["api"], "app_test_runtime")
    _assert_login_posture(engines["auth"], "auth_test_runtime")
    _assert_login_posture(engines["worker"], "worker_test_runtime")
    _assert_login_posture(engines["maintenance"], "maintenance_test_runtime")


def stage_acl_contract(engines: dict[str, sa.Engine]) -> None:
    api = engines["api"]
    maintenance = engines["maintenance"]

    with api.begin() as conn:
        observed = _column_updates(conn, "app_runtime")
        assert observed == API_COLUMNS, (
            "app_runtime state UPDATE column drift: "
            f"expected={sorted(API_COLUMNS)!r}, observed={sorted(observed)!r}"
        )
        assert not conn.execute(
            sa.text(
                "SELECT pg_catalog.has_table_privilege('app_runtime', "
                "'public.org_branch_state', 'UPDATE')"
            )
        ).scalar_one()
        assert not conn.execute(
            sa.text(
                "SELECT pg_catalog.has_table_privilege('app_runtime', "
                "'public.branch_watchdog_alerts', 'SELECT')"
            )
        ).scalar_one()
        assert not conn.execute(
            sa.text(
                "SELECT pg_catalog.has_table_privilege('app_runtime', "
                "'public.branch_watchdog_alerts', 'INSERT')"
            )
        ).scalar_one()

    with maintenance.begin() as conn:
        observed = _column_updates(conn, "lifecycle_maintenance_runtime")
        assert observed == MAINTENANCE_COLUMNS, (
            "maintenance state UPDATE column drift: "
            f"expected={sorted(MAINTENANCE_COLUMNS)!r}, observed={sorted(observed)!r}"
        )
        assert conn.execute(
            sa.text(
                "SELECT pg_catalog.has_table_privilege("
                "'lifecycle_maintenance_runtime', 'public.org_branch_state', 'SELECT')"
            )
        ).scalar_one()
        assert not conn.execute(
            sa.text(
                "SELECT pg_catalog.has_table_privilege("
                "'lifecycle_maintenance_runtime', 'public.org_branch_state', 'UPDATE')"
            )
        ).scalar_one()
        assert conn.execute(
            sa.text(
                "SELECT pg_catalog.has_table_privilege("
                "'lifecycle_maintenance_runtime', "
                "'public.branch_watchdog_alerts', 'SELECT')"
            )
        ).scalar_one()
        assert conn.execute(
            sa.text(
                "SELECT pg_catalog.has_table_privilege("
                "'lifecycle_maintenance_runtime', "
                "'public.branch_watchdog_alerts', 'INSERT')"
            )
        ).scalar_one()


def stage_context_gate(engines: dict[str, sa.Engine]) -> None:
    maintenance = engines["maintenance"]

    with maintenance.begin() as conn:
        assert conn.execute(
            sa.text(
                "SELECT count(*) FROM public.org_branch_state "
                "WHERE branch_id = :branch_id"
            ),
            {"branch_id": BRANCH_ID},
        ).scalar_one() == 0
        assert conn.execute(
            sa.text(
                "UPDATE public.org_branch_state "
                "SET reconciliation_claimed_by = :claim_id "
                "WHERE branch_id = :branch_id"
            ),
            {"claim_id": CLAIM_ID, "branch_id": BRANCH_ID},
        ).rowcount == 0

    _expect_db_denial(
        maintenance,
        "INSERT INTO public.branch_watchdog_alerts (branch_id, alert_type) "
        "VALUES (:branch_id, 'without_context')",
        {"branch_id": BRANCH_ID},
    )


def stage_positive_maintenance(engines: dict[str, sa.Engine]) -> None:
    maintenance = engines["maintenance"]

    with maintenance.begin() as conn:
        _set_context(conn)
        row = conn.execute(
            sa.text(
                "SELECT status, search_visibility_version "
                "FROM public.org_branch_state WHERE branch_id = :branch_id"
            ),
            {"branch_id": BRANCH_ID},
        ).one()
        assert row.status == "active"
        assert int(row.search_visibility_version) == 1

        updated = conn.execute(
            sa.text(
                "UPDATE public.org_branch_state "
                "SET reconciliation_claimed_by = :claim_id, "
                "    reconciliation_claimed_at = clock_timestamp(), "
                "    search_sync_failed_at = clock_timestamp() "
                "WHERE branch_id = :branch_id"
            ),
            {"claim_id": CLAIM_ID, "branch_id": BRANCH_ID},
        )
        assert updated.rowcount == 1

        alert_type = f"maintenance_boundary_{uuid.uuid4().hex}"
        inserted = conn.execute(
            sa.text(
                "INSERT INTO public.branch_watchdog_alerts (branch_id, alert_type) "
                "VALUES (:branch_id, :alert_type) RETURNING alert_id"
            ),
            {"branch_id": BRANCH_ID, "alert_type": alert_type},
        ).scalar_one()
        assert inserted is not None


def stage_negative_boundaries(engines: dict[str, sa.Engine]) -> None:
    api = engines["api"]
    auth = engines["auth"]
    worker = engines["worker"]
    maintenance = engines["maintenance"]

    _expect_db_denial(
        maintenance,
        "UPDATE public.org_branch_state "
        "SET status = 'suspended' WHERE branch_id = :branch_id",
        {"branch_id": BRANCH_ID},
    )
    _expect_db_denial(
        maintenance,
        "SELECT * FROM public.branch_outbox_events LIMIT 1",
    )
    _expect_db_denial(
        maintenance,
        "INSERT INTO public.organizations (id, name, tier, is_active) "
        "VALUES (gen_random_uuid(), 'forbidden', 'basic', true)",
    )

    _expect_db_denial(
        api,
        "UPDATE public.org_branch_state "
        "SET reconciliation_claimed_at = clock_timestamp() "
        "WHERE branch_id = :branch_id",
        {"branch_id": BRANCH_ID},
    )
    _expect_db_denial(api, "SELECT * FROM public.branch_watchdog_alerts LIMIT 1")
    _expect_db_denial(
        api,
        "INSERT INTO public.branch_watchdog_alerts (branch_id, alert_type) "
        "VALUES (:branch_id, 'forbidden_api')",
        {"branch_id": BRANCH_ID},
    )

    for engine, identity in ((worker, "worker"), (auth, "auth")):
        try:
            with engine.begin() as conn:
                _set_context(conn, org_id=ORG_ID)
                conn.execute(
                    sa.text(
                        "UPDATE public.org_branch_state "
                        "SET reconciliation_claimed_at = clock_timestamp() "
                        "WHERE branch_id = :branch_id"
                    ),
                    {"branch_id": BRANCH_ID},
                )
        except DBAPIError:
            pass
        else:
            raise AssertionError(
                f"{identity} identity updated maintenance reconciliation state"
            )


def run_stage(stage: str) -> None:
    engines = _engines()
    try:
        globals()[f"stage_{stage}"](engines)
    finally:
        for engine in engines.values():
            engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--stage",
        choices=("all", *STAGES),
        default="login_posture",
    )
    args = parser.parse_args()

    selected = STAGES if args.stage == "all" else (args.stage,)
    for stage in selected:
        print(f"VERIFY_STAGE_START: {stage}", flush=True)
        try:
            run_stage(stage)
        except Exception as exc:
            print(
                f"VERIFY_STAGE_FAILURE: {stage}: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            raise
        print(f"VERIFY_STAGE_PASS: {stage}", flush=True)

    print("PASS: lifecycle maintenance runtime boundary verified", flush=True)


if __name__ == "__main__":
    main()
