from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.observability.runtime_metrics import (
    configure_runtime_metrics,
    force_flush_runtime_metrics,
    runtime_metrics,
    shutdown_runtime_metrics,
)


ROOT = Path(__file__).resolve().parents[1]
ALERTS = ROOT / "docs/architecture/p8_alert_slo_contract.json"
CAPTURE = Path(os.environ.get("P8O_OTLP_CAPTURE_PATH", "/tmp/p8o-operational-otlp.jsonl"))


def _url(name: str):
    raw = os.environ.get(name, "").strip()
    if not raw:
        pytest.skip(f"{name} is required")
    return make_url(raw)


def _wait_for(predicate: Callable[[], Any], *, description: str, timeout: float = 10.0) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {description}; last={last!r}")


def _wait_postgres() -> None:
    admin = _url("TEST_ADMIN_DATABASE_URL")
    host = admin.host or "127.0.0.1"
    port = admin.port or 5432
    _wait_for(
        lambda: subprocess.run(
            ["pg_isready", "-h", host, "-p", str(port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0,
        description="PostgreSQL recovery",
        timeout=20.0,
    )


def _matching_records(metric: str, **attributes: str) -> list[dict[str, Any]]:
    if not CAPTURE.exists():
        return []
    matches: list[dict[str, Any]] = []
    for line in CAPTURE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("metric") != metric:
            continue
        observed = record.get("attributes", {})
        if all(str(observed.get(key)) == str(value) for key, value in attributes.items()):
            matches.append(record)
    return matches


def _counter_value(metric: str, **attributes: str) -> float:
    matches = _matching_records(metric, **attributes)
    if not matches:
        return 0.0
    return max(float(record.get("value", 0.0)) for record in matches)


def _flush() -> None:
    assert force_flush_runtime_metrics(timeout_millis=4000) is True


def test_real_postgresql_disconnect_storm_exports_five_increment_alert_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove the production alert numerator from an isolated real DB outage.

    The alert is defined as ``increase(...[5m]) >= 5``.  Capture the exported
    counter baseline before the destructive fault and require a real increase
    of at least five after five probe attempts.  The fault, runtime classifier,
    metric SDK and OTLP/HTTP exporter are all the production code paths.
    """

    assert os.environ.get("P8O_PROCESS_FAULTS") == "1"
    endpoint = os.environ.get("P8_METRICS_OTLP_ENDPOINT", "").strip()
    assert endpoint

    shutdown_runtime_metrics(timeout_millis=1000)
    configure_runtime_metrics(
        endpoint=endpoint,
        # Keep this destructive proof deterministic: the explicit force-flush
        # below is the scrape boundary, while production still uses its normal
        # periodic exporter interval.
        export_interval_seconds=300.0,
        export_timeout_seconds=1.0,
        environment="ci-p8o-db-disconnect",
        service_name="doers-p8o-db-disconnect",
    )
    runtime_metrics().telemetry_heartbeat(profile="api")
    _flush()

    baseline = _counter_value("doers.database.disconnects", pool="api")

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
    observed = _wait_for(
        lambda: _counter_value("doers.database.disconnects", pool="api"),
        description="database disconnect counter export",
    )
    assert observed - baseline >= 5, {
        "baseline": baseline,
        "observed": observed,
        "records": _matching_records("doers.database.disconnects", pool="api"),
    }

    contract = json.loads(ALERTS.read_text(encoding="utf-8"))
    alert = next(
        item
        for item in contract["critical_alerts"]
        if item["failure_mode"] == "database_disconnect_storm"
    )
    assert alert["signal"] == "doers.database.disconnects"
    assert ">= 5" in alert["threshold"] or ">=5" in alert["query"]

    shutdown_runtime_metrics(timeout_millis=1000)
