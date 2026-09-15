"""P8 process-local metrics bootstrap.

Production runtime profiles fail closed at startup if the P8 OTLP endpoint is
missing or invalid. Once configured, exporter failures remain telemetry-only and
must never mutate or block durable business authority.
"""

from __future__ import annotations

from typing import Any

from app.observability.runtime_metrics import configure_runtime_metrics, runtime_metrics


_RUNTIME_PROFILES = frozenset({"api", "worker", "maintenance", "beat"})


def service_name_for_profile(profile: str) -> str:
    normalized = str(profile or "").strip().lower()
    if normalized not in _RUNTIME_PROFILES:
        normalized = "unknown"
    return f"doers-{normalized}-runtime"


def configure_process_runtime_metrics(settings: Any) -> bool:
    profile = str(getattr(settings, "process_profile", "") or "").strip().lower()
    endpoint = str(getattr(settings, "P8_METRICS_OTLP_ENDPOINT", "") or "").strip()

    if bool(getattr(settings, "is_production", False)) and profile in _RUNTIME_PROFILES and not endpoint:
        raise RuntimeError(
            f"production {profile} process requires P8_METRICS_OTLP_ENDPOINT"
        )

    if not endpoint:
        return False

    configure_runtime_metrics(
        endpoint=endpoint,
        export_interval_seconds=float(
            getattr(settings, "P8_METRICS_EXPORT_INTERVAL_SECONDS", 30.0)
        ),
        export_timeout_seconds=float(
            getattr(settings, "P8_METRICS_EXPORT_TIMEOUT_SECONDS", 5.0)
        ),
        environment=str(getattr(settings, "ENVIRONMENT", "unknown")),
        service_name=service_name_for_profile(profile),
    )
    # Synchronous gauges retain their last observation for subsequent periodic
    # collections. This heartbeat therefore gives the alert backend a positive,
    # bounded signal for an otherwise idle runtime process without creating a
    # business dependency on telemetry delivery.
    runtime_metrics().telemetry_heartbeat(profile=profile)
    return True
