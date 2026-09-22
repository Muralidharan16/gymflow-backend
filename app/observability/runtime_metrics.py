"""P8 bounded-cardinality runtime metrics.

Observability is evidence only. These instruments never participate in business
state decisions and every recording path is fail-open: a broken telemetry sink
must not break API, database, queue, lifecycle, Finance or provider authority.
"""

from __future__ import annotations

import logging
import math
import os
import re
import threading
from typing import Callable
from urllib.parse import urlparse

from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource


logger = logging.getLogger("doers.observability.metrics")
_METER_NAME = "doers.runtime"
_METER_VERSION = "1.0"
_LOCK = threading.Lock()
_PROVIDER: MeterProvider | None = None
_PROVIDER_PID: int | None = None
_PROVIDER_CONFIG: tuple[str, float, float, str, str] | None = None

FORBIDDEN_METRIC_ATTRIBUTE_KEYS = frozenset(
    {
        "request_id",
        "correlation_id",
        "tenant_id",
        "branch_id",
        "principal_id",
        "saga_id",
        "task_id",
        "trace_id",
        "span_id",
    }
)

_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
_STATUS_CLASSES = frozenset({"1xx", "2xx", "3xx", "4xx", "5xx", "unknown"})
_PROCESS_PROFILES = frozenset({"api", "worker", "maintenance", "beat", "finance_config", "unknown"})
_DATABASE_POOLS = frozenset({"api", "worker", "maintenance", "finance", "unknown"})
_QUEUES = frozenset(
    {
        "worker",
        "lifecycle-maintenance",
        "search",
        "notification",
        "refund",
        "branch-outbox",
        "outbox",
        "unknown",
    }
)
_LIFECYCLE_STATES = frozenset({"pending", "stuck", "failed", "unknown"})
_FINANCE_SIGNALS = frozenset(
    {
        "reconciliation_mismatch",
        "idempotency_conflict",
        "provider_ack_ambiguity",
        "refund_obligation",
        "unknown",
    }
)
_FINANCE_SECURITY_EVENTS = frozenset(
    {
        "finance.security.identity_mismatch",
        "finance.security.revocation_backend_unavailable",
        "finance.security.csrf_rejected",
        "finance.offline_payment.maker_checker_rejected",
        "finance.offline_payment.velocity_rejected",
        "finance.offline_payment.prepared",
        "finance.offline_payment.approved",
        "finance.offline_payment.rejected",
        "finance.checkout.initiated",
        "unknown",
    }
)
_SECURITY_SEVERITIES = frozenset({"info", "warning", "critical", "unknown"})
_FINANCE_MANDATE_STATES = frozenset({"failed", "expired", "revoked", "unknown"})
_FINANCE_DUNNING_STAGES = frozenset(
    {
        "full_grace",
        "limited_write",
        "read_only",
        "billing_only",
        "recovered",
        "unknown",
    }
)
_WEBHOOK_SIGNATURE_FAILURE_REASONS = frozenset({"missing", "invalid", "unknown"})
_PROVIDERS = frozenset(
    {
        "opensearch",
        "resend",
        "razorpay",
        "aws_kms",
        "s3",
        "google_maps",
        "unknown",
    }
)
_PROVIDER_OPERATIONS = frozenset(
    {
        "search",
        "index",
        "delete",
        "send",
        "verify",
        "reconcile",
        "refund",
        "upload",
        "download",
        "encrypt",
        "decrypt",
        "unknown",
        "other",
    }
)
_PROVIDER_OUTCOMES = frozenset(
    {
        "success",
        "error",
        "timeout",
        "rate_limited",
        "circuit_open",
        "rejected",
        "unknown",
    }
)
_BACKUP_CLASSES = frozenset({"database", "object_storage", "configuration", "unknown"})
_SCHEDULER_STATES = frozenset({"owned", "contended", "unavailable", "released", "unknown"})
_REDIS_ROLES = frozenset({"application", "broker", "result_backend", "unknown"})

_UUIDISH_RE = re.compile(
    r"(?i)(?:[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}|\b\d{7,}\b)"
)
_SAFE_ROUTE_RE = re.compile(r"^/[A-Za-z0-9_./{}:-]{0,180}$")


def _enum(value: object, allowed: frozenset[str], *, upper: bool = False) -> str:
    normalized = str(value or "").strip()
    normalized = normalized.upper() if upper else normalized.lower()
    return normalized if normalized in allowed else "unknown"


def safe_route_template(value: object) -> str:
    """Accept only router templates; reject likely concrete entity paths."""

    route = str(value or "").strip()
    if not route or route == "unknown":
        return "unknown"
    if len(route) > 192 or not _SAFE_ROUTE_RE.fullmatch(route):
        return "unknown"
    if _UUIDISH_RE.search(route):
        return "unknown"
    return route


def _status_class(status_code: object) -> str:
    try:
        code = int(status_code)
    except (TypeError, ValueError):
        return "unknown"
    candidate = f"{code // 100}xx" if 100 <= code <= 599 else "unknown"
    return candidate if candidate in _STATUS_CLASSES else "unknown"


def _finite_non_negative(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) and number >= 0 else 0.0


def _count(value: object) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


class RuntimeMetrics:
    """One process-local metric instrument set with finite label domains."""

    def __init__(self, meter) -> None:
        # API
        self.api_requests = meter.create_counter("doers.api.requests", unit="1")
        self.api_errors = meter.create_counter("doers.api.errors", unit="1")
        self.api_latency = meter.create_histogram("doers.api.request.duration", unit="ms")
        self.api_inflight = meter.create_up_down_counter("doers.api.inflight", unit="1")
        self.api_readiness = meter.create_gauge("doers.api.readiness", unit="1")
        self.api_drain_rejections = meter.create_counter("doers.api.drain_rejections", unit="1")

        # Database
        self.db_checked_out = meter.create_gauge("doers.database.pool.checked_out", unit="1")
        self.db_utilization = meter.create_gauge("doers.database.pool.utilization", unit="1")
        self.db_wait = meter.create_histogram("doers.database.pool.wait", unit="ms")
        self.db_timeouts = meter.create_counter("doers.database.pool.timeouts", unit="1")
        self.db_disconnects = meter.create_counter("doers.database.disconnects", unit="1")

        # Queues/workers
        self.queue_depth = meter.create_gauge("doers.queue.depth", unit="1")
        self.queue_oldest_age = meter.create_gauge("doers.queue.oldest_message_age", unit="s")
        self.queue_redeliveries = meter.create_counter("doers.queue.redeliveries", unit="1")
        self.queue_dead_letters = meter.create_gauge("doers.queue.dead_letters", unit="1")
        self.worker_available = meter.create_gauge("doers.queue.worker.available", unit="1")

        # Lifecycle
        self.lifecycle_depth = meter.create_gauge("doers.lifecycle.state.depth", unit="1")
        self.lifecycle_replayed = meter.create_counter("doers.lifecycle.replayed", unit="1")
        self.lifecycle_compensations = meter.create_counter("doers.lifecycle.compensations", unit="1")

        # Finance
        self.finance_signal = meter.create_gauge("doers.finance.signal.depth", unit="1")
        self.finance_idempotency_conflicts = meter.create_counter(
            "doers.finance.idempotency_conflicts", unit="1"
        )
        self.finance_security_events = meter.create_counter(
            "doers.finance.security_events", unit="1"
        )

        # PAY-18 financial operations. Snapshot-derived totals/backlogs are
        # gauges because PostgreSQL durable state is authoritative and can move
        # both up and down as work progresses. Signature failures are a counter
        # because rejected signatures intentionally never become durable rows.
        self.payment_attempt_total = meter.create_gauge(
            "payment_attempt_total", unit="1"
        )
        self.payment_failure_total = meter.create_gauge(
            "payment_failure_total", unit="1"
        )
        self.payment_unknown_total = meter.create_gauge(
            "payment_unknown_total", unit="1"
        )
        self.webhook_signature_failure_total = meter.create_counter(
            "webhook_signature_failure", unit="1"
        )
        self.webhook_backlog = meter.create_gauge(
            "webhook_backlog", unit="1"
        )
        self.payment_application_backlog = meter.create_gauge(
            "payment_application_backlog", unit="1"
        )
        self.finance_outbox_backlog = meter.create_gauge(
            "finance_outbox_backlog", unit="1"
        )
        self.refund_backlog = meter.create_gauge(
            "refund_backlog", unit="1"
        )
        self.refund_unknown_total = meter.create_gauge(
            "refund_unknown_total", unit="1"
        )
        self.settlement_mismatch_total = meter.create_gauge(
            "settlement_mismatch_total", unit="1"
        )
        self.reconciliation_open_total = meter.create_gauge(
            "reconciliation_open_total", unit="1"
        )
        self.mandate_failure_total = meter.create_gauge(
            "mandate_failure_total", unit="1"
        )
        self.dunning_stage_total = meter.create_gauge(
            "dunning_stage_total", unit="1"
        )
        self.chargeback_open_total = meter.create_gauge(
            "chargeback_open_total", unit="1"
        )
        self.duplicate_payment_allegation_open_total = meter.create_gauge(
            "duplicate_payment_allegation_open_total", unit="1"
        )

        # Providers
        self.provider_requests = meter.create_counter("doers.provider.requests", unit="1")
        self.provider_errors = meter.create_counter("doers.provider.errors", unit="1")
        self.provider_latency = meter.create_histogram("doers.provider.latency", unit="ms")
        self.provider_timeouts = meter.create_counter("doers.provider.timeouts", unit="1")
        self.provider_rate_limits = meter.create_counter("doers.provider.rate_limits", unit="1")
        self.provider_circuit_open = meter.create_gauge("doers.provider.circuit_open", unit="1")

        # Platform
        self.redis_health = meter.create_gauge("doers.platform.redis.health", unit="1")
        self.scheduler_ownership = meter.create_gauge("doers.platform.scheduler.ownership", unit="1")
        self.backup_age = meter.create_gauge("doers.platform.backup.age", unit="s")
        self.backup_failures = meter.create_counter("doers.platform.backup.failures", unit="1")
        # A persistent gauge is exported every collection interval after one
        # process-local set. Missing samples therefore distinguish telemetry loss
        # from an otherwise idle but healthy runtime profile.
        self.observability_heartbeat = meter.create_gauge(
            "doers.platform.observability.heartbeat", unit="1"
        )

    @staticmethod
    def _safe(operation: str, recorder: Callable[[], None]) -> bool:
        try:
            recorder()
            return True
        except Exception:
            # Telemetry must never become business authority or availability authority.
            logger.warning("P8 metric recording failed: %s", operation, exc_info=True)
            return False

    def api_started(self, *, method: str) -> bool:
        attrs = {"method": _enum(method, _HTTP_METHODS, upper=True)}
        return self._safe("api_started", lambda: self.api_inflight.add(1, attrs))

    def api_completed(
        self,
        *,
        method: str,
        route: str,
        status_code: int,
        duration_ms: float,
    ) -> bool:
        attrs = {
            "method": _enum(method, _HTTP_METHODS, upper=True),
            "route": safe_route_template(route),
            "status_class": _status_class(status_code),
        }

        def _record() -> None:
            self.api_requests.add(1, attrs)
            self.api_latency.record(_finite_non_negative(duration_ms), attrs)
            if attrs["status_class"] == "5xx":
                self.api_errors.add(1, attrs)

        return self._safe("api_completed", _record)

    def api_finished(self, *, method: str) -> bool:
        attrs = {"method": _enum(method, _HTTP_METHODS, upper=True)}
        return self._safe("api_finished", lambda: self.api_inflight.add(-1, attrs))

    def api_ready(self, ready: bool) -> bool:
        return self._safe("api_ready", lambda: self.api_readiness.set(1 if ready else 0, {}))

    def api_drain_rejected(self, *, method: str) -> bool:
        attrs = {"method": _enum(method, _HTTP_METHODS, upper=True)}
        return self._safe(
            "api_drain_rejected", lambda: self.api_drain_rejections.add(1, attrs)
        )

    def database_pool_snapshot(
        self,
        *,
        pool: str,
        checked_out: int,
        capacity: int,
    ) -> bool:
        pool_name = _enum(pool, _DATABASE_POOLS)
        used = _count(checked_out)
        cap = max(1, _count(capacity))
        attrs = {"pool": pool_name}

        def _record() -> None:
            self.db_checked_out.set(used, attrs)
            self.db_utilization.set(min(1.0, used / cap), attrs)

        return self._safe("database_pool_snapshot", _record)

    def database_pool_wait(self, *, pool: str, duration_ms: float) -> bool:
        attrs = {"pool": _enum(pool, _DATABASE_POOLS)}
        return self._safe(
            "database_pool_wait",
            lambda: self.db_wait.record(_finite_non_negative(duration_ms), attrs),
        )

    def database_pool_timeout(self, *, pool: str) -> bool:
        attrs = {"pool": _enum(pool, _DATABASE_POOLS)}
        return self._safe("database_pool_timeout", lambda: self.db_timeouts.add(1, attrs))

    def database_disconnect(self, *, pool: str) -> bool:
        attrs = {"pool": _enum(pool, _DATABASE_POOLS)}
        return self._safe("database_disconnect", lambda: self.db_disconnects.add(1, attrs))

    def queue_snapshot(
        self,
        *,
        queue: str,
        depth: int,
        oldest_age_seconds: float,
        dead_letters: int,
    ) -> bool:
        attrs = {"queue": _enum(queue, _QUEUES)}

        def _record() -> None:
            self.queue_depth.set(_count(depth), attrs)
            self.queue_oldest_age.set(_finite_non_negative(oldest_age_seconds), attrs)
            self.queue_dead_letters.set(_count(dead_letters), attrs)

        return self._safe("queue_snapshot", _record)

    def queue_redelivery(self, *, queue: str) -> bool:
        attrs = {"queue": _enum(queue, _QUEUES)}
        return self._safe("queue_redelivery", lambda: self.queue_redeliveries.add(1, attrs))

    def worker_state(self, *, profile: str, available: bool) -> bool:
        attrs = {"profile": _enum(profile, _PROCESS_PROFILES)}
        return self._safe(
            "worker_state", lambda: self.worker_available.set(1 if available else 0, attrs)
        )

    def lifecycle_snapshot(self, *, pending: int, stuck: int, failed: int) -> bool:
        values = {"pending": pending, "stuck": stuck, "failed": failed}

        def _record() -> None:
            for state, value in values.items():
                self.lifecycle_depth.set(
                    _count(value), {"state": _enum(state, _LIFECYCLE_STATES)}
                )

        return self._safe("lifecycle_snapshot", _record)

    def lifecycle_replay(self, *, result: str = "replayed") -> bool:
        attrs = {"result": "replayed" if str(result).lower() == "replayed" else "other"}
        return self._safe("lifecycle_replay", lambda: self.lifecycle_replayed.add(1, attrs))

    def lifecycle_compensation(self, *, result: str) -> bool:
        normalized = str(result or "").strip().lower()
        if normalized not in {"completed", "skipped", "failed"}:
            normalized = "unknown"
        return self._safe(
            "lifecycle_compensation",
            lambda: self.lifecycle_compensations.add(1, {"result": normalized}),
        )

    def finance_snapshot(
        self,
        *,
        reconciliation_mismatches: int,
        provider_ack_ambiguity: int,
        refund_obligations: int,
    ) -> bool:
        values = {
            "reconciliation_mismatch": reconciliation_mismatches,
            "provider_ack_ambiguity": provider_ack_ambiguity,
            "refund_obligation": refund_obligations,
        }

        def _record() -> None:
            for signal, value in values.items():
                self.finance_signal.set(
                    _count(value), {"signal": _enum(signal, _FINANCE_SIGNALS)}
                )

        return self._safe("finance_snapshot", _record)

    def finance_idempotency_conflict(self) -> bool:
        return self._safe(
            "finance_idempotency_conflict",
            lambda: self.finance_idempotency_conflicts.add(
                1, {"signal": "idempotency_conflict"}
            ),
        )

    def finance_security_event(
        self,
        *,
        event: str,
        severity: str,
    ) -> bool:
        attrs = {
            "event": _enum(event, _FINANCE_SECURITY_EVENTS),
            "severity": _enum(severity, _SECURITY_SEVERITIES),
        }
        return self._safe(
            "finance_security_event",
            lambda: self.finance_security_events.add(1, attrs),
        )

    def financial_observability_snapshot(
        self,
        *,
        payment_attempt_total: int,
        payment_failure_total: int,
        payment_unknown_total: int,
        webhook_backlog: int,
        payment_application_backlog: int,
        finance_outbox_backlog: int,
        refund_backlog: int,
        refund_unknown_total: int,
        settlement_mismatch_total: int,
        reconciliation_open_total: int,
        mandate_failed_total: int,
        mandate_expired_total: int,
        mandate_revoked_total: int,
        dunning_full_grace_total: int,
        dunning_limited_write_total: int,
        dunning_read_only_total: int,
        dunning_billing_only_total: int,
        dunning_recovered_total: int,
        chargeback_open_total: int,
        duplicate_payment_allegation_open_total: int,
    ) -> bool:
        """Record aggregate financial truth without entity-identity labels."""

        def _record() -> None:
            self.payment_attempt_total.set(_count(payment_attempt_total), {})
            self.payment_failure_total.set(_count(payment_failure_total), {})
            self.payment_unknown_total.set(_count(payment_unknown_total), {})
            self.webhook_backlog.set(_count(webhook_backlog), {})
            self.payment_application_backlog.set(
                _count(payment_application_backlog), {}
            )
            self.finance_outbox_backlog.set(_count(finance_outbox_backlog), {})
            self.refund_backlog.set(_count(refund_backlog), {})
            self.refund_unknown_total.set(_count(refund_unknown_total), {})
            self.settlement_mismatch_total.set(
                _count(settlement_mismatch_total), {}
            )
            self.reconciliation_open_total.set(
                _count(reconciliation_open_total), {}
            )

            mandate_values = {
                "failed": mandate_failed_total,
                "expired": mandate_expired_total,
                "revoked": mandate_revoked_total,
            }
            for state, value in mandate_values.items():
                self.mandate_failure_total.set(
                    _count(value),
                    {"state": _enum(state, _FINANCE_MANDATE_STATES)},
                )

            dunning_values = {
                "full_grace": dunning_full_grace_total,
                "limited_write": dunning_limited_write_total,
                "read_only": dunning_read_only_total,
                "billing_only": dunning_billing_only_total,
                "recovered": dunning_recovered_total,
            }
            for stage, value in dunning_values.items():
                self.dunning_stage_total.set(
                    _count(value),
                    {"stage": _enum(stage, _FINANCE_DUNNING_STAGES)},
                )

            self.chargeback_open_total.set(_count(chargeback_open_total), {})
            self.duplicate_payment_allegation_open_total.set(
                _count(duplicate_payment_allegation_open_total), {}
            )

        return self._safe("financial_observability_snapshot", _record)

    def webhook_signature_failure(
        self,
        *,
        provider: str,
        reason: str,
    ) -> bool:
        attrs = {
            "provider": _enum(provider, _PROVIDERS),
            "reason": _enum(reason, _WEBHOOK_SIGNATURE_FAILURE_REASONS),
        }
        return self._safe(
            "webhook_signature_failure",
            lambda: self.webhook_signature_failure_total.add(1, attrs),
        )

    def provider_call(
        self,
        *,
        provider: str,
        operation: str,
        outcome: str,
        duration_ms: float,
    ) -> bool:
        provider_name = _enum(provider, _PROVIDERS)
        operation_name = _enum(operation, _PROVIDER_OPERATIONS)
        outcome_name = _enum(outcome, _PROVIDER_OUTCOMES)
        attrs = {
            "provider": provider_name,
            "operation": operation_name,
            "outcome": outcome_name,
        }

        def _record() -> None:
            self.provider_requests.add(1, attrs)
            self.provider_latency.record(_finite_non_negative(duration_ms), attrs)
            if outcome_name not in {"success", "unknown"}:
                self.provider_errors.add(1, attrs)
            if outcome_name == "timeout":
                self.provider_timeouts.add(1, attrs)
            elif outcome_name == "rate_limited":
                self.provider_rate_limits.add(1, attrs)

        return self._safe("provider_call", _record)

    def provider_circuit_state(self, *, provider: str, open_: bool) -> bool:
        attrs = {"provider": _enum(provider, _PROVIDERS)}
        return self._safe(
            "provider_circuit_state",
            lambda: self.provider_circuit_open.set(1 if open_ else 0, attrs),
        )

    def redis_state(self, *, role: str, healthy: bool) -> bool:
        attrs = {"role": _enum(role, _REDIS_ROLES)}
        return self._safe(
            "redis_state", lambda: self.redis_health.set(1 if healthy else 0, attrs)
        )

    def scheduler_state(self, *, state: str) -> bool:
        normalized = _enum(state, _SCHEDULER_STATES)
        value = 1 if normalized == "owned" else 0
        return self._safe(
            "scheduler_state",
            lambda: self.scheduler_ownership.set(value, {"state": normalized}),
        )

    def backup_snapshot(
        self,
        *,
        backup_class: str,
        age_seconds: float,
        failed: bool,
    ) -> bool:
        attrs = {"backup_class": _enum(backup_class, _BACKUP_CLASSES)}

        def _record() -> None:
            self.backup_age.set(_finite_non_negative(age_seconds), attrs)
            if failed:
                self.backup_failures.add(1, attrs)

        return self._safe("backup_snapshot", _record)

    def telemetry_heartbeat(self, *, profile: str) -> bool:
        attrs = {"profile": _enum(profile, _PROCESS_PROFILES)}
        return self._safe(
            "telemetry_heartbeat",
            lambda: self.observability_heartbeat.set(1, attrs),
        )


_RUNTIME = RuntimeMetrics(metrics.get_meter(_METER_NAME, _METER_VERSION))


def validate_runtime_metrics_configuration(
    *, endpoint: str, export_interval_seconds: float, export_timeout_seconds: float
) -> None:
    endpoint = endpoint.strip()
    parsed = urlparse(endpoint)
    if not endpoint or parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("P8_METRICS_OTLP_ENDPOINT must be an HTTP(S) URL")
    if not 1 <= float(export_interval_seconds) <= 300:
        raise ValueError("P8_METRICS_EXPORT_INTERVAL_SECONDS must be in [1, 300]")
    if not 0 < float(export_timeout_seconds) <= 60:
        raise ValueError("P8_METRICS_EXPORT_TIMEOUT_SECONDS must be in (0, 60]")


def configure_runtime_metrics(
    *,
    endpoint: str,
    export_interval_seconds: float = 30.0,
    export_timeout_seconds: float = 5.0,
    environment: str = "unknown",
    service_name: str = "doers-runtime",
) -> None:
    global _PROVIDER, _PROVIDER_PID, _PROVIDER_CONFIG, _RUNTIME

    validate_runtime_metrics_configuration(
        endpoint=endpoint,
        export_interval_seconds=export_interval_seconds,
        export_timeout_seconds=export_timeout_seconds,
    )
    config = (
        endpoint.strip(),
        float(export_interval_seconds),
        float(export_timeout_seconds),
        environment.strip() or "unknown",
        service_name.strip() or "doers-runtime",
    )
    pid = os.getpid()
    with _LOCK:
        if _PROVIDER is not None and _PROVIDER_PID == pid:
            if _PROVIDER_CONFIG != config:
                raise RuntimeError("P8 runtime metrics already configured differently in this process")
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
        _RUNTIME = RuntimeMetrics(provider.get_meter(_METER_NAME, _METER_VERSION))
        _PROVIDER = provider
        _PROVIDER_PID = pid
        _PROVIDER_CONFIG = config


def force_flush_runtime_metrics(*, timeout_millis: int = 5000) -> bool:
    provider = _PROVIDER
    if provider is None or _PROVIDER_PID != os.getpid():
        return False
    try:
        return bool(provider.force_flush(timeout_millis=timeout_millis))
    except Exception:
        logger.warning("P8 metric flush failed", exc_info=True)
        return False


def shutdown_runtime_metrics(*, timeout_millis: int = 5000) -> None:
    global _PROVIDER, _PROVIDER_PID, _PROVIDER_CONFIG, _RUNTIME
    with _LOCK:
        provider = _PROVIDER if _PROVIDER_PID == os.getpid() else None
        _PROVIDER = None
        _PROVIDER_PID = None
        _PROVIDER_CONFIG = None
        _RUNTIME = RuntimeMetrics(metrics.get_meter(_METER_NAME, _METER_VERSION))
    if provider is not None:
        try:
            provider.shutdown(timeout_millis=timeout_millis)
        except Exception:
            logger.warning("P8 metric shutdown failed", exc_info=True)


def runtime_metrics() -> RuntimeMetrics:
    return _RUNTIME
