from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

import psycopg
from sqlalchemy.engine import make_url

from app.observability.runtime_metrics import (
    configure_runtime_metrics,
    force_flush_runtime_metrics,
    runtime_metrics,
    shutdown_runtime_metrics,
)
from app.tasks.branch_lifecycle_sweeps import (
    _prepare_maintenance_session,
    _record_lifecycle_snapshot,
)


ROOT = Path(__file__).resolve().parents[1]
CAPTURE = Path(os.environ.get("P8O_OTLP_CAPTURE_PATH", "/tmp/p8o-operational-otlp.jsonl"))
OTLP_ENDPOINT = os.environ.get(
    "P8_METRICS_OTLP_ENDPOINT", "http://127.0.0.1:4318/v1/metrics"
)


def _admin_url():
    value = make_url(os.environ["TEST_ADMIN_DATABASE_URL"])
    assert str(value.host) in {"127.0.0.1", "localhost"}
    assert str(value.database) == "gymflow_p8o_test"
    return value


def _admin_connection():
    value = _admin_url()
    return psycopg.connect(
        host=str(value.host),
        port=int(value.port),
        dbname=str(value.database),
        user=value.username,
        password=value.password,
    )


def _auth_connection():
    value = _admin_url()
    return psycopg.connect(
        host=str(value.host),
        port=int(value.port),
        dbname=str(value.database),
        user="auth_p4e_runtime",
        password=os.environ["AUTH_RUNTIME_PASSWORD"],
    )


def _app_connection():
    value = _admin_url()
    return psycopg.connect(
        host=str(value.host),
        port=int(value.port),
        dbname=str(value.database),
        user="app_test_runtime",
        password=os.environ["APP_RUNTIME_PASSWORD"],
    )


def _records() -> list[dict[str, Any]]:
    if not CAPTURE.is_file():
        return []
    result: list[dict[str, Any]] = []
    for line in CAPTURE.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            result.append(item)
    return result


def _stuck_depth() -> float:
    values = [
        float(record.get("value", 0.0))
        for record in _records()
        if record.get("metric") == "doers.lifecycle.state.depth"
        and record.get("attributes", {}).get("state") == "stuck"
    ]
    return max(values, default=-1.0)


def _seed_disposable_branch_state() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Create one real branch state through the certified bootstrap boundary."""
    org_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    branch_id = uuid.uuid4()

    # The disposable migration owner may create prerequisite tenant entities,
    # but it does not manufacture the lifecycle state that the maintenance
    # observer will inspect. That state is created below by the reduced auth
    # identity through the already-certified P3A bootstrap authority.
    with _admin_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO public.organizations(
                    id,name,slug,tier,is_active,max_branches,
                    default_currency_code
                ) VALUES (%s,%s,%s,'basic',true,10,'INR')
                """,
                (
                    org_id,
                    f"P8O Stuck Lifecycle {org_id}",
                    f"p8o-stuck-{org_id.hex}",
                ),
            )
            cursor.execute(
                """
                INSERT INTO public.owners(
                    id,org_id,owner_name,email,hashed_password,email_verified
                ) VALUES (%s,%s,'P8O Stuck Owner',%s,'not-a-real-password',true)
                """,
                (
                    owner_id,
                    org_id,
                    f"p8o-stuck-{owner_id.hex}@example.test",
                ),
            )
            cursor.execute(
                "SELECT pg_catalog.set_config('app.current_org_id', %s, true)",
                (str(org_id),),
            )
            cursor.execute(
                """
                INSERT INTO public.org_branches(
                    id,org_id,branch_name,branch_code,internal_slug,
                    country_code,currency_code,created_by
                ) VALUES (%s,%s,'P8O Stuck Branch','P8O-S',%s,'IN','INR',%s)
                """,
                (
                    branch_id,
                    org_id,
                    f"p8o-stuck-{branch_id.hex}",
                    owner_id,
                ),
            )
        connection.commit()

    with _auth_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    pg_catalog.set_config('app.current_role','owner',true),
                    pg_catalog.set_config('app.current_org_id',%s,true),
                    pg_catalog.set_config('app.current_user_id',%s,true),
                    pg_catalog.set_config('app.current_principal_type','owner',true),
                    pg_catalog.set_config('app.current_gym_id','',true)
                """,
                (str(org_id), str(owner_id)),
            )
            cursor.execute(
                """
                INSERT INTO public.org_branch_state(
                    branch_id,org_id,branch_status,is_primary,is_active,
                    is_public,status,is_operational,status_changed_by,
                    status_reason,transition_source,scheduled_transition_at,
                    scheduled_transition_to,lifecycle_transition_in_progress,
                    saga_last_checkpoint,saga_compensation_strategy,
                    watchdog_recovered_at,watchdog_recovery_count,
                    search_visibility_version,search_last_synced_at,
                    search_sync_failed_at,reconciliation_claimed_by,
                    reconciliation_claimed_at,worm_archive_uri,
                    worm_archive_checksum,worm_archive_verified_at,
                    worm_archive_status,version,search_logical_clock,
                    search_epoch_ulid,deleted_at,archived_at,purged_at
                ) VALUES (
                    %s,%s,'active',true,true,true,'active',true,NULL,NULL,
                    'api',NULL,NULL,false,NULL,NULL,NULL,0,1,NULL,NULL,NULL,
                    NULL,NULL,NULL,NULL,NULL,1,0,%s,NULL,NULL,NULL
                )
                RETURNING status_changed_at,updated_at
                """,
                (
                    branch_id,
                    org_id,
                    uuid.uuid4().hex[:26].upper(),
                ),
            )
            returned = cursor.fetchone()
            assert returned is not None
            assert all(value is not None for value in returned)
        connection.commit()

    return branch_id, org_id, owner_id


def test_real_persisted_stuck_lifecycle_is_visible_to_maintenance_observability() -> None:
    assert os.environ.get("P8O_PROCESS_FAULTS") == "1"

    branch_id, org_id, owner_id = _seed_disposable_branch_state()

    # Fault aging is a test-only tenant-scoped lifecycle mutation. app_runtime
    # already owns UPDATE only on the lifecycle-domain columns used here, and
    # p_branch_update requires the authoritative tenant GUC plus an allowed role.
    # This deliberately exercises that existing least-privilege boundary rather
    # than granting migration/security-owner access to lifecycle columns.
    with _app_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    pg_catalog.set_config('app.current_role','owner',true),
                    pg_catalog.set_config('app.current_org_id',%s,true),
                    pg_catalog.set_config('app.current_user_id',%s,true),
                    pg_catalog.set_config('app.current_principal_type','owner',true),
                    pg_catalog.set_config('app.current_gym_id','',true)
                """,
                (str(org_id), str(owner_id)),
            )
            cursor.execute(
                """
                UPDATE public.org_branch_state
                SET lifecycle_transition_in_progress=true,
                    status_changed_at=pg_catalog.clock_timestamp()-interval '20 minutes'
                WHERE branch_id=%s
                  AND deleted_at IS NULL
                """,
                (branch_id,),
            )
            assert cursor.rowcount == 1
        connection.commit()

    from app.core.database import maintenance_async_session_maker

    shutdown_runtime_metrics(timeout_millis=1000)
    configure_runtime_metrics(
        endpoint=OTLP_ENDPOINT,
        export_interval_seconds=1.0,
        export_timeout_seconds=1.0,
        environment="ci-p8o",
        service_name="doers-p8o-stuck-lifecycle",
    )

    async def collect() -> None:
        async with maintenance_async_session_maker() as session:
            await _prepare_maintenance_session(session)
            await _record_lifecycle_snapshot(session)
            await session.commit()

    asyncio.run(collect())
    runtime_metrics().telemetry_heartbeat(profile="maintenance")
    force_flush_runtime_metrics(timeout_millis=4000)

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and _stuck_depth() < 1.0:
        time.sleep(0.1)
    assert _stuck_depth() >= 1.0

    alert_contract = json.loads(
        (ROOT / "docs/architecture/p8_alert_slo_contract.json").read_text(
            encoding="utf-8"
        )
    )["critical_alerts"]["stuck_saga_or_lifecycle"]
    assert alert_contract["alert"] == "DoersLifecycleStuck"
    assert alert_contract["runbook"] == "docs/runbooks/p8/stuck-lifecycle.md"
    assert (ROOT / alert_contract["runbook"]).is_file()

    shutdown_runtime_metrics(timeout_millis=2000)
