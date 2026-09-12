from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)

from app.observability.notification_metrics import (
    configure_notification_metrics,
    force_flush_notification_metrics,
    record_operational_snapshot,
    record_provider_accepted,
    record_provider_call,
    record_reconciliation_result,
    shutdown_notification_metrics,
)
from app.services.notification_provider import NotificationProviderError, ResendEmailProvider


class _CollectorHandler(BaseHTTPRequestHandler):
    requests: list[tuple[str, str, bytes]] = []

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        type(self).requests.append((self.path, self.headers.get("Content-Type", ""), body))
        self.send_response(200)
        self.end_headers()

    def log_message(self, _format: str, *_args) -> None:
        return


def _decode(payload: bytes) -> ExportMetricsServiceRequest:
    request = ExportMetricsServiceRequest()
    request.ParseFromString(payload)
    return request


def _metric_names(request: ExportMetricsServiceRequest) -> set[str]:
    return {
        metric.name
        for resource_metrics in request.resource_metrics
        for scope_metrics in resource_metrics.scope_metrics
        for metric in scope_metrics.metrics
    }


def _metric_attribute_keys(request: ExportMetricsServiceRequest) -> set[str]:
    keys: set[str] = set()
    for resource_metrics in request.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                data = None
                if metric.HasField("sum"):
                    data = metric.sum
                elif metric.HasField("histogram"):
                    data = metric.histogram
                if data is None:
                    continue
                for point in data.data_points:
                    keys.update(attribute.key for attribute in point.attributes)
    return keys


def _resource_attributes(request: ExportMetricsServiceRequest) -> dict[str, str]:
    result: dict[str, str] = {}
    for resource_metrics in request.resource_metrics:
        for attribute in resource_metrics.resource.attributes:
            if attribute.value.HasField("string_value"):
                result[attribute.key] = attribute.value.string_value
    return result


def test_notification_metrics_export_real_otlp_http_payload() -> None:
    shutdown_notification_metrics(timeout_millis=1000)
    _CollectorHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CollectorHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}/v1/metrics"

    try:
        configure_notification_metrics(
            endpoint=endpoint,
            export_interval_seconds=300,
            export_timeout_seconds=2,
            environment="ci",
        )
        record_provider_call(operation="send", outcome="provider_accepted_nonterminal", duration_ms=9.5)
        record_provider_accepted()
        record_reconciliation_result(result="delivered")
        record_operational_snapshot(pending=3, dead_lettered=1, oldest_age_seconds=42.0)
        assert force_flush_notification_metrics(timeout_millis=2000)
        assert _CollectorHandler.requests
        path, content_type, body = _CollectorHandler.requests[-1]
        assert path == "/v1/metrics"
        assert "application/x-protobuf" in content_type
        request = _decode(body)
        assert {
            "doers.notification.provider.requests",
            "doers.notification.provider.latency",
            "doers.notification.delivery.accepted",
            "doers.notification.reconciliation.results",
            "doers.notification.backlog.oldest_age",
            "doers.notification.backlog.depth",
            "doers.notification.dlq.depth",
        } <= _metric_names(request)
        assert _metric_attribute_keys(request) <= {
            "provider", "operation", "outcome", "channel", "result"
        }
        attrs = _resource_attributes(request)
        assert attrs.get("service.name") == "doers-notification-worker"
        assert attrs.get("deployment.environment.name") == "ci"
    finally:
        shutdown_notification_metrics(timeout_millis=2000)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_production_notification_effect_is_blocked_before_provider_io_without_metrics() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, json={"id": "should-not-be-called"}, request=request)

    client = httpx.AsyncClient(
        base_url="https://api.resend.example.test",
        transport=httpx.MockTransport(handler),
    )
    provider = ResendEmailProvider(
        mode="resend",
        api_key="re_test_key",
        from_email="members@example.test",
        base_url="https://api.resend.example.test",
        client=client,
        metrics_required=True,
        metrics_otlp_endpoint="",
        environment="production",
    )
    try:
        with pytest.raises(NotificationProviderError) as exc_info:
            asyncio.run(
                provider.send(
                    destination="member@example.test",
                    member_name="Member",
                    template_key="branch_lifecycle_status_changed",
                    template_data={
                        "branch_name": "Main",
                        "from_status": "active",
                        "to_status": "temporarily_closed",
                    },
                    idempotency_key="branch-lifecycle/test/member/email",
                )
            )
    finally:
        asyncio.run(client.aclose())

    error = exc_info.value
    assert error.outcome == "retryable_failure"
    assert error.error_code == "notification_metrics_configuration_invalid"
    assert calls == []
