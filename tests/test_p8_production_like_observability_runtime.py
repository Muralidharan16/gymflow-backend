from __future__ import annotations

import asyncio
import io
import json
import os
import socket
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.request import urlopen

import psycopg
import pytest
import redis
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.testclient import TestClient

from app.observability.request_context import RequestObservabilityMiddleware
from app.observability.runtime_metrics import (
    configure_runtime_metrics,
    force_flush_runtime_metrics,
    runtime_metrics,
    shutdown_runtime_metrics,
)
from app.observability.search_metrics import record_provider_call
from app.observability.structured_logging import StructuredJsonFormatter
from app.tasks.external_effect_observability import (
    _run_external_effect_operational_snapshot,
)
from app.tasks.runtime_observability import _run_runtime_operational_snapshot


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = json.loads(
    (ROOT / "docs/architecture/p8_production_like_observability_contract.json").read_text(
        encoding="utf-8"
    )
)
ALERTS = json.loads(
    (ROOT / "docs/architecture/p8_alert_slo_contract.json").read_text(encoding="utf-8")
)["critical_alerts"]
CAPTURE = Path(os.environ.get("P8O_OTLP_CAPTURE_PATH", "/tmp/p8o-operational-otlp.jsonl"))
OTLP_ENDPOINT = os.environ.get(
    "P8_METRICS_OTLP_ENDPOINT", "http://127.0.0.1:4318/v1/metrics"
)

_FORBIDDEN_METRIC_KEYS = {
    "request_id",
    "correlation_id",
    "tenant_id",
    "branch_id",
    "principal_id",
    "saga_id",
    "task_id",
    "trace_id",
    "span_id",
}


def _url(env_name: str):
    raw = os.environ.get(env_name)
    if not raw:
        raise RuntimeError(f"P8-O runtime requires {env_name}")
    value = make_url(raw)
    if not value.host or not value.port or not value.database:
        raise RuntimeError(f"P8-O {env_name} must include host, port and database")
    if str(value.host) not in {"127.0.0.1", "localhost"}:
        raise RuntimeError(f"P8-O refuses non-local destructive target: {value.host}")
    if "test" not in str(value.database):
        raise RuntimeError(f"P8-O refuses non-disposable database: {value.database}")
    return value


def _connect_admin(*, autocommit: bool = False):
    value = _url("TEST_ADMIN_DATABASE_URL")
    return psycopg.connect(
        host=str(value.host),
        port=int(value.port),
        dbname=str(value.database),
        user=value.username or "migration_owner",
        password=value.password or os.environ.get("MIGRATION_PASSWORD", ""),
        autocommit=autocommit,
    )


def _capture_records() -> list[dict[str, Any]]:
    if not CAPTURE.exists():
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


def _wait_for(
    predicate: Callable[[], Any],
    *,
    description: str,
    timeout: float = 10.0,
) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {description}; last={last!r}")


def _matching_records(metric: str, **attributes: str) -> list[dict[str, Any]]:
    return [
        record
        for record in _capture_records()
        if record.get("metric") == metric
        and all(str(record.get("attributes", {}).get(key)) == value for key, value in attributes.items())
    ]


def _metric_value(metric: str, **attributes: str) -> float:
    matches = _matching_records(metric, **attributes)
    if not matches:
        return -1.0
    return max(float(record.get("value", 0.0)) for record in matches)


def _histogram_sum(metric: str, **attributes: str) -> float:
    matches = _matching_records(metric, **attributes)
    if not matches:
        return -1.0
    return max(float(record.get("sum", 0.0)) for record in matches)


def _flush() -> None:
    force_flush_runtime_metrics(timeout_millis=4000)


def _assert_binding(failure_mode: str, metric: str) -> None:
    proof = CONTRACT["critical_failure_mode_proof"][failure_mode]
    alert = ALERTS[failure_mode]
    assert proof["metric"] == metric
    assert proof["alert"] == alert["alert"]
    assert proof["runbook"] == alert["runbook"]
    assert (ROOT / proof["runbook"]).is_file()


def _redis_container_id() -> str:
    completed = subprocess.run(
        ["docker", "ps", "-aq", "--filter", "ancestor=redis:7-alpine"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=10,
        check=False,
    )
    ids = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    assert completed.returncode == 0 and len(ids) == 1, completed.stdout
    return ids[0]


def _broker_ping() -> bool:
    try:
        client = redis.Redis.from_url(
            os.environ["CELERY_BROKER_URL"], socket_connect_timeout=0.5, socket_timeout=0.5
        )
        return client.ping() is True
    except redis.RedisError:
        return False


def _stop_redis() -> None:
    completed = subprocess.run(
        ["docker", "stop", "-t", "1", _redis_container_id()],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout
    _wait_for(lambda: not _broker_ping(), description="real Redis outage", timeout=8)


def _start_redis() -> None:
    completed = subprocess.run(
        ["docker", "start", _redis_container_id()],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout
    _wait_for(_broker_ping, description="real Redis recovery", timeout=15)


def _wait_postgres() -> None:
    def available() -> bool:
        try:
            with _connect_admin() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    return cursor.fetchone() == (1,)
        except Exception:
            return False

    _wait_for(available, description="PostgreSQL recovery", timeout=20)


@pytest.fixture(scope="module", autouse=True)
def _configured_runtime_metrics() -> Iterator[None]:
    if os.environ.get("P8O_PROCESS_FAULTS") != "1":
        raise RuntimeError("P8-O production-like faults require P8O_PROCESS_FAULTS=1")
    _url("TEST_ADMIN_DATABASE_URL")
    assert CAPTURE.parent.exists() or CAPTURE.parent.mkdir(parents=True, exist_ok=True) is None
    shutdown_runtime_metrics(timeout_millis=1000)
    configure_runtime_metrics(
        endpoint=OTLP_ENDPOINT,
        export_interval_seconds=1.0,
        export_timeout_seconds=1.0,
        environment="ci-p8o",
        service_name="doers-p8o-runtime",
    )
    runtime_metrics().telemetry_heartbeat(profile="maintenance")
    _flush()
    yield
    shutdown_runtime_metrics(timeout_millis=2000)


def test_00_runtime_topology_is_real_disposable_and_collector_is_receiving() -> None:
    db = _url("TEST_ADMIN_DATABASE_URL")
    assert str(db.database) == "gymflow_p8o_test"
    with _connect_admin() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SHOW server_version")
            version = cursor.fetchone()[0]
            assert str(version).startswith("16.")
    assert _broker_ping()
    _wait_for(
        lambda: _metric_value(
            "doers.platform.observability.heartbeat", profile="maintenance"
        )
        == 1.0,
        description="real OTLP heartbeat export",
    )


def test_10_real_postgresql_snapshots_emit_dead_letter_and_finance_ambiguity() -> None:
    snapshot = asyncio.run(_run_external_effect_operational_snapshot())
    assert snapshot["search"]["dead_letter_count"] >= 1
    assert snapshot["refund"]["dead_letter_count"] >= 1
    assert snapshot["refund"]["reconciliation_pending_count"] >= 1
    assert snapshot["refund"]["provider_accepted_count"] >= 1
    _flush()

    _wait_for(
        lambda: _metric_value("doers.queue.dead_letters", queue="search") >= 1,
        description="PostgreSQL dead-letter metric",
    )
    assert _metric_value(
        "doers.finance.signal.depth", signal="reconciliation_mismatch"
    ) >= 1
    assert _metric_value(
        "doers.finance.signal.depth", signal="provider_ack_ambiguity"
    ) >= 1
    _assert_binding("dead_letter_present", "doers.queue.dead_letters")
    _assert_binding(
        "finance_reconciliation_mismatch", "doers.finance.signal.depth"
    )
    _assert_binding(
        "provider_success_db_ack_ambiguity", "doers.finance.signal.depth"
    )


def test_20_real_postgresql_stuck_lifecycle_snapshot_crosses_watchdog_boundary() -> None:
    with _connect_admin() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL ROLE app_security_owner")
            cursor.execute(
                """
                UPDATE public.org_branch_state
                SET lifecycle_transition_in_progress=true,
                    status_changed_at=pg_catalog.clock_timestamp()-interval '20 minutes'
                WHERE branch_id=(
                    SELECT branch_id FROM public.org_branch_state
                    WHERE deleted_at IS NULL
                    ORDER BY branch_id
                    LIMIT 1
                )
                """
            )
            assert cursor.rowcount == 1
            cursor.execute("RESET ROLE")
        connection.commit()

    from app.core.database import maintenance_async_session_maker
    from app.tasks.branch_lifecycle_sweeps import (
        _prepare_maintenance_session,
        _record_lifecycle_snapshot,
    )

    async def collect() -> None:
        async with maintenance_async_session_maker() as session:
            await _prepare_maintenance_session(session)
            await _record_lifecycle_snapshot(session)
            await session.commit()

    asyncio.run(collect())
    _flush()
    _wait_for(
        lambda: _metric_value("doers.lifecycle.state.depth", state="stuck") >= 1,
        description="stuck lifecycle metric",
    )
    _assert_binding("stuck_saga_or_lifecycle", "doers.lifecycle.state.depth")


def test_30_real_redis_backlog_exports_oldest_age_above_alert_threshold() -> None:
    broker = redis.Redis.from_url(os.environ["CELERY_BROKER_URL"])
    broker.delete("worker")
    stale = json.dumps(
        {"headers": {"doers_published_at_unix": time.time() - 610.0}, "body": "opaque"}
    ).encode()
    recent = json.dumps(
        {"headers": {"doers_published_at_unix": time.time() - 5.0}, "body": "opaque"}
    ).encode()
    broker.rpush("worker", recent, stale)
    result = asyncio.run(_run_runtime_operational_snapshot())
    assert result["queues"]["worker"]["depth"] == 2
    assert result["queues"]["worker"]["oldest_age_seconds"] > 600
    _flush()
    _wait_for(
        lambda: _metric_value("doers.queue.oldest_message_age", queue="worker") > 300,
        description="Redis queue-age breach metric",
    )
    _assert_binding("queue_oldest_age_breach", "doers.queue.oldest_message_age")
    broker.delete("worker")


def test_40_real_database_pool_exhaustion_records_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import database as database_module
    from app.observability.runtime_probes import probe_api_runtime_once

    source = _url("TEST_DATABASE_URL").set(drivername="postgresql+asyncpg")
    engine = create_async_engine(
        source,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.2,
        pool_pre_ping=True,
        connect_args={"timeout": 1.0},
    )
    monkeypatch.setattr(database_module, "async_engine", engine)

    async def exhaust() -> None:
        held = await engine.connect()
        try:
            await probe_api_runtime_once()
        finally:
            await held.close()
            await engine.dispose()

    asyncio.run(exhaust())
    _flush()
    _wait_for(
        lambda: _metric_value("doers.database.pool.timeouts", pool="api") >= 1,
        description="real QueuePool timeout metric",
    )
    _assert_binding("database_pool_exhaustion", "doers.database.pool.timeouts")


def test_50_real_redis_outage_exports_broker_unhealthy_and_recovers() -> None:
    _stop_redis()
    try:
        result = asyncio.run(_run_runtime_operational_snapshot())
        assert result["redis"]["broker"] is False
        assert result["redis"]["result_backend"] is False
        _flush()
        _wait_for(
            lambda: any(
                float(record.get("value", 1.0)) == 0.0
                for record in _matching_records(
                    "doers.platform.redis.health", role="broker"
                )
            ),
            description="real broker-unavailable metric",
        )
    finally:
        _start_redis()
    recovered = asyncio.run(_run_runtime_operational_snapshot())
    assert recovered["redis"]["broker"] is True
    _assert_binding("redis_or_broker_unavailable", "doers.platform.redis.health")


def test_60_real_scheduler_contention_fails_closed_and_emits_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core import celery_beat_owner as beat_module

    broker_url = os.environ["REDIS_URL"]
    monkeypatch.setattr(beat_module.settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(beat_module.settings, "DOERS_PROCESS_PROFILE", "beat")
    monkeypatch.setattr(beat_module.settings, "REDIS_URL", broker_url)
    monkeypatch.setenv("CELERY_BEAT_OWNERSHIP_KEY", "doers:p8o:beat-owner")
    monkeypatch.setenv("CELERY_BEAT_OWNERSHIP_TTL_SECONDS", "10")
    monkeypatch.setenv("CELERY_BEAT_OWNERSHIP_RETRY_SECONDS", "1")
    monkeypatch.setenv("P6S_SCHEDULER_FAULTS", "1")

    client = redis.Redis.from_url(broker_url)
    client.delete("doers:p8o:beat-owner")
    monkeypatch.setenv("P6S_TEST_BEAT_OWNER_ID", "p8o-owner-a")
    first = beat_module.BeatOwnershipLease()
    monkeypatch.setenv("P6S_TEST_BEAT_OWNER_ID", "p8o-owner-b")
    second = beat_module.BeatOwnershipLease()
    try:
        assert first.ensure_owned() is True
        assert second.ensure_owned() is False
        _flush()
        _wait_for(
            lambda: bool(
                _matching_records(
                    "doers.platform.scheduler.ownership", state="contended"
                )
            ),
            description="scheduler contention metric",
        )
    finally:
        first.release()
        first.close()
        second.close()
        client.delete("doers:p8o:beat-owner")
    _assert_binding(
        "duplicate_scheduler_effect_risk", "doers.platform.scheduler.ownership"
    )


def test_70_external_backup_monitor_ingress_exports_stale_and_failed_without_fake_success() -> None:
    runtime_metrics().backup_snapshot(
        backup_class="database", age_seconds=90_100.0, failed=True
    )
    _flush()
    _wait_for(
        lambda: _metric_value("doers.platform.backup.age", backup_class="database")
        > 90_000,
        description="stale external backup evidence",
    )
    assert _metric_value(
        "doers.platform.backup.failures", backup_class="database"
    ) >= 1
    assert (
        json.loads(
            (ROOT / "docs/architecture/p8_metric_contract.json").read_text(
                encoding="utf-8"
            )
        )["truth_boundaries"]["backup_status"]
        == "external infrastructure backup system"
    )
    _assert_binding("backup_failure_or_stale_backup", "doers.platform.backup.age")


def test_80_real_asgi_failure_and_latency_emit_structured_context_and_slo_numerators() -> None:
    app = FastAPI()
    app.add_middleware(RequestObservabilityMiddleware)

    @app.get("/p8o/fail")
    async def p8o_fail():
        await asyncio.sleep(1.05)
        return JSONResponse({"status": "injected"}, status_code=500)

    stream = io.StringIO()
    import logging

    handler = logging.StreamHandler(stream)
    handler.setFormatter(StructuredJsonFormatter())
    logger = logging.getLogger("doers.observability.request")
    previous_level = logger.level
    previous_propagate = logger.propagate
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/p8o/fail", headers={"X-Request-ID": "p8o-api-slo"})
        assert response.status_code == 500
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate

    structured = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    completed = next(item for item in structured if item.get("fields", {}).get("event") == "api.request.completed")
    assert completed["request_id"] != "unknown"
    assert completed["correlation_id"] == "p8o-api-slo"

    _flush()
    _wait_for(
        lambda: _metric_value(
            "doers.api.errors", method="GET", route="/p8o/fail", status_class="5xx"
        )
        >= 1,
        description="API 5xx metric",
    )
    assert _histogram_sum(
        "doers.api.request.duration",
        method="GET",
        route="/p8o/fail",
        status_class="5xx",
    ) > 1000.0
    _assert_binding("api_availability_or_latency_slo_breach", "doers.api.errors")


class _SlowProviderHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        time.sleep(0.5)
        try:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        except OSError:
            return

    def log_message(self, _format: str, *_args) -> None:
        return


@contextmanager
def _slow_provider() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SlowProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/slow"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_85_real_provider_timeout_is_bridged_to_bounded_provider_metrics() -> None:
    with _slow_provider() as endpoint:
        started = time.monotonic()
        with pytest.raises(Exception):
            urlopen(endpoint, timeout=0.05).read()
        duration_ms = (time.monotonic() - started) * 1000
        record_provider_call(operation="search", outcome="timeout", duration_ms=duration_ms)
    _flush()
    _wait_for(
        lambda: _metric_value(
            "doers.provider.timeouts",
            provider="opensearch",
            operation="search",
            outcome="timeout",
        )
        >= 1,
        description="provider timeout metric",
    )
    assert _metric_value(
        "doers.provider.errors",
        provider="opensearch",
        operation="search",
        outcome="timeout",
    ) >= 1
    _assert_binding("provider_error_or_timeout_slo_breach", "doers.provider.timeouts")


def test_90_real_postgresql_service_outage_emits_disconnect_storm_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core import database as database_module
    from app.observability.runtime_probes import probe_api_runtime_once

    source = _url("TEST_DATABASE_URL").set(drivername="postgresql+asyncpg")
    engine = create_async_engine(
        source,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.3,
        pool_pre_ping=True,
        connect_args={"timeout": 0.5},
    )
    monkeypatch.setattr(database_module, "async_engine", engine)

    stopped = subprocess.run(
        ["sudo", "systemctl", "stop", "postgresql"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=20,
        check=False,
    )
    assert stopped.returncode == 0, stopped.stdout
    try:
        async def fail_five_times() -> None:
            for _ in range(5):
                await probe_api_runtime_once()

        asyncio.run(fail_five_times())
    finally:
        started = subprocess.run(
            ["sudo", "systemctl", "start", "postgresql"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20,
            check=False,
        )
        assert started.returncode == 0, started.stdout
        _wait_postgres()
        asyncio.run(engine.dispose())

    _flush()
    _wait_for(
        lambda: _metric_value("doers.database.disconnects", pool="api") >= 5,
        description="database disconnect storm metric",
    )
    _assert_binding("database_disconnect_storm", "doers.database.disconnects")


def test_95_real_otlp_sink_loss_does_not_block_durable_postgresql_commit() -> None:
    before_count = len(_capture_records())
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        dead_port = int(sock.getsockname()[1])

    shutdown_runtime_metrics(timeout_millis=1000)
    configure_runtime_metrics(
        endpoint=f"http://127.0.0.1:{dead_port}/v1/metrics",
        export_interval_seconds=300.0,
        export_timeout_seconds=0.2,
        environment="ci-p8o",
        service_name="doers-p8o-dead-sink",
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
                (org_id, "P8-O telemetry outage authority probe", f"p8o-otel-{org_id.hex}"),
            )
        connection.commit()
    force_flush_runtime_metrics(timeout_millis=1000)

    with _connect_admin() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM public.organizations WHERE id=%s", (org_id,))
            assert cursor.fetchone() == (1,)
    assert len(_capture_records()) == before_count
    assert "absent_over_time" in ALERTS["observability_pipeline_failure"]["query"]
    _assert_binding(
        "observability_pipeline_failure", "doers.platform.observability.heartbeat"
    )

    shutdown_runtime_metrics(timeout_millis=1000)
    configure_runtime_metrics(
        endpoint=OTLP_ENDPOINT,
        export_interval_seconds=1.0,
        export_timeout_seconds=1.0,
        environment="ci-p8o",
        service_name="doers-p8o-runtime-recovered",
    )
    runtime_metrics().telemetry_heartbeat(profile="maintenance")
    _flush()
    _wait_for(
        lambda: any(
            record.get("resource", {}).get("service.name")
            == "doers-p8o-runtime-recovered"
            and record.get("metric") == "doers.platform.observability.heartbeat"
            for record in _capture_records()
        ),
        description="OTLP recovery heartbeat",
    )


def test_99_all_captured_p8_metrics_remain_low_cardinality_and_non_authoritative() -> None:
    p8_records = [
        record
        for record in _capture_records()
        if str(record.get("metric", "")).startswith("doers.")
    ]
    assert p8_records
    for record in p8_records:
        attrs = set(record.get("attributes", {}))
        assert not (attrs & _FORBIDDEN_METRIC_KEYS), record
    assert CONTRACT["authority_assertions"] == {
        "postgresql_remains_durable_business_authority": True,
        "redis_is_delivery_coordination_only": True,
        "observability_is_evidence_only": True,
        "provider_ambiguity_never_authorizes_blind_retry": True,
        "telemetry_outage_never_rolls_back_or_blocks_durable_commit": True,
        "refund_provider_execution": "deferred_fail_closed",
    }
