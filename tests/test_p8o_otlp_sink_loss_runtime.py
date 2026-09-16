from __future__ import annotations

import json
import os
import socket
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import psycopg
import pytest
from sqlalchemy.engine import make_url

from app.observability.runtime_metrics import (
    configure_runtime_metrics,
    force_flush_runtime_metrics,
    runtime_metrics,
    shutdown_runtime_metrics,
)


ROOT = Path(__file__).resolve().parents[1]
ALERTS = json.loads(
    (ROOT / "docs/architecture/p8_alert_slo_contract.json").read_text(encoding="utf-8")
)["critical_alerts"]
CONTRACT = json.loads(
    (ROOT / "docs/architecture/p8_production_like_observability_contract.json").read_text(
        encoding="utf-8"
    )
)
CAPTURE = Path(os.environ.get("P8O_OTLP_CAPTURE_PATH", "/tmp/p8o-operational-otlp.jsonl"))
LIVE_ENDPOINT = os.environ.get(
    "P8_METRICS_OTLP_ENDPOINT", "http://127.0.0.1:4318/v1/metrics"
)
DEAD_SERVICE = "doers-p8o-dead-sink"
RECOVERED_SERVICE = "doers-p8o-runtime-recovered"


def _url(name: str):
    raw = os.environ.get(name, "").strip()
    if not raw:
        pytest.skip(f"{name} is required")
    value = make_url(raw)
    if str(value.host) not in {"127.0.0.1", "localhost"}:
        raise RuntimeError(f"P8-O refuses non-local destructive target: {value.host}")
    if "test" not in str(value.database):
        raise RuntimeError(f"P8-O refuses non-disposable database: {value.database}")
    return value


def _connect_admin():
    value = _url("TEST_ADMIN_DATABASE_URL")
    return psycopg.connect(
        host=str(value.host),
        port=int(value.port),
        dbname=str(value.database),
        user=value.username or "migration_owner",
        password=value.password or os.environ.get("MIGRATION_PASSWORD", ""),
    )


def _capture_records() -> list[dict[str, Any]]:
    if not CAPTURE.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in CAPTURE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def _records_for_service(service_name: str) -> list[dict[str, Any]]:
    return [
        record
        for record in _capture_records()
        if record.get("resource", {}).get("service.name") == service_name
    ]


def _wait_for(predicate: Callable[[], Any], *, description: str, timeout: float = 10.0) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {description}; last={last!r}")


def test_real_otlp_sink_loss_does_not_block_durable_postgresql_commit() -> None:
    """A dead telemetry sink must never become business-state authority.

    The dead exporter uses a real unused loopback port. A real PostgreSQL commit
    must still succeed. The live collector may concurrently receive delayed
    batches from an older provider, so the evidence is scoped to the unique
    dead-sink resource: no metric from DEAD_SERVICE may reach the live receiver.
    """

    assert os.environ.get("P8O_PROCESS_FAULTS") == "1"
    _url("TEST_ADMIN_DATABASE_URL")

    # Establish an unused loopback port without introducing any mock transport.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        dead_port = int(sock.getsockname()[1])

    shutdown_runtime_metrics(timeout_millis=1000)
    assert _records_for_service(DEAD_SERVICE) == []
    configure_runtime_metrics(
        endpoint=f"http://127.0.0.1:{dead_port}/v1/metrics",
        export_interval_seconds=300.0,
        export_timeout_seconds=0.2,
        environment="ci-p8o",
        service_name=DEAD_SERVICE,
    )
    runtime_metrics().telemetry_heartbeat(profile="maintenance")

    org_id = uuid.uuid4()
    with _connect_admin() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO public.organizations(
                    id,name,slug,tier,is_active,max_branches,default_currency_code
                ) VALUES (%s,%s,%s,'basic',true,1,'INR')
                """,
                (
                    org_id,
                    "P8-O telemetry outage authority probe",
                    f"p8o-otel-isolated-{org_id.hex}",
                ),
            )
        connection.commit()

    # Exercise the real OTLP/HTTP exporter against the unavailable sink. The
    # return value is SDK-specific; the authority assertion is the durable row
    # plus the absence of this service from the live collector.
    force_flush_runtime_metrics(timeout_millis=1000)

    with _connect_admin() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM public.organizations WHERE id=%s", (org_id,))
            assert cursor.fetchone() == (1,)

    # Delayed batches from a previous live provider are allowed to append to the
    # shared collector. What must never happen is a batch from this dead-sink
    # resource appearing at the live collector.
    assert _records_for_service(DEAD_SERVICE) == []

    alert = ALERTS["observability_pipeline_failure"]
    assert "absent_over_time" in alert["query"]
    proof = CONTRACT["critical_failure_mode_proof"]["observability_pipeline_failure"]
    assert proof["metric"] == "doers.platform.observability.heartbeat"
    assert proof["alert"] == alert["alert"]
    assert proof["runbook"] == alert["runbook"]
    assert (ROOT / proof["runbook"]).is_file()

    # Prove recovery with a fresh provider pointed back at the real disposable
    # collector. This is evidence only and does not mutate the committed row.
    shutdown_runtime_metrics(timeout_millis=1000)
    configure_runtime_metrics(
        endpoint=LIVE_ENDPOINT,
        export_interval_seconds=1.0,
        export_timeout_seconds=1.0,
        environment="ci-p8o",
        service_name=RECOVERED_SERVICE,
    )
    runtime_metrics().telemetry_heartbeat(profile="maintenance")
    assert force_flush_runtime_metrics(timeout_millis=4000) is True
    _wait_for(
        lambda: any(
            record.get("metric") == "doers.platform.observability.heartbeat"
            for record in _records_for_service(RECOVERED_SERVICE)
        ),
        description="OTLP recovery heartbeat",
    )

    shutdown_runtime_metrics(timeout_millis=1000)
