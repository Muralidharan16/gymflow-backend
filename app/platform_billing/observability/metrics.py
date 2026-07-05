"""
app/platform_billing/observability/metrics.py
==============================================
Phase 2 observability metric interfaces.

These define the metric names and types used by Platform Billing
resolvers and shadow comparison. Actual monitoring vendor integration
is deferred to a later phase; these are callable interfaces that
structured logging or basic counters can satisfy.
"""

METRIC_NAMES = {
    "shadow_resolution_total": "platform_billing_shadow_resolution_total",
    "shadow_resolution_duration_ms": "platform_billing_shadow_resolution_duration_ms",
    "shadow_mismatch_total": "platform_billing_shadow_mismatch_total",
    "projection_stale_total": "platform_billing_projection_stale_total",
    "projection_refresh_total": "platform_billing_projection_refresh_total",
    "projection_refresh_failed_total": "platform_billing_projection_refresh_failed_total",
    "usage_measurement_total": "platform_billing_usage_measurement_total",
    "access_decisions_total": "platform_billing_access_decisions_total",
    "capability_decision_total": "platform_billing_capability_decision_total",
    "capability_denied_total": "platform_billing_capability_denied_total",
    "capability_unavailable_total": "platform_billing_capability_unavailable_total",
    "capability_fallback_total": "platform_billing_capability_fallback_total",
    "legacy_new_mismatch_total": "platform_billing_legacy_new_mismatch_total",
    "read_api_total": "platform_billing_read_api_total",
}


class MetricsCollector:
    """
    Lightweight metric collector for Phase 2.

    Replace with Prometheus Counter/Gauge/Histogram objects when
    a monitoring vendor is integrated.
    """

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}
        self._duration_sums: dict[str, float] = {}

    def increment(self, name: str, labels: dict[str, str] | None = None) -> None:
        key = name + (str(sorted(labels.items())) if labels else "")
        self._counters[key] = self._counters.get(key, 0) + 1

    def record_duration(self, name: str, duration_ms: float) -> None:
        key = name
        self._duration_sums[key] = self._duration_sums.get(key, 0.0) + duration_ms
        self._counters[f"{name}_count"] = self._counters.get(f"{name}_count", 0) + 1

    def get_counter(self, name: str) -> int:
        return self._counters.get(name, 0)

    def reset(self) -> None:
        self._counters.clear()
        self._duration_sums.clear()


# Module-level singleton
_metrics = MetricsCollector()


def get_metrics() -> MetricsCollector:
    return _metrics
