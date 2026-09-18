from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/p10l-representative-load-concurrency.yml"
VERIFIER = ROOT / "scripts/ci/p10l_verify_representative_load.py"
BINDER = ROOT / "scripts/ci/p10l_bind_same_head_baseline.py"
BUDGETS = ROOT / "docs/architecture/p10_performance_budgets.v1.json"


def test_p10l_workflow_binds_frozen_budget_and_real_dependencies():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    for fragment in (
        "P10-L Representative Load and Concurrency Stress",
        "scripts/ci/verify_p10_performance_budgets.py",
        "scripts/ci/p10l_bind_same_head_baseline.py",
        "scripts/ci/p10l_verify_representative_load.py",
        "actions: read",
        "P10L_SAME_HEAD_BASELINE_BOUND=PASS",
        "p10l-evidence",
        "P10_REPRESENTATIVE_LOAD=PASS",
        "P10_CONCURRENCY_STRESS=PASS",
        "P10_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED",
        "Install terminal pytest",
        "pytest==9.1.1",
    ):
        assert fragment in workflow


def test_p10l_reuses_real_p5r_contention_with_current_head():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "uses: ./.github/workflows/p5r-race-deadlock-pg16.yml" in workflow
    assert "certification_head: zk07d8e9f0a45" in workflow
    assert "needs: [representative_load, concurrency_stress]" in workflow
    assert "if: always()" in workflow


def test_p10l_verifier_enforces_every_frozen_http_resource_budget():
    verifier = VERIFIER.read_text(encoding="utf-8")
    budgets = json.loads(BUDGETS.read_text(encoding="utf-8"))["budgets"]["representative_http"]
    for key in budgets:
        assert key in verifier
    assert "rejected_connections" in verifier
    assert "postgres_activity_count" in verifier
    assert "DEFERRED_FAIL_CLOSED" in verifier


def test_p10l_binds_canonical_same_head_load_instead_of_retrying_or_loosened_budget():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    binder = BINDER.read_text(encoding="utf-8")
    assert "run_p10b_baseline_calibration.sh" not in workflow
    assert "P10B_CPU_LIMIT:" not in workflow
    assert "p10b-baseline-calibration.yml" in binder
    assert "head_sha" in binder
    assert "candidate_sha" in binder
    assert "P10L_SAME_HEAD_BASELINE_BOUND=PASS" in binder
    assert "retry" not in workflow.lower()
    assert "min_throughput_rps" in VERIFIER.read_text(encoding="utf-8")


def test_p10l_artifact_binding_strips_github_auth_before_blob_download():
    binder = (ROOT / "scripts/ci/p10l_bind_same_head_baseline.py").read_text(encoding="utf-8")
    transport = (ROOT / "scripts/ci/github_artifact_transport.py").read_text(encoding="utf-8")
    assert "download_github_artifact" in binder
    assert "_download(" not in binder
    assert "class _NoRedirect" in transport
    assert '"Authorization": f"Bearer {token}"' in transport
    assert 'headers={"User-Agent": "doers-p10-certification"}' in transport


def test_p10l_authenticated_admission_is_single_atomic_redis_round_trip():
    middleware = (ROOT / "app/core/middleware.py").read_text(encoding="utf-8")
    assert '_LUA_TENANT_ADMISSION = """' in middleware
    assert "redis.call('TIME')" in middleware
    assert "redis.call('GET', tier_key)" in middleware
    assert "redis.call('ZREMRANGEBYSCORE', sem_key" in middleware
    assert "redis.call('ZADD', sem_key, now_s, token)" in middleware
    assert "redis.call('HMGET', rate_key" in middleware
    assert "redis.call('ZREM', sem_key, token)" in middleware
    assert "_LUA_RATE_LIMITER" not in middleware
    assert "_LUA_SEMAPHORE" not in middleware
    assert "async def _get_tier" not in middleware
    assert "_LUA_TENANT_ADMISSION,\n            3," in middleware
    assert "if decision is None:" in middleware
    assert "otherwise-authorized application traffic remains available" in middleware
    assert 'if decision in (2, "2"):' in middleware
    assert 'if decision not in (1, "1"):' in middleware


def test_p10l_hot_path_admission_layers_use_pure_asgi_without_semantic_bypass():
    middleware = (ROOT / "app/core/middleware.py").read_text(encoding="utf-8")
    assert "from starlette.types import ASGIApp, Receive, Scope, Send" in middleware
    assert "class RedisRateLimiterMiddleware:" in middleware
    assert "class AdaptiveWriteThrottler:" in middleware
    assert "class CorrelationIdMiddleware:" in middleware
    assert "class TenantMiddleware:" in middleware
    assert "class IdempotencyMiddleware:" in middleware
    assert "class RedisRateLimiterMiddleware(BaseHTTPMiddleware)" not in middleware
    assert "class AdaptiveWriteThrottler(BaseHTTPMiddleware)" not in middleware
    assert "class CorrelationIdMiddleware(BaseHTTPMiddleware)" not in middleware
    assert "class TenantMiddleware(BaseHTTPMiddleware)" not in middleware
    assert "class IdempotencyMiddleware(BaseHTTPMiddleware)" not in middleware
    assert 'dict(scope.get("headers") or ()).get(b"x-tenant-id")' in middleware
    assert "if decision is None:" in middleware
    assert 'method in ("POST", "PUT", "PATCH", "DELETE")' in middleware
    assert 'redis_client.get("backpressure:write_throttle_active")' in middleware
    assert 'request.headers.get("Authorization", "")' in middleware
    assert "request.state.principal_type = principal_type" in middleware
    assert 'request.headers.get("X-Idempotency-Key")' in middleware
    assert "send_with_correlation" in middleware
    assert "capture_status" in middleware
    assert "await self.app(scope, receive, send)" in middleware

def test_p10l_adaptive_write_throttler_preserves_redis_degraded_mode():
    middleware = (ROOT / "app/core/middleware.py").read_text(encoding="utf-8")
    start = middleware.index("class AdaptiveWriteThrottler:")
    end = middleware.index("# 6. Tenant Authentication Middleware", start)
    block = middleware[start:end]
    assert 'try:\n                is_throttled = await redis_client.get("backpressure:write_throttle_active")' in block
    assert "except Exception:" in block
    assert "is_throttled = None" in block

