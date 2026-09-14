from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

import pytest
import redis


ROOT = Path(__file__).resolve().parents[1]
_DATABASE = "gymflow_p6w_test"
_LIFECYCLE_INDEX = 1


def _load_p5w2():
    path = ROOT / "tests" / "test_p5w2_worker_crash_redelivery_runtime.py"
    spec = importlib.util.spec_from_file_location("p6w_reused_p5w2_runtime", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


P5 = _load_p5w2()
P5._DATABASE = _DATABASE
P5._AUTH_LOGIN = "auth_p6w_runtime"
P5._APP_LOGIN = "app_p6w_runtime"
P5._WORKER_LOGIN = "worker_p6w_runtime"


def _safe_p6w_broker() -> redis.Redis:
    raw = os.environ.get("CELERY_BROKER_URL", "")
    parsed = urlparse(raw)
    if (
        parsed.scheme != "rediss"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.port != 16380
        or parsed.path != "/1"
    ):
        raise RuntimeError(f"unsafe P6-W broker URL: {raw!r}")
    client = redis.Redis.from_url(
        raw,
        socket_connect_timeout=1.0,
        socket_timeout=1.0,
        decode_responses=True,
    )
    if client.ping() is not True:
        raise RuntimeError("P6-W Redis broker did not answer PING")
    return client


P5._safe_broker = _safe_p6w_broker
_ORIGINAL_P5_RUNNING_WORKER = P5._running_worker


@contextmanager
def _reduced_p5_running_worker(*args, **kwargs):
    auth_password = os.environ.pop("AUTH_RUNTIME_PASSWORD", None)
    try:
        with _ORIGINAL_P5_RUNNING_WORKER(*args, **kwargs) as worker:
            yield worker
    finally:
        if auth_password is not None:
            os.environ["AUTH_RUNTIME_PASSWORD"] = auth_password


P5._running_worker = _reduced_p5_running_worker


def _worker_environment(
    telemetry: Path,
    hold_sentinel: Path,
    release_path: Path,
    event_id: uuid.UUID,
) -> dict[str, str]:
    environment = os.environ.copy()
    for forbidden in (
        "DATABASE_URL",
        "AUTH_DATABASE_URL",
        "MAINTENANCE_DATABASE_URL",
        "FINANCE_CONFIG_DATABASE_URL",
        "TEST_DATABASE_URL",
        "TEST_ADMIN_DATABASE_URL",
        "P6W_APP_DATABASE_URL",
        "P6W_WORKER_DATABASE_URL",
        "MIGRATION_PASSWORD",
        "AUTH_RUNTIME_PASSWORD",
        "APP_RUNTIME_PASSWORD",
        "WORKER_RUNTIME_PASSWORD",
    ):
        environment.pop(forbidden, None)
    environment.update(
        {
            "ENVIRONMENT": "production",
            "DOERS_PROCESS_PROFILE": "worker",
            "CELERY_WORKER_PROFILE": "worker",
            "NOTIFICATION_EMAIL_PROVIDER_MODE": "disabled",
            "P4C_RESEND_API_KEY": "",
            "RESEND_WEBHOOK_SECRET": "",
            "NOTIFICATION_METRICS_OTLP_ENDPOINT": "",
            "SEARCH_PROVIDER_MODE": "disabled",
            "OPENSEARCH_URL": "",
            "OPENSEARCH_USERNAME": "",
            "OPENSEARCH_PASSWORD": "",
            "SEARCH_METRICS_OTLP_ENDPOINT": "",
            "P4E_METRICS_OTLP_ENDPOINT": "",
            "P6W_TELEMETRY_PATH": str(telemetry),
            "P6W_HOLD_SENTINEL": str(hold_sentinel),
            "P6W_RELEASE_PATH": str(release_path),
            "P6W_TARGET_EVENT_ID": str(event_id),
            "PYTHONUNBUFFERED": "1",
        }
    )
    return environment


@contextmanager
def _running_sigterm_worker(tmp_path: Path, event_id: uuid.UUID) -> Iterator[object]:
    token = uuid.uuid4().hex
    hostname = f"p6w-sigterm-{token}@localhost"
    queue = f"p6w-sigterm-{token}"
    telemetry = tmp_path / f"telemetry-{token}.jsonl"
    hold_sentinel = tmp_path / f"hold-{token}.sentinel"
    release_path = tmp_path / f"release-{token}.sentinel"
    log = tmp_path / f"worker-{token}.log"
    environment = _worker_environment(
        telemetry,
        hold_sentinel,
        release_path,
        event_id,
    )
    with log.open("w", encoding="utf-8") as log_stream:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "celery",
                "-A",
                "app.core.celery_app:celery_app",
                "worker",
                "--pool=prefork",
                "--concurrency=2",
                "--prefetch-multiplier=1",
                f"--queues={queue}",
                f"--hostname={hostname}",
                "--without-gossip",
                "--without-mingle",
                "--loglevel=INFO",
                "--include=scripts.ci.p6w_sigterm_fault_hooks",
            ],
            cwd=ROOT,
            env=environment,
            stdout=log_stream,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        worker = P5._Worker(process, hostname, queue, telemetry, log)
        try:
            P5._wait_until_ready(worker)
            yield worker
        except Exception as exc:
            raise AssertionError(
                f"{exc}\nCelery worker log:\n{P5._worker_log(worker)}"
            ) from exc
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=15)


def _release_path(worker: object) -> Path:
    token = worker.log.stem.removeprefix("worker-")
    return worker.log.parent / f"release-{token}.sentinel"


@pytest.fixture(scope="session", autouse=True)
def _runtime_guard() -> Iterator[None]:
    if os.environ.get("P6W_PROCESS_FAULTS") != "1":
        raise RuntimeError("P6-W destructive worker faults require explicit CI enablement")
    if os.environ.get("P6W_DISPOSABLE_DATABASE") != _DATABASE:
        raise RuntimeError("P6-W disposable database acknowledgement is absent")
    if os.environ.get("P5W2_PROCESS_FAULTS") != "1":
        raise RuntimeError("P6-W requires the inherited P5-W2 fault guard")
    if os.environ.get("P5W2_DISPOSABLE_DATABASE") != _DATABASE:
        raise RuntimeError("P6-W inherited P5-W2 database guard is absent")

    P5._safe_database_topology()
    client = _safe_p6w_broker()
    client.flushdb()
    client.close()
    yield
    client = _safe_p6w_broker()
    client.flushdb()
    client.close()


def test_main_process_sigterm_is_warm_and_preserves_one_authoritative_effect(
    tmp_path: Path,
) -> None:
    surface = P5.SURFACES[_LIFECYCLE_INDEX]
    seed = P5._seed(surface)
    with _running_sigterm_worker(tmp_path, seed.event_id) as worker:
        task = P5._send(surface, worker.queue)
        P5._wait_for(
            lambda: P5._fault_record(worker, "sigterm_hold_entered"),
            description="durable task to enter P6-W SIGTERM hold",
        )
        P5._assert_claimed_not_committed(seed)
        assert len(P5._task_processes(worker, task.id)) == 1

        # On POSIX, terminate() sends SIGTERM to this main Celery process only.
        # Warm shutdown must keep the active child alive until the task is safe.
        worker.process.terminate()
        P5._wait_for(
            lambda: "Warm shutdown" in P5._worker_log(worker),
            description="Celery Warm shutdown observation",
            timeout=10,
        )
        time.sleep(1.0)
        assert worker.process.poll() is None

        _release_path(worker).write_text("release\n", encoding="utf-8")
        assert worker.process.wait(timeout=20) == 0

    P5._wait_for(
        lambda: P5._state(seed)[0] == "dead_lettered",
        description="SIGTERM task terminal PostgreSQL state",
    )
    P5._assert_single_terminal_effect(seed, fence=1)
    P5._wait_for(
        lambda: P5._broker_drained(_safe_p6w_broker(), worker.queue),
        description="SIGTERM task broker acknowledgement",
        timeout=20,
    )

    # A separately started replacement worker must observe no second business effect.
    with P5._running_worker(tmp_path) as replacement:
        result = P5._result(P5._send(surface, replacement.queue))
        assert result["claimed"] == 0
        P5._assert_single_terminal_effect(seed, fence=1)

    print("P6W_MAIN_PROCESS_SIGTERM=PASS")
    print("P6W_WARM_SHUTDOWN_OBSERVED=PASS")


def test_before_commit_late_ack_redelivery_is_reclaimed_once(tmp_path: Path) -> None:
    surface = P5.SURFACES[_LIFECYCLE_INDEX]
    P5.test_real_worker_death_before_commit_is_reclaimed_by_replacement(
        surface,
        tmp_path,
    )
    print("P6W_BEFORE_COMMIT_REDELIVERY=PASS")


def test_after_commit_before_ack_redelivery_does_not_repeat_effect(
    tmp_path: Path,
) -> None:
    surface = P5.SURFACES[_LIFECYCLE_INDEX]
    P5.test_real_worker_death_after_commit_redelivers_without_repeating_effect(
        surface,
        tmp_path,
    )
    print("P6W_AFTER_COMMIT_PRE_ACK_REDELIVERY=PASS")
    print("P6W_SINGLE_AUTHORITATIVE_EFFECT=PASS")
