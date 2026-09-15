from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github/workflows/p7o-production-like-orchestration.yml"
FIXTURE_PATH = ROOT / "scripts/ci/p7o_runtime_app.py"
API_RUNTIME_PATH = ROOT / "app/core/api_runtime.py"
P7_BRANCH = "hardening/p7-api-runtime-graceful-deployment"
P7_GOVERNANCE = "a666250e8dd83a7de195ce06731f935406a90846"


def _workflow() -> dict:
    return yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_p7o_is_reusable_exact_head_production_like_gate() -> None:
    workflow = _workflow()
    source = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["on"]["push"]["branches"] == [P7_BRANCH]
    assert "workflow_call" in workflow["on"]
    assert workflow["on"]["workflow_call"]["inputs"]["certification_head"]["type"] == "string"
    assert set(workflow["jobs"]) == {"production-like-orchestration"}
    assert P7_GOVERNANCE in source
    assert 'test "$(git rev-parse HEAD)" = "${GITHUB_SHA}"' in source
    assert "CERTIFICATION_HEAD" in source


def test_p7o_uses_real_processes_and_reduced_production_identity() -> None:
    source = WORKFLOW_PATH.read_text(encoding="utf-8")
    for required in (
        "ENVIRONMENT: production",
        "DOERS_PROCESS_PROFILE: api",
        "uvicorn scripts.ci.p7o_runtime_app:app",
        "scripts/ci/install_pg16_test_stack.sh",
        "scripts/ci/bootstrap_cluster_roles.sh",
        "python -s -m alembic -c alembic.ini upgrade head",
        "redis:7-alpine",
        "REDIS_PRODUCTION_TOPOLOGY=ha_primary_replica",
        "REDIS_PERSISTENCE_MODE=aof_everysec_rdb",
        "REDIS_MAXMEMORY_POLICY=noeviction",
        "scripts/verify_redis_production_readiness.py",
        "WORKER_DATABASE_URL: ''",
        "MAINTENANCE_DATABASE_URL: ''",
        "FINANCE_CONFIG_DATABASE_URL: ''",
    ):
        assert required in source
    assert "TestClient" not in source
    assert "docker compose up" not in source


def test_p7o_proves_control_drain_and_inflight_sequence() -> None:
    source = WORKFLOW_PATH.read_text(encoding="utf-8")
    for required in (
        "/_system/live",
        "/_system/ready",
        "/_system/preStop",
        "X-Doers-PreStop-Token",
        "P7O_SLOW_STARTED_FILE",
        "/_p7/slow",
        "pod_draining",
        "kill -TERM",
    ):
        assert required in source
    assert source.count("/_system/preStop") >= 3
    assert "missing preStop token unexpectedly authorized" in source
    assert "wrong preStop token unexpectedly authorized" in source
    assert "readiness did not become 503 during drain" in source
    assert "liveness failed while draining" in source
    assert "new request was not rejected during drain" in source
    assert "slow admitted request did not complete" in source


def test_p7o_proves_post_shutdown_database_and_redis_cleanup() -> None:
    source = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "pg_stat_activity" in source
    assert "app_p7o_runtime" in source
    assert "CLIENT LIST TYPE normal" in source
    assert "Redis client count did not return to baseline" in source
    assert "API database connections remain after shutdown" in source


def test_p7o_fixture_is_ci_only_ordinary_traffic() -> None:
    fixture = FIXTURE_PATH.read_text(encoding="utf-8")
    runtime = API_RUNTIME_PATH.read_text(encoding="utf-8")
    assert 'SLOW_PATH = "/_p7/slow"' in fixture
    assert "EXEMPT_PATHS.add(SLOW_PATH)" in fixture
    assert "AsyncSessionLocal" in fixture
    assert "SELECT pg_backend_pid()" in fixture
    assert "SELECT 1" in fixture
    assert "/_p7/slow" not in runtime
    assert "SYSTEM_REQUEST_PATHS.add" not in fixture
    assert "TestClient" not in fixture


def test_p7o_emits_frozen_slice_markers_without_release_or_deploy() -> None:
    source = WORKFLOW_PATH.read_text(encoding="utf-8")
    for marker in (
        "P7_PRESTOP_AUTHORIZATION=PASS",
        "P7_LIVENESS_READINESS_SEPARATED=PASS",
        "P7_READINESS_FALSE_BEFORE_SHUTDOWN=PASS",
        "P7_NEW_REQUEST_DRAIN=PASS",
        "P7_INFLIGHT_COMPLETION=PASS",
        "P7_API_RESOURCE_SHUTDOWN=PASS",
        "P7_PRODUCTION_LIKE_ROLLOUT=PASS",
        "P7_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED",
    ):
        assert marker in source
    assert "kubectl apply" not in source
    assert "helm upgrade" not in source
    assert "gh release" not in source
