from __future__ import annotations

import asyncio
import json
import os
import time
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


def test_real_persisted_stuck_lifecycle_is_visible_to_maintenance_observability() -> None:
    assert os.environ.get("P8O_PROCESS_FAULTS") == "1"

    # Fault seeding is deliberately performed only by the disposable migration
    # owner. The observation itself below still runs through the certified
    # lifecycle-maintenance runtime identity/capability.
    with _admin_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE public.org_branch_state
                SET lifecycle_transition_in_progress=true,
                    status_changed_at=pg_catalog.clock_timestamp()-interval '20 minutes'
                WHERE branch_id=(
                    SELECT branch_id
                    FROM public.org_branch_state
                    WHERE deleted_at IS NULL
                    ORDER BY branch_id
                    LIMIT 1
                )
                """
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
