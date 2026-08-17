"""Low-cardinality, process-local OTLP metrics for P4C notifications."""

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


logger = logging.getLogger("doers.notification_metrics")
_METER_NAME = "doers.notification"
_METER_VERSION = "1.0"
_LOCK = threading.Lock()
_PROVIDER: MeterProvider | None = None
_PROVIDER_PID: int | None = None
_PROVIDER_CONFIG: tuple[str, float, float, str, str] | None = None


def _instrument_set(meter):
    return (
        meter.create_counter(
            "doers.notification.provider.requests",
            unit="1",
            description="Notification provider calls by operation and classified outcome",
        ),
        meter.create_histogram(
            "doers.notification.provider.latency",
            unit="ms",
            description="Notification provider request latency",
        ),
        meter.create_counter(
            "doers.notification.delivery.accepted",
            unit="1",
            description="Provider-accepted notification commands awaiting terminal evidence",
        ),
        meter.create_counter(
            "doers.notification.reconciliation.results",
            unit="1",
            description="Notification provider reconciliation outcomes",
        ),
        meter.create_histogram(
            "doers.notification.backlog.oldest_age",
            unit="s",
            description="Age of the oldest pending notification command",
        ),
        meter.create_histogram(
            "doers.notification.backlog.depth",
            unit="1",
            description="Snapshot of pending notification command depth",
        ),
        meter.create_histogram(
            "doers.notification.dlq.depth",
            unit="1",
            description="Snapshot of notification dead-letter depth",
        ),
    )


(
    _PROVIDER_REQUESTS,
    _PROVIDER_LATENCY_MS,
    _DELIVERY_ACCEPTED,
    _RECONCILIATION_RESULTS,
    _BACKLOG_AGE,
    _BACKLOG_DEPTH,
    _DLQ_DEPTH,
) = _instrument_set(metrics.get_meter(_METER_NAME, _METER_VERSION))


def validate_notification_metrics_configuration(
    *, endpoint: str, export_interval_seconds: float, export_timeout_seconds: float
) -> None:
    endpoint = endpoint.strip()
    parsed = urlparse(endpoint)
    if not endpoint or parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("NOTIFICATION_METRICS_OTLP_ENDPOINT must be an HTTP(S) URL")
    if not 1 <= float(export_interval_seconds) <= 300:
        raise ValueError("NOTIFICATION_METRICS_EXPORT_INTERVAL_SECONDS must be in [1, 300]")
    if not 0 < float(export_timeout_seconds) <= 60:
        raise ValueError("NOTIFICATION_METRICS_EXPORT_TIMEOUT_SECONDS must be in (0, 60]")


def configure_notification_metrics(
    *,
    endpoint: str,
    export_interval_seconds: float = 30.0,
    export_timeout_seconds: float = 5.0,
    environment: str = "unknown",
    service_name: str = "doers-notification-worker",
) -> None:
    global _PROVIDER, _PROVIDER_PID, _PROVIDER_CONFIG
    global _PROVIDER_REQUESTS, _PROVIDER_LATENCY_MS, _DELIVERY_ACCEPTED
    global _RECONCILIATION_RESULTS, _BACKLOG_AGE, _BACKLOG_DEPTH, _DLQ_DEPTH

    validate_notification_metrics_configuration(
        endpoint=endpoint,
        export_interval_seconds=export_interval_seconds,
        export_timeout_seconds=export_timeout_seconds,
    )
    config = (
        endpoint.strip(),
        float(export_interval_seconds),
        float(export_timeout_seconds),
        environment.strip() or "unknown",
        service_name.strip() or "doers-notification-worker",
    )
    pid = os.getpid()
    with _LOCK:
        if _PROVIDER is not None and _PROVIDER_PID == pid:
            if _PROVIDER_CONFIG != config:
                raise RuntimeError("P4C notification metrics already configured differently in this process")
            return
        exporter = OTLPMetricExporter(endpoint=config[0], timeout=config[2])
        reader = PeriodicExportingMetricReader(
            exporter,
            export_interval_millis=int(config[1] * 1000),
            export_timeout_millis=int(config[2] * 1000),
        )
        provider = MeterProvider(
            metric_readers=[reader],
            resource=Resource.create(
                {
                    "service.name": config[4],
                    "deployment.environment.name": config[3],
                }
            ),
        )
        meter = provider.get_meter(_METER_NAME, _METER_VERSION)
        (
            _PROVIDER_REQUESTS,
            _PROVIDER_LATENCY_MS,
            _DELIVERY_ACCEPTED,
            _RECONCILIATION_RESULTS,
            _BACKLOG_AGE,
            _BACKLOG_DEPTH,
            _DLQ_DEPTH,
        ) = _instrument_set(meter)
        _PROVIDER = provider
        _PROVIDER_PID = pid
        _PROVIDER_CONFIG = config


def force_flush_notification_metrics(*, timeout_millis: int = 5000) -> bool:
    provider = _PROVIDER
    if provider is None or _PROVIDER_PID != os.getpid():
        return False
    return bool(provider.force_flush(timeout_millis=timeout_millis))


def shutdown_notification_metrics(*, timeout_millis: int = 5000) -> None:
    global _PROVIDER, _PROVIDER_PID, _PROVIDER_CONFIG
    global _PROVIDER_REQUESTS, _PROVIDER_LATENCY_MS, _DELIVERY_ACCEPTED
    global _RECONCILIATION_RESULTS, _BACKLOG_AGE, _BACKLOG_DEPTH, _DLQ_DEPTH
    with _LOCK:
        provider = _PROVIDER if _PROVIDER_PID == os.getpid() else None
        _PROVIDER = None
        _PROVIDER_PID = None
        _PROVIDER_CONFIG = None
        (
            _PROVIDER_REQUESTS,
            _PROVIDER_LATENCY_MS,
            _DELIVERY_ACCEPTED,
            _RECONCILIATION_RESULTS,
            _BACKLOG_AGE,
            _BACKLOG_DEPTH,
            _DLQ_DEPTH,
        ) = _instrument_set(metrics.get_meter(_METER_NAME, _METER_VERSION))
    if provider is not None:
        provider.shutdown(timeout_millis=timeout_millis)


def record_provider_call(*, operation: str, outcome: str, duration_ms: float) -> None:
    attrs = {"provider": "resend", "operation": operation, "outcome": outcome}
    _PROVIDER_REQUESTS.add(1, attrs)
    _PROVIDER_LATENCY_MS.record(max(0.0, float(duration_ms)), attrs)


def record_provider_accepted() -> None:
    _DELIVERY_ACCEPTED.add(1, {"provider": "resend", "channel": "email"})


def record_reconciliation_result(*, result: str) -> None:
    _RECONCILIATION_RESULTS.add(1, {"provider": "resend", "result": result})


def record_operational_snapshot(*, pending: int, dead_lettered: int, oldest_age_seconds: float) -> None:
    _BACKLOG_DEPTH.record(max(0, int(pending)), {"channel": "email"})
    _DLQ_DEPTH.record(max(0, int(dead_lettered)), {"channel": "email"})
    _BACKLOG_AGE.record(max(0.0, float(oldest_age_seconds)), {"channel": "email"})
