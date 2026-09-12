from __future__ import annotations

import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import pytest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)
from pydantic import ValidationError

from app.core.config import Settings
from app.observability.external_effect_metrics import (
    configure_external_effect_metrics,
    force_flush_external_effect_metrics,
    record_operational_snapshots,
    shutdown_external_effect_metrics,
)


_BASE = {
    "REDIS_URL": "redis://localhost:6379/0",
    "CELERY_BROKER_URL": "redis://localhost:6379/1",
    "CELERY_RESULT_BACKEND": "redis://localhost:6379/2",
    "SECRET_KEY": "test-secret",
    "AWS_ACCESS_KEY_ID": "test",
    "AWS_SECRET_ACCESS_KEY": "test",
    "ENVIRONMENT": "production",
}
_API = "postgresql+asyncpg://api_login@localhost/doers"
_AUTH = "postgresql+asyncpg://auth_login@localhost/doers"
_WORKER = "postgresql+asyncpg://worker_login@localhost/doers"
_MAINTENANCE = "postgresql+asyncpg://maintenance_login@localhost/doers"
_ENDPOINT = "https://otel.example.test/v1/metrics"

_SEARCH = {
    "pending_count": 3,
    "processing_count": 2,
    "dead_letter_count": 1,
    "reconciliation_candidate_count": 4,
    "oldest_actionable_age_seconds": 42.5,
}
_REFUND = {
    "pending_count": 5,
    "processing_count": 4,
    "retry_pending_count": 3,
    "provider_accepted_count": 2,
    "reconciliation_pending_count": 1,
    "dead_letter_count": 6,
    "oldest_unresolved_age_seconds": 84.5,
}


class _CollectorHandler(BaseHTTPRequestHandler):
    requests: list[tuple[str, str, bytes]] = []

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        type(self).requests.append(
            (self.path, self.headers.get("Content-Type", ""), body)
        )
        self.send_response(200)
        self.end_headers()

    def log_message(self, _format: str, *_args) -> None:
        return


def _decode(payload: bytes) -> ExportMetricsServiceRequest:
    request = ExportMetricsServiceRequest()
    request.ParseFromString(payload)
    return request


def _metrics(request: ExportMetricsServiceRequest):
    return [
        metric
        for resource_metrics in request.resource_metrics
        for scope_metrics in resource_metrics.scope_metrics
        for metric in scope_metrics.metrics
    ]


def _point_attributes(request: ExportMetricsServiceRequest) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for metric in _metrics(request):
        data = metric.histogram if metric.HasField("histogram") else None
        if data is None:
            continue
        for point in data.data_points:
            values: dict[str, str] = {}
            for attribute in point.attributes:
                if attribute.value.HasField("string_value"):
                    values[attribute.key] = attribute.value.string_value
            result.append(values)
    return result


def _resource_attributes(request: ExportMetricsServiceRequest) -> dict[str, str]:
    result: dict[str, str] = {}
    for resource_metrics in request.resource_metrics:
        for attribute in resource_metrics.resource.attributes:
            if attribute.value.HasField("string_value"):
                result[attribute.key] = attribute.value.string_value
    return result


def _settings(**values) -> Settings:
    with patch.dict(os.environ, {}, clear=True):
        return Settings(_env_file=None, **(_BASE | values))


def test_external_effect_metrics_export_real_low_cardinality_otlp_payload() -> None:
    shutdown_external_effect_metrics(timeout_millis=1000)
    _CollectorHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CollectorHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    endpoint = f"http://127.0.0.1:{server.server_port}/v1/metrics"
    try:
        configure_external_effect_metrics(
            endpoint=endpoint,
            export_interval_seconds=300,
            export_timeout_seconds=2,
            environment="ci",
        )
        record_operational_snapshots(search=_SEARCH, refund=_REFUND)
        assert force_flush_external_effect_metrics(timeout_millis=2000)

        assert _CollectorHandler.requests
        path, content_type, body = _CollectorHandler.requests[-1]
        assert path == "/v1/metrics"
        assert "application/x-protobuf" in content_type
        request = _decode(body)
        assert {metric.name for metric in _metrics(request)} == {
            "doers.external_effect.snapshot.depth",
            "doers.external_effect.snapshot.oldest_age",
        }
        attributes = _point_attributes(request)
        assert attributes
        assert {key for item in attributes for key in item} <= {"domain", "state"}
        assert {item["domain"] for item in attributes} == {"search", "refund"}
        assert {item["state"] for item in attributes if "state" in item} == {
            "pending",
            "processing",
            "dead_lettered",
            "reconciliation_candidate",
            "retry_pending",
            "provider_accepted",
            "reconciliation_pending",
        }
        resource = _resource_attributes(request)
        assert resource["service.name"] == "doers-external-effect-maintenance"
        assert resource["deployment.environment.name"] == "ci"
    finally:
        shutdown_external_effect_metrics(timeout_millis=2000)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_operational_metric_shape_rejects_identifiers_and_invalid_values() -> None:
    with pytest.raises(ValueError, match="shape is not certified"):
        record_operational_snapshots(
            search=_SEARCH | {"tenant_id": "not-allowed"},
            refund=_REFUND,
        )
    with pytest.raises(ValueError, match="non-negative integers"):
        record_operational_snapshots(
            search=_SEARCH | {"pending_count": -1},
            refund=_REFUND,
        )
    with pytest.raises(ValueError, match="finite and non-negative"):
        record_operational_snapshots(
            search=_SEARCH,
            refund=_REFUND | {"oldest_unresolved_age_seconds": float("inf")},
        )


def test_production_maintenance_requires_valid_p4e_metrics_configuration() -> None:
    settings = _settings(
        DOERS_PROCESS_PROFILE="maintenance",
        CELERY_WORKER_PROFILE="maintenance",
        MAINTENANCE_DATABASE_URL=_MAINTENANCE,
        P4E_METRICS_OTLP_ENDPOINT=_ENDPOINT,
    )
    assert settings.P4E_METRICS_OTLP_ENDPOINT == _ENDPOINT

    with pytest.raises(ValidationError, match="required for the production maintenance"):
        _settings(
            DOERS_PROCESS_PROFILE="maintenance",
            CELERY_WORKER_PROFILE="maintenance",
            MAINTENANCE_DATABASE_URL=_MAINTENANCE,
        )
    with pytest.raises(ValidationError, match=r"must be an HTTP\(S\) URL"):
        _settings(
            DOERS_PROCESS_PROFILE="maintenance",
            CELERY_WORKER_PROFILE="maintenance",
            MAINTENANCE_DATABASE_URL=_MAINTENANCE,
            P4E_METRICS_OTLP_ENDPOINT="file:///tmp/metrics",
        )


@pytest.mark.parametrize(
    ("profile", "values"),
    (
        (
            "api",
            {"DATABASE_URL": _API, "AUTH_DATABASE_URL": _AUTH},
        ),
        (
            "worker",
            {
                "CELERY_WORKER_PROFILE": "worker",
                "WORKER_DATABASE_URL": _WORKER,
            },
        ),
        ("beat", {}),
    ),
)
def test_non_maintenance_profiles_reject_p4e_metrics_endpoint(
    profile: str, values: dict[str, str]
) -> None:
    with pytest.raises(ValidationError, match="restricted to the maintenance profile"):
        _settings(
            DOERS_PROCESS_PROFILE=profile,
            P4E_METRICS_OTLP_ENDPOINT=_ENDPOINT,
            **values,
        )
