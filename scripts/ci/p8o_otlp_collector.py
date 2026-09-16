from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)


_LOCK = threading.Lock()
_SUM_LOCK = threading.Lock()
_CAPTURE_PATH: Path | None = None
# The production P8 alert backend is Prometheus-compatible and evaluates
# ``increase(..._total[window])`` over monotonic counters. OTLP exporters may
# legitimately send Counter sums with DELTA temporality, especially when a
# fault spans more than one export interval. Keep a disposable collector-local
# running total for DELTA sums so the captured series has the same cumulative
# semantics that the Prometheus adapter exposes. This is evidence normalization
# only; it never participates in application or business authority.
_DELTA_SUM_TOTALS: dict[tuple[str, tuple[tuple[str, Any], ...], tuple[tuple[str, Any], ...]], float] = {}
_OTLP_AGGREGATION_TEMPORALITY_DELTA = 1


def _any_value(value) -> Any:
    if value.HasField("string_value"):
        return value.string_value
    if value.HasField("bool_value"):
        return value.bool_value
    if value.HasField("int_value"):
        return int(value.int_value)
    if value.HasField("double_value"):
        return float(value.double_value)
    return None


def _attributes(items) -> dict[str, Any]:
    return {item.key: _any_value(item.value) for item in items}


def _number(point) -> float:
    selected = point.WhichOneof("value")
    if selected == "as_int":
        return float(point.as_int)
    if selected == "as_double":
        return float(point.as_double)
    return 0.0


def _normalized_sum_value(
    *,
    metric_name: str,
    point,
    resource: dict[str, Any],
    aggregation_temporality: int,
) -> float:
    value = _number(point)
    if aggregation_temporality != _OTLP_AGGREGATION_TEMPORALITY_DELTA:
        return value

    attributes = _attributes(point.attributes)
    key = (
        metric_name,
        tuple(sorted(attributes.items())),
        tuple(sorted(resource.items())),
    )
    with _SUM_LOCK:
        total = _DELTA_SUM_TOTALS.get(key, 0.0) + value
        _DELTA_SUM_TOTALS[key] = total
    return total


def _records(payload: bytes) -> list[dict[str, Any]]:
    request = ExportMetricsServiceRequest()
    request.ParseFromString(payload)
    received_at = time.time()
    records: list[dict[str, Any]] = []

    for resource_metrics in request.resource_metrics:
        resource = _attributes(resource_metrics.resource.attributes)
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.HasField("gauge"):
                    for point in metric.gauge.data_points:
                        records.append(
                            {
                                "received_at": received_at,
                                "metric": metric.name,
                                "kind": "gauge",
                                "attributes": _attributes(point.attributes),
                                "value": _number(point),
                                "resource": resource,
                            }
                        )
                elif metric.HasField("sum"):
                    temporality = int(metric.sum.aggregation_temporality)
                    for point in metric.sum.data_points:
                        records.append(
                            {
                                "received_at": received_at,
                                "metric": metric.name,
                                "kind": "sum",
                                "attributes": _attributes(point.attributes),
                                "value": _normalized_sum_value(
                                    metric_name=metric.name,
                                    point=point,
                                    resource=resource,
                                    aggregation_temporality=temporality,
                                ),
                                "aggregation_temporality": temporality,
                                "resource": resource,
                            }
                        )
                elif metric.HasField("histogram"):
                    for point in metric.histogram.data_points:
                        records.append(
                            {
                                "received_at": received_at,
                                "metric": metric.name,
                                "kind": "histogram",
                                "attributes": _attributes(point.attributes),
                                "count": int(point.count),
                                "sum": float(point.sum),
                                "resource": resource,
                            }
                        )
    return records


def _append(records: list[dict[str, Any]]) -> None:
    if not records:
        return
    path = _CAPTURE_PATH
    if path is None:
        raise RuntimeError("P8-O collector capture path is not configured")
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")))
                handle.write("\n")


class CollectorHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/healthz":
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok\n")

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/metrics":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        try:
            _append(_records(body))
        except Exception:
            self.send_response(400)
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()

    def log_message(self, _format: str, *_args) -> None:
        return


def main() -> int:
    parser = argparse.ArgumentParser(description="Disposable P8-O OTLP/HTTP metric collector")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4318)
    parser.add_argument("--capture", required=True)
    args = parser.parse_args()

    global _CAPTURE_PATH
    _CAPTURE_PATH = Path(args.capture).resolve()
    _CAPTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CAPTURE_PATH.write_text("", encoding="utf-8")
    with _SUM_LOCK:
        _DELTA_SUM_TOTALS.clear()

    server = ThreadingHTTPServer((args.host, args.port), CollectorHandler)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
