"""Low-cardinality, exportable P4B search provider metrics.

P4B uses a dedicated OTLP/HTTP metric reader instead of assuming that importing
the OpenTelemetry API makes metrics observable.  The recorder is initialized in
the process that performs search work, which is safe for Celery prefork workers.
No tenant, branch, document, URL or request identifiers are metric attributes.
"""

from __future__ import annotations

import logging
import os
import threading
from urllib.parse import urlparse

from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource


logger = logging.getLogger("doers.search_metrics")
_METER_NAME = "doers.search"
_METER_VERSION = "1.0"
_LOCK = threading.Lock()
_PROVIDER: MeterProvider | None = None
_PROVIDER_PID: int | None = None
_PROVIDER_CONFIG: tuple[str, float, float, str] | None = None


def _instrument_set(meter):
    return (
        meter.create_counter(
            "doers.search.provider.requests",
            unit="1",
            description="Search provider calls by operation and classified outcome",
        ),
        meter.create_histogram(
            "doers.search.provider.latency",
            unit="ms",
            description="End-to-end search provider mutation plus verification latency",
        ),
        meter.create_counter(
            "doers.search.reconciliation.drift_repairs",
            unit="1",
            description="Provider drift repair decisions made by the search worker",
        ),
    )


# Safe no-op/proxy instruments until an enabled production worker installs its
# dedicated SDK MeterProvider. Tests that construct the provider directly do not
# need an external collector merely to exercise provider semantics.
_PROVIDER_REQUESTS, _PROVIDER_LATENCY_MS, _DRIFT_REPAIRS = _instrument_set(
    metrics.get_meter(_METER_NAME, _METER_VERSION)
)


def validate_search_metrics_configuration(
    *,
    endpoint: str,
    export_interval_seconds: float,
    export_timeout_seconds: float,
) -> None:
    endpoint = endpoint.strip()
    if not endpoint:
        raise ValueError(
            "SEARCH_METRICS_OTLP_ENDPOINT is required when OpenSearch is enabled"
        )
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("SEARCH_METRICS_OTLP_ENDPOINT must be an HTTP(S) URL")
    if export_interval_seconds < 1 or export_interval_seconds > 300:
        raise ValueError(
            "SEARCH_METRICS_EXPORT_INTERVAL_SECONDS must be in the range [1, 300]"
        )
    if export_timeout_seconds <= 0 or export_timeout_seconds > 60:
        raise ValueError(
            "SEARCH_METRICS_EXPORT_TIMEOUT_SECONDS must be in the range (0, 60]"
        )


def configure_search_metrics(
    *,
    endpoint: str,
    export_interval_seconds: float = 30.0,
    export_timeout_seconds: float = 5.0,
    environment: str = "unknown",
) -> None:
    """Install one process-local OTLP metric reader for search work."""

    global _PROVIDER, _PROVIDER_PID, _PROVIDER_CONFIG
    global _PROVIDER_REQUESTS, _PROVIDER_LATENCY_MS, _DRIFT_REPAIRS

    endpoint = endpoint.strip()
    environment = environment.strip() or "unknown"
    validate_search_metrics_configuration(
        endpoint=endpoint,
        export_interval_seconds=export_interval_seconds,
        export_timeout_seconds=export_timeout_seconds,
    )
    config = (
        endpoint,
        float(export_interval_seconds),
        float(export_timeout_seconds),
        environment,
    )
    pid = os.getpid()

    with _LOCK:
        if _PROVIDER is not None and _PROVIDER_PID == pid:
            if _PROVIDER_CONFIG != config:
                raise RuntimeError(
                    "P4B search metrics were already configured differently in this process"
                )
            return

        # Never call shutdown on a MeterProvider inherited from a parent after
        # fork. P4B configures lazily in the process that performs provider work.
        exporter = OTLPMetricExporter(
            endpoint=endpoint,
            timeout=float(export_timeout_seconds),
        )
        reader = PeriodicExportingMetricReader(
            exporter,
            export_interval_millis=int(float(export_interval_seconds) * 1000),
            export_timeout_millis=int(float(export_timeout_seconds) * 1000),
        )
        provider = MeterProvider(
            metric_readers=[reader],
            resource=Resource.create(
                {
                    "service.name": "doers-search-worker",
                    "deployment.environment.name": environment,
                }
            ),
        )
        meter = provider.get_meter(_METER_NAME, _METER_VERSION)
        (
            _PROVIDER_REQUESTS,
            _PROVIDER_LATENCY_MS,
            _DRIFT_REPAIRS,
        ) = _instrument_set(meter)
        _PROVIDER = provider
        _PROVIDER_PID = pid
        _PROVIDER_CONFIG = config


def force_flush_search_metrics(*, timeout_millis: int = 5000) -> bool:
    provider = _PROVIDER
    if provider is None or _PROVIDER_PID != os.getpid():
        return False
    return bool(provider.force_flush(timeout_millis=timeout_millis))


def shutdown_search_metrics(*, timeout_millis: int = 5000) -> None:
    """Flush/shutdown the process-local provider; primarily useful for tests."""

    global _PROVIDER, _PROVIDER_PID, _PROVIDER_CONFIG
    global _PROVIDER_REQUESTS, _PROVIDER_LATENCY_MS, _DRIFT_REPAIRS

    with _LOCK:
        provider = _PROVIDER if _PROVIDER_PID == os.getpid() else None
        _PROVIDER = None
        _PROVIDER_PID = None
        _PROVIDER_CONFIG = None
        (
            _PROVIDER_REQUESTS,
            _PROVIDER_LATENCY_MS,
            _DRIFT_REPAIRS,
        ) = _instrument_set(metrics.get_meter(_METER_NAME, _METER_VERSION))
    if provider is not None:
        provider.shutdown(timeout_millis=timeout_millis)


def record_provider_call(*, operation: str, outcome: str, duration_ms: float) -> None:
    attributes = {
        "provider": "opensearch",
        "operation": operation,
        "outcome": outcome,
    }
    _PROVIDER_REQUESTS.add(1, attributes)
    _PROVIDER_LATENCY_MS.record(max(0.0, float(duration_ms)), attributes)


def record_drift_repair(*, operation: str, result: str) -> None:
    _DRIFT_REPAIRS.add(
        1,
        {
            "provider": "opensearch",
            "operation": operation,
            "result": result,
        },
    )
