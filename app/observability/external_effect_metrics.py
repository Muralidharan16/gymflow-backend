"""Low-cardinality P4E operational snapshot metrics.

Only the isolated maintenance process configures this exporter. Snapshot values
come from the no-argument ``app_secure`` aggregate functions installed by zf07;
callers cannot supply metric labels, tenant IDs, entity IDs, or provider data.
"""

from __future__ import annotations

import math
import os
import threading
from collections.abc import Mapping
from urllib.parse import urlparse

from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource


_METER_NAME = "doers.external_effect"
_METER_VERSION = "1.0"
_SERVICE_NAME = "doers-external-effect-maintenance"
_LOCK = threading.Lock()
_PROVIDER: MeterProvider | None = None
_PROVIDER_PID: int | None = None
_PROVIDER_CONFIG: tuple[str, float, float, str] | None = None

_SEARCH_COUNT_STATES = {
    "pending_count": "pending",
    "processing_count": "processing",
    "dead_letter_count": "dead_lettered",
    "reconciliation_candidate_count": "reconciliation_candidate",
}
_REFUND_COUNT_STATES = {
    "pending_count": "pending",
    "processing_count": "processing",
    "retry_pending_count": "retry_pending",
    "provider_accepted_count": "provider_accepted",
    "reconciliation_pending_count": "reconciliation_pending",
    "dead_letter_count": "dead_lettered",
}
_SEARCH_AGE = "oldest_actionable_age_seconds"
_REFUND_AGE = "oldest_unresolved_age_seconds"


def _instrument_set(meter):
    return (
        meter.create_histogram(
            "doers.external_effect.snapshot.depth",
            unit="1",
            description="Aggregate durable external-effect work by domain and state",
        ),
        meter.create_histogram(
            "doers.external_effect.snapshot.oldest_age",
            unit="s",
            description="Age of the oldest actionable external-effect obligation",
        ),
    )


_SNAPSHOT_DEPTH, _SNAPSHOT_OLDEST_AGE = _instrument_set(
    metrics.get_meter(_METER_NAME, _METER_VERSION)
)


def validate_external_effect_metrics_configuration(
    *,
    endpoint: str,
    export_interval_seconds: float,
    export_timeout_seconds: float,
) -> None:
    parsed = urlparse(endpoint.strip())
    if not endpoint.strip() or parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("P4E_METRICS_OTLP_ENDPOINT must be an HTTP(S) URL")
    if not 1 <= float(export_interval_seconds) <= 300:
        raise ValueError("P4E_METRICS_EXPORT_INTERVAL_SECONDS must be in [1, 300]")
    if not 0 < float(export_timeout_seconds) <= 60:
        raise ValueError("P4E_METRICS_EXPORT_TIMEOUT_SECONDS must be in (0, 60]")


def configure_external_effect_metrics(
    *,
    endpoint: str,
    export_interval_seconds: float = 30.0,
    export_timeout_seconds: float = 5.0,
    environment: str = "unknown",
) -> None:
    """Install one process-local OTLP reader for P4E maintenance snapshots."""

    global _PROVIDER, _PROVIDER_PID, _PROVIDER_CONFIG
    global _SNAPSHOT_DEPTH, _SNAPSHOT_OLDEST_AGE

    validate_external_effect_metrics_configuration(
        endpoint=endpoint,
        export_interval_seconds=export_interval_seconds,
        export_timeout_seconds=export_timeout_seconds,
    )
    config = (
        endpoint.strip(),
        float(export_interval_seconds),
        float(export_timeout_seconds),
        environment.strip() or "unknown",
    )
    pid = os.getpid()

    with _LOCK:
        if _PROVIDER is not None and _PROVIDER_PID == pid:
            if _PROVIDER_CONFIG != config:
                raise RuntimeError(
                    "P4E operational metrics were already configured differently "
                    "in this process"
                )
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
                    "service.name": _SERVICE_NAME,
                    "deployment.environment.name": config[3],
                }
            ),
        )
        meter = provider.get_meter(_METER_NAME, _METER_VERSION)
        _SNAPSHOT_DEPTH, _SNAPSHOT_OLDEST_AGE = _instrument_set(meter)
        _PROVIDER = provider
        _PROVIDER_PID = pid
        _PROVIDER_CONFIG = config


def _validated_snapshot(
    snapshot: Mapping[str, object],
    *,
    count_states: Mapping[str, str],
    age_key: str,
) -> tuple[dict[str, int], float]:
    expected = set(count_states) | {age_key}
    if set(snapshot) != expected:
        raise ValueError("P4E operational snapshot shape is not certified")

    counts: dict[str, int] = {}
    for source_key, state in count_states.items():
        value = snapshot[source_key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("P4E operational snapshot counts must be non-negative integers")
        counts[state] = value

    age_value = snapshot[age_key]
    if isinstance(age_value, bool) or not isinstance(age_value, (int, float)):
        raise ValueError("P4E operational snapshot ages must be numeric")
    age = float(age_value)
    if not math.isfinite(age) or age < 0:
        raise ValueError("P4E operational snapshot ages must be finite and non-negative")
    return counts, age


def record_operational_snapshots(
    *,
    search: Mapping[str, object],
    refund: Mapping[str, object],
) -> None:
    search_counts, search_age = _validated_snapshot(
        search,
        count_states=_SEARCH_COUNT_STATES,
        age_key=_SEARCH_AGE,
    )
    refund_counts, refund_age = _validated_snapshot(
        refund,
        count_states=_REFUND_COUNT_STATES,
        age_key=_REFUND_AGE,
    )

    for domain, counts in (("search", search_counts), ("refund", refund_counts)):
        for state, value in counts.items():
            _SNAPSHOT_DEPTH.record(value, {"domain": domain, "state": state})
    _SNAPSHOT_OLDEST_AGE.record(search_age, {"domain": "search"})
    _SNAPSHOT_OLDEST_AGE.record(refund_age, {"domain": "refund"})


def force_flush_external_effect_metrics(*, timeout_millis: int = 5000) -> bool:
    provider = _PROVIDER
    if provider is None or _PROVIDER_PID != os.getpid():
        return False
    return bool(provider.force_flush(timeout_millis=timeout_millis))


def shutdown_external_effect_metrics(*, timeout_millis: int = 5000) -> None:
    global _PROVIDER, _PROVIDER_PID, _PROVIDER_CONFIG
    global _SNAPSHOT_DEPTH, _SNAPSHOT_OLDEST_AGE

    with _LOCK:
        provider = _PROVIDER if _PROVIDER_PID == os.getpid() else None
        _PROVIDER = None
        _PROVIDER_PID = None
        _PROVIDER_CONFIG = None
        _SNAPSHOT_DEPTH, _SNAPSHOT_OLDEST_AGE = _instrument_set(
            metrics.get_meter(_METER_NAME, _METER_VERSION)
        )
    if provider is not None:
        provider.shutdown(timeout_millis=timeout_millis)
