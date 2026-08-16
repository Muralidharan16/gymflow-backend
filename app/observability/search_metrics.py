"""Low-cardinality P4B search provider metrics.

The module uses the repository's OpenTelemetry API dependency so worker processes
can export through the runtime-configured MeterProvider without introducing a
second metrics stack.  No tenant, branch, document or URL values are labels.
"""

from __future__ import annotations

from opentelemetry import metrics


_METER = metrics.get_meter("doers.search", "1.0")
_PROVIDER_REQUESTS = _METER.create_counter(
    "doers.search.provider.requests",
    unit="1",
    description="Search provider calls by operation and classified outcome",
)
_PROVIDER_LATENCY_MS = _METER.create_histogram(
    "doers.search.provider.latency",
    unit="ms",
    description="End-to-end search provider mutation plus verification latency",
)
_DRIFT_REPAIRS = _METER.create_counter(
    "doers.search.reconciliation.drift_repairs",
    unit="1",
    description="Provider drift repair decisions made by the search worker",
)


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
