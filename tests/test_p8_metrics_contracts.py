from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from app.observability.metrics_bootstrap import configure_process_runtime_metrics
from app.observability.runtime_metrics import (
    FORBIDDEN_METRIC_ATTRIBUTE_KEYS,
    RuntimeMetrics,
    safe_route_template,
)
from app.tasks.runtime_observability import _published_at


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "docs/architecture/p8_metric_contract.json"
RUNTIME_METRICS_SOURCE = (ROOT / "app/observability/runtime_metrics.py").read_text(encoding="utf-8")
TRACE_CONTEXT_SOURCE = (ROOT / "app/observability/trace_context.py").read_text(encoding="utf-8")
MAIN_SOURCE = (ROOT / "app/main.py").read_text(encoding="utf-8")
CELERY_SOURCE = (ROOT / "app/core/celery_app.py").read_text(encoding="utf-8")
CELERY_CONTEXT_SOURCE = (ROOT / "app/observability/celery_context.py").read_text(encoding="utf-8")
EXTERNAL_SNAPSHOT_SOURCE = (ROOT / "app/tasks/external_effect_observability.py").read_text(encoding="utf-8")
LIFECYCLE_SNAPSHOT_SOURCE = (ROOT / "app/tasks/branch_lifecycle_sweeps.py").read_text(encoding="utf-8")
RUNTIME_SNAPSHOT_SOURCE = (ROOT / "app/tasks/runtime_observability.py").read_text(encoding="utf-8")
BEAT_OWNER_SOURCE = (ROOT / "app/core/celery_beat_owner.py").read_text(encoding="utf-8")

P8L_CERTIFIED_HEAD = "0aee9c3e49f693132bb358334744c228b4848225"
P8L_CERTIFIED_TREE = "3c40259148eaed01e587c5d3921567d2577b8356"

EXPECTED_DOMAINS = {
    "api",
    "database",
    "queues",
    "lifecycle",
    "finance",
    "providers",
    "platform",
}
EXPECTED_METRICS = {
    "doers.api.requests",
    "doers.api.errors",
    "doers.api.request.duration",
    "doers.api.inflight",
    "doers.api.readiness",
    "doers.api.drain_rejections",
    "doers.database.pool.checked_out",
    "doers.database.pool.utilization",
    "doers.database.pool.wait",
    "doers.database.pool.timeouts",
    "doers.database.disconnects",
    "doers.queue.depth",
    "doers.queue.oldest_message_age",
    "doers.queue.redeliveries",
    "doers.queue.dead_letters",
    "doers.queue.worker.available",
    "doers.lifecycle.state.depth",
    "doers.lifecycle.replayed",
    "doers.lifecycle.compensations",
    "doers.finance.signal.depth",
    "doers.finance.idempotency_conflicts",
    "doers.provider.requests",
    "doers.provider.errors",
    "doers.provider.latency",
    "doers.provider.timeouts",
    "doers.provider.rate_limits",
    "doers.provider.circuit_open",
    "doers.platform.redis.health",
    "doers.platform.scheduler.ownership",
    "doers.platform.backup.age",
    "doers.platform.backup.failures",
}


def _contract() -> dict:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def _collect_metrics_data(metrics_data):
    observed = {}
    for resource_metrics in metrics_data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                observed[metric.name] = metric
    return observed


def test_p8m_is_exactly_bound_to_certified_p8l_parent() -> None:
    contract = _contract()
    assert contract["schema_version"] == 1
    assert contract["phase"] == "P8-M"
    assert contract["parent_certified_head"] == P8L_CERTIFIED_HEAD
    assert contract["parent_certified_tree"] == P8L_CERTIFIED_TREE
    assert contract["export"]["protocol"] == "otlp_http"
    assert contract["export"]["public_metrics_endpoint"] is False
    assert set(contract["export"]["production_endpoint_required_for_profiles"]) == {
        "api",
        "worker",
        "maintenance",
        "beat",
    }
    assert contract["export"]["recording_or_export_failure_is_business_authority"] is False


def test_metric_contract_covers_all_required_domains_and_has_no_forbidden_labels() -> None:
    contract = _contract()
    assert set(contract["domains"]) == EXPECTED_DOMAINS
    assert set(contract["forbidden_labels"]) == FORBIDDEN_METRIC_ATTRIBUTE_KEYS

    metrics = []
    for definitions in contract["domains"].values():
        metrics.extend(definitions)
    assert {definition["name"] for definition in metrics} == EXPECTED_METRICS

    for definition in metrics:
        assert not (set(definition["labels"]) & FORBIDDEN_METRIC_ATTRIBUTE_KEYS), definition
        assert definition["source"]

    assert contract["truth_boundaries"]["durable_dead_letters"] == "PostgreSQL"
    assert contract["truth_boundaries"]["lifecycle_state"] == "PostgreSQL"
    assert contract["truth_boundaries"]["backup_status"] == "external infrastructure backup system"
    assert contract["truth_boundaries"]["observability_is_business_authority"] is False
    assert "public_prometheus_endpoint" in contract["non_goals"]
    assert "refund_provider_activation" in contract["non_goals"]
    assert "release" in contract["non_goals"]
    assert "deployment" in contract["non_goals"]


def test_real_sdk_reader_observes_every_p8_metric_with_bounded_attributes() -> None:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    recorder = RuntimeMetrics(provider.get_meter("p8-certification", "1.0"))

    recorder.api_started(method="GET")
    recorder.api_completed(method="GET", route="/members/{member_id}", status_code=500, duration_ms=12.5)
    recorder.api_finished(method="GET")
    recorder.api_ready(False)
    recorder.api_drain_rejected(method="POST")

    recorder.database_pool_snapshot(pool="api", checked_out=7, capacity=30)
    recorder.database_pool_wait(pool="api", duration_ms=4.2)
    recorder.database_pool_timeout(pool="api")
    recorder.database_disconnect(pool="api")

    recorder.queue_snapshot(queue="worker", depth=8, oldest_age_seconds=31.0, dead_letters=2)
    recorder.queue_redelivery(queue="worker")
    recorder.worker_state(profile="worker", available=True)

    recorder.lifecycle_snapshot(pending=5, stuck=2, failed=1)
    recorder.lifecycle_replay(result="replayed")
    recorder.lifecycle_compensation(result="completed")

    recorder.finance_snapshot(
        reconciliation_mismatches=1,
        provider_ack_ambiguity=2,
        refund_obligations=3,
    )
    recorder.finance_idempotency_conflict()

    recorder.provider_call(
        provider="opensearch", operation="index", outcome="success", duration_ms=21.0
    )
    recorder.provider_call(
        provider="resend", operation="send", outcome="timeout", duration_ms=900.0
    )
    recorder.provider_call(
        provider="razorpay", operation="verify", outcome="rate_limited", duration_ms=25.0
    )
    recorder.provider_circuit_state(provider="resend", open_=True)

    recorder.redis_state(role="application", healthy=True)
    recorder.scheduler_state(state="owned")
    recorder.backup_snapshot(backup_class="database", age_seconds=600.0, failed=True)

    observed = _collect_metrics_data(reader.get_metrics_data())
    assert EXPECTED_METRICS <= set(observed)

    for metric in observed.values():
        for data_point in metric.data.data_points:
            attributes = dict(data_point.attributes)
            assert not (set(attributes) & FORBIDDEN_METRIC_ATTRIBUTE_KEYS), (
                metric.name,
                attributes,
            )
            for value in attributes.values():
                assert len(str(value)) <= 192

    provider.shutdown()


def test_metric_routes_never_accept_concrete_high_cardinality_entity_paths() -> None:
    concrete_uuid = "/members/123e4567-e89b-12d3-a456-426614174000"
    concrete_numeric = "/members/123456789"
    assert safe_route_template(concrete_uuid) == "unknown"
    assert safe_route_template(concrete_numeric) == "unknown"
    assert safe_route_template("/members/{member_id}") == "/members/{member_id}"
    assert safe_route_template("/api/v1/finance/payments/webhooks/razorpay") == (
        "/api/v1/finance/payments/webhooks/razorpay"
    )


def test_runtime_metric_recording_is_explicitly_fail_open_for_business_authority() -> None:
    assert "Telemetry must never become business authority" in RUNTIME_METRICS_SOURCE
    assert 'logger.warning("P8 metric recording failed: %s", operation, exc_info=True)' in RUNTIME_METRICS_SOURCE
    assert "except Exception:" in RUNTIME_METRICS_SOURCE


def test_production_processes_require_configured_p8_exporter_without_public_metrics_route() -> None:
    missing = SimpleNamespace(
        is_production=True,
        process_profile="api",
        P8_METRICS_OTLP_ENDPOINT="",
        P8_METRICS_EXPORT_INTERVAL_SECONDS=30.0,
        P8_METRICS_EXPORT_TIMEOUT_SECONDS=5.0,
        ENVIRONMENT="production",
    )
    with pytest.raises(RuntimeError, match="requires P8_METRICS_OTLP_ENDPOINT"):
        configure_process_runtime_metrics(missing)

    disabled_test = SimpleNamespace(
        is_production=False,
        process_profile="api",
        P8_METRICS_OTLP_ENDPOINT="",
        P8_METRICS_EXPORT_INTERVAL_SECONDS=30.0,
        P8_METRICS_EXPORT_TIMEOUT_SECONDS=5.0,
        ENVIRONMENT="test",
    )
    assert configure_process_runtime_metrics(disabled_test) is False
    assert '@app.get("/metrics")' not in MAIN_SOURCE
    assert '@app.route("/metrics")' not in MAIN_SOURCE


def test_trace_identity_is_finalized_from_authoritative_request_state_not_tenant_headers() -> None:
    assert "request_authority_context(request)" in TRACE_CONTEXT_SOURCE
    assert "finally:" in TRACE_CONTEXT_SOURCE
    assert 'span.set_attribute("tenant.id", context["tenant_id"])' in TRACE_CONTEXT_SOURCE
    assert 'span.set_attribute("branch.id", context["branch_id"])' in TRACE_CONTEXT_SOURCE
    assert "X-Tenant-ID" not in TRACE_CONTEXT_SOURCE
    assert "request.headers" not in TRACE_CONTEXT_SOURCE
    assert "AuthoritativeTraceContextMiddleware" in MAIN_SOURCE
    # Registration is reversed. OpenTelemetry is added after the authoritative
    # finalizer, making the server span outer and active when the finalizer exits.
    assert MAIN_SOURCE.index("app.add_middleware(AuthoritativeTraceContextMiddleware)") < MAIN_SOURCE.index(
        "app.add_middleware(OpenTelemetryTraceMiddleware)"
    )


def test_durable_queue_lifecycle_finance_and_platform_sources_are_real_not_mock_only() -> None:
    assert "app_secure.search_operational_snapshot()" in EXTERNAL_SNAPSHOT_SOURCE
    assert "app_secure.refund_execution_operational_snapshot()" in EXTERNAL_SNAPSHOT_SOURCE
    assert 'queue="search"' in EXTERNAL_SNAPSHOT_SOURCE
    assert 'queue="refund"' in EXTERNAL_SNAPSHOT_SOURCE
    assert "finance_snapshot(" in EXTERNAL_SNAPSHOT_SOURCE

    assert "OrgBranchState.lifecycle_transition_in_progress.is_(True)" in LIFECYCLE_SNAPSHOT_SOURCE
    assert 'BranchOutboxEvent.status == "dead_lettered"' in LIFECYCLE_SNAPSHOT_SOURCE
    assert 'queue="notification"' in LIFECYCLE_SNAPSHOT_SOURCE

    assert "await client.llen(queue)" in RUNTIME_SNAPSHOT_SOURCE
    assert "client.lindex(queue, 0)" in RUNTIME_SNAPSHOT_SOURCE
    assert "client.lindex(queue, -1)" in RUNTIME_SNAPSHOT_SOURCE
    assert 'role="broker"' in RUNTIME_SNAPSHOT_SOURCE
    assert 'role="result_backend"' in RUNTIME_SNAPSHOT_SOURCE

    assert 'state="owned" if owned else "contended"' in BEAT_OWNER_SOURCE
    assert 'state="unavailable"' in BEAT_OWNER_SOURCE


def test_celery_publish_age_header_is_scalar_and_p6_delivery_semantics_are_preserved() -> None:
    assert '_PUBLISHED_AT_HEADER = "doers_published_at_unix"' in CELERY_CONTEXT_SOURCE
    assert "headers[_PUBLISHED_AT_HEADER] = time.time()" in CELERY_CONTEXT_SOURCE
    assert "task_acks_late=True" in CELERY_SOURCE
    assert "task_reject_on_worker_lost=True" in CELERY_SOURCE
    assert "worker_prefetch_multiplier=1" in CELERY_SOURCE
    assert 'WORKER_QUEUE = "worker"' in CELERY_SOURCE
    assert 'MAINTENANCE_QUEUE = "lifecycle-maintenance"' in CELERY_SOURCE
    assert '"app.tasks.runtime_observability.snapshot"' in CELERY_SOURCE


def test_broker_message_timestamp_parser_reads_only_bounded_operational_header() -> None:
    payload = json.dumps(
        {
            "headers": {
                "doers_published_at_unix": 1_700_000_000.25,
                "request_id": "must-not-be-used-as-metric-label",
            },
            "body": "opaque",
        }
    ).encode()
    assert _published_at(payload) == 1_700_000_000.25
    assert _published_at(b"not-json") is None
    assert _published_at(json.dumps({"headers": {}})) is None
