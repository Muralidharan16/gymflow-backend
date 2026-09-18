from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def test_p10s_is_continuous_long_running_and_frozen_budget_bound():
    w=(ROOT/".github/workflows/p10s-long-soak.yml").read_text()
    s=(ROOT/"scripts/ci/run_p10s_soak.sh").read_text()
    v=(ROOT/"scripts/ci/p10s_verify_soak.py").read_text()
    for x in ("P10-S Long Soak","P10S_DURATION_SECONDS: '300'","P10_SOAK=PASS","scripts/ci/verify_p10_performance_budgets.py"): assert x in w
    for x in ("p10s-api","p10s_worker_activity.py","p10s_resource_sampler.py","p10s_http_soak.py"): assert x in s
    for x in ("max_overall_p95_ms","max_overall_p99_ms","max_write_p95_ms","max_write_p99_ms","max_rss_bytes"): assert x in v
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
