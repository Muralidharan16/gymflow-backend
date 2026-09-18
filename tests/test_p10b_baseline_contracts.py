from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/p10b-baseline-calibration.yml"
RUNNER = ROOT / "scripts/ci/run_p10b_baseline_calibration.sh"
LOAD = ROOT / "scripts/ci/p10b_load_calibration.py"
SEED = ROOT / "scripts/ci/p10b_seed_baseline.sql"


def test_p10b_is_calibration_not_moving_performance_budget():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    runner = RUNNER.read_text(encoding="utf-8")
    load = LOAD.read_text(encoding="utf-8")

    assert "P10B_CALIBRATION=PASS" in runner
    assert "P10_BASELINE_BUDGETS=PASS" not in runner
    assert "budgets_frozen': False" in runner
    assert '"mode": "calibration"' in load
    assert "p50_ms" in load and "p95_ms" in load and "p99_ms" in load
    assert "throughput_rps" in load
    assert "P10B_DURATION_SECONDS: '60'" in workflow
    assert "P10B_CONCURRENCY: '24'" in workflow
    assert "P10B_CPU_LIMIT: '1.25'" in workflow
    assert "P10B_CPU_LIMIT must be in (0, 1.25] cores" in runner
    assert 'DOCKER_RESOURCE_ARGS+=(--cpus "${P10B_CPU_LIMIT}")' in runner


def test_p10b_uses_real_pg16_redis_and_production_api_container():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    runner = RUNNER.read_text(encoding="utf-8")

    assert "scripts/ci/prepare_p3e_pg16.sh" in workflow
    assert "redis:7-alpine" in workflow
    assert "docker build" in runner
    assert "--env ENVIRONMENT=production" in runner
    assert "--env DOERS_PROCESS_PROFILE=api" in runner
    assert "app_test_runtime:ci-app-test-runtime" in runner
    assert "auth_test_runtime:ci-auth-test-runtime" in runner
    assert "WORKER_DATABASE_URL" not in runner
    assert "MAINTENANCE_DATABASE_URL" not in runner
    assert "FINANCE_CONFIG_DATABASE_URL" not in runner
    assert "/_system/ready" in runner


def test_p10b_provider_execution_is_disabled_and_metrics_remain_required():
    runner = RUNNER.read_text(encoding="utf-8")

    for fragment in (
        "NOTIFICATION_EMAIL_PROVIDER_MODE=disabled",
        "SEARCH_PROVIDER_MODE=disabled",
        "PLATFORM_BILLING_PROVIDER_MODE=disabled",
        "PLATFORM_BILLING_CHECKOUT=false",
        "PLATFORM_BILLING_WEBHOOK_PROCESSING=false",
        "PLATFORM_BILLING_DUNNING_TRANSITIONS=false",
        "PLATFORM_BILLING_NOTIFICATIONS=false",
        "P8_METRICS_OTLP_ENDPOINT=",
        "scripts/ci/p8o_otlp_collector.py",
        "P10_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED",
    ):
        assert fragment in runner


def test_p10b_seed_is_synthetic_bounded_and_counter_synchronized():
    seed = SEED.read_text(encoding="utf-8")

    assert "Synthetic deterministic data only" in seed
    assert "FOR org_sequence IN 1..8" in seed
    assert "FOR member_sequence IN 1..500" in seed
    assert "source = 'p10b_synthetic'" in seed
    assert "current_value" in seed and "599" in seed
    assert "P10B_SYNTHETIC_BASELINE_SEED=PASS" in seed
    assert "production/customer" in seed


def test_p10b_load_is_authenticated_multi_tenant_read_write_traffic():
    load = LOAD.read_text(encoding="utf-8")

    assert "principal_type" in load and '"owner"' in load
    assert "Authorization" in load
    assert "X-Tenant-ID" in load
    assert "/organizations/{org_id}/members" in load
    assert "client.get" in load
    assert "client.post" in load
    assert "read_write_mix" in load
    assert "tenant_auth" in load


def test_p10b_preserves_p9_and_member_number_integrity():
    runner = RUNNER.read_text(encoding="utf-8")

    assert runner.count("scripts/ci/p9m_stable_snapshot.sql") == 2
    assert "cmp \"$EVIDENCE_DIR/p9-protected-before.txt\" \"$EVIDENCE_DIR/p9-protected-after.txt\"" in runner
    assert "P10B_P9_PROTECTED_BASELINE_UNCHANGED=PASS" in runner
    assert "max_number = counter_value" in runner
    assert "member_count = distinct_numbers" in runner
    assert "P10B_REAL_WRITE_COUNTER_INTEGRITY=PASS" in runner


def test_p10b_records_machine_readable_http_resource_and_image_evidence():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    runner = RUNNER.read_text(encoding="utf-8")

    for fragment in (
        "http-calibration.json",
        "resource-calibration.json",
        "image-inspect.json",
        "image-posture.json",
        "docker-stats.jsonl",
        "postgres-before.txt",
        "postgres-after.txt",
        "redis-stats.txt",
        "decision.json",
        "decision.sha256",
    ):
        assert fragment in runner
    assert "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02" in workflow
    assert "p10b-calibration-${{ github.event.pull_request.head.sha || github.sha }}" in workflow
