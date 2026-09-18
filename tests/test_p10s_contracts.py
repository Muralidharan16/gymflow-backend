from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
BINDER=ROOT/"scripts/ci/p10s_bind_same_head_soak.py"
def test_p10s_is_continuous_long_running_and_frozen_budget_bound():
    w=(ROOT/".github/workflows/p10s-long-soak.yml").read_text()
    s=(ROOT/"scripts/ci/run_p10s_soak.sh").read_text()
    v=(ROOT/"scripts/ci/p10s_verify_soak.py").read_text()
    for x in ("P10-S Long Soak","P10S_DURATION_SECONDS: '300'","P10S_CPU_LIMIT: '1.25'","P10_SOAK=PASS","scripts/ci/verify_p10_performance_budgets.py"): assert x in w
    for x in ("p10s-api","p10s_worker_activity.py","p10s_resource_sampler.py","p10s_http_soak.py"): assert x in s
    for x in ("max_overall_p95_ms","max_overall_p99_ms","max_write_p95_ms","max_write_p99_ms","max_rss_bytes"): assert x in v
def test_p10s_enforces_cpu_budget_with_a_stricter_container_quota():
    s=(ROOT/"scripts/ci/run_p10s_soak.sh").read_text()
    assert 'P10S_CPU_LIMIT:?' in s
    assert 'limit <= 1.25' in s
    assert '--cpus "$P10S_CPU_LIMIT"' in s
    assert "cpu-limit-cores.txt" in s
    assert "P10S_FROZEN_CPU_ENVELOPE_ENFORCED=PASS" in s


def test_p10s_worker_is_one_long_lived_process_and_provider_free():
    x=(ROOT/"scripts/ci/p10s_worker_activity.py").read_text()
    assert "continuous_worker" in x and "external_provider_effects" in x
    assert "start_new_session=True" in x



def test_p10s_worker_prioritizes_only_its_synthetic_cycle_over_old_fixture_backlog():
    worker=(ROOT/"scripts/ci/p10s_worker_activity.py").read_text()
    durable=(ROOT/"scripts/ci/p10b_durable_queue_calibration.py").read_text()
    assert 'process_after="2000-01-01T00:00:00+00:00"' in worker
    assert "synthetic_process_after_priority" in worker
    assert "process_after: str | None = None" in durable
    assert "COALESCE(%s::timestamptz, pg_catalog.clock_timestamp())" in durable


def test_p10s_worker_smooths_the_same_two_items_per_second_without_reducing_rate():
    worker=(ROOT/"scripts/ci/p10s_worker_activity.py").read_text()
    assert "BATCH_SIZE=2" in worker
    assert 'default=1' in worker
    assert '"target_worker_items_per_second":BATCH_SIZE/a.interval_seconds' in worker
    assert '"worker_batch_size":BATCH_SIZE' in worker
    assert 'ctl.send_task("app.tasks.branch_outbox_poller.run",queue=queue)' in worker



def test_p10s_uses_one_canonical_live_soak_per_sha_and_pr_binds_push():
    w=(ROOT/".github/workflows/p10s-long-soak.yml").read_text()
    b=BINDER.read_text()
    assert "if: github.event_name != 'pull_request'" in w
    assert "if: github.event_name == 'pull_request'" in w
    assert "actions: read" in w
    assert "p10s_bind_same_head_soak.py" in w
    assert "P10S_SAME_HEAD_SOAK_BOUND=PASS" in w
    assert 'SOURCE_EVENT = "push"' in b
    assert "head_sha" in b
    assert "candidate_sha" in b
    assert "duration_seconds" in b
    assert "windows" in b
    assert "retry" not in w.lower()



def test_p10s_enforces_frozen_growth_budgets_not_p10l_absolute_throughput_floor():
    verifier=(ROOT/"scripts/ci/p10s_verify_soak.py").read_text()
    budget=(ROOT/"docs/architecture/p10_performance_budgets.v1.json").read_text()
    for token in (
        "soak_stability",
        "max_rss_growth_bytes",
        "max_db_connection_growth",
        "max_db_connections",
        "max_worker_broker_depth",
        "max_throughput_degradation_ratio",
        "max_overall_p95_growth_ratio",
        "max_write_p95_growth_ratio",
    ):
        assert token in verifier
        assert token in budget
    assert 'representative["min_throughput_rps"]' not in verifier
    assert "sustained throughput progressively degraded" in verifier
    assert "RSS progressively grew" in verifier
    assert "database connections progressively grew" in verifier


def test_p10s_artifact_binding_strips_github_auth_before_blob_download():
    binder = BINDER.read_text(encoding="utf-8")
    transport = (ROOT / "scripts/ci/github_artifact_transport.py").read_text(encoding="utf-8")
    assert "download_github_artifact" in binder
    assert "_download(" not in binder
    assert "_NoRedirect" in transport
    assert "GitHub bearer token must never be forwarded" in transport


def test_p10s_push_is_the_canonical_final_certification_soak():
    w=(ROOT/".github/workflows/p10s-long-soak.yml").read_text()
    assert "Canonical five minute API worker PostgreSQL Redis soak" in w
    assert "Bind canonical same-head push five-minute soak" in w
    assert "P10S_CANDIDATE_SHA: ${{ github.event.pull_request.head.sha }}" in w
