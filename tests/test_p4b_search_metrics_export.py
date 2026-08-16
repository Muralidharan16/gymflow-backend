from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)

from app.observability.search_metrics import (
    configure_search_metrics,
    force_flush_search_metrics,
    record_drift_repair,
    record_provider_call,
    shutdown_search_metrics,
)
from app.services.search_provider import OpenSearchProvider, SearchProviderError


class _CollectorHandler(BaseHTTPRequestHandler):
    requests: list[tuple[str, str, bytes]] = []

    def do_POST(self) -> None:  # noqa: N802 - stdlib HTTP handler contract
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        type(self).requests.append(
            (self.path, self.headers.get("Content-Type", ""), body)
        )
        self.send_response(200)
        self.end_headers()

    def log_message(self, _format: str, *_args) -> None:
        return


def _metric_names(payload: bytes) -> set[str]:
    request = ExportMetricsServiceRequest()
    request.ParseFromString(payload)
    return {
        metric.name
        for resource_metrics in request.resource_metrics
        for scope_metrics in resource_metrics.scope_metrics
        for metric in scope_metrics.metrics
    }


def test_search_metrics_export_real_otlp_http_payload() -> None:
    shutdown_search_metrics(timeout_millis=1000)
    _CollectorHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CollectorHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    endpoint = f"http://127.0.0.1:{server.server_port}/v1/metrics"
    try:
        configure_search_metrics(
            endpoint=endpoint,
            export_interval_seconds=300,
            export_timeout_seconds=2,
            environment="ci",
        )
        record_provider_call(
            operation="index",
            outcome="definite_success",
            duration_ms=12.5,
        )
        record_drift_repair(operation="index", result="requeued")
        assert force_flush_search_metrics(timeout_millis=2000)

        assert _CollectorHandler.requests
        path, content_type, body = _CollectorHandler.requests[-1]
        assert path == "/v1/metrics"
        assert "application/x-protobuf" in content_type
        assert body
        assert {
            "doers.search.provider.requests",
            "doers.search.provider.latency",
            "doers.search.reconciliation.drift_repairs",
        } <= _metric_names(body)
    finally:
        shutdown_search_metrics(timeout_millis=2000)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_production_search_effect_is_blocked_before_provider_io_without_metrics() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(500, request=request)

    client = httpx.AsyncClient(
        base_url="https://search.example.test",
        transport=httpx.MockTransport(handler),
    )
    provider = OpenSearchProvider(
        mode="opensearch",
        base_url="https://search.example.test",
        index="branches-v1",
        client=client,
        metrics_required=True,
        metrics_otlp_endpoint="",
        environment="production",
    )

    try:
        with pytest.raises(SearchProviderError) as exc_info:
            asyncio.run(
                provider.apply(
                    branch_id="44000000-0000-4000-8000-000000000011",
                    operation="index",
                    desired_version=1,
                    document={"branch_id": "44000000-0000-4000-8000-000000000011"},
                )
            )
    finally:
        asyncio.run(client.aclose())

    error = exc_info.value
    assert error.error_code == "search_metrics_configuration_invalid"
    assert error.outcome == "retryable_failure"
    assert calls == []
