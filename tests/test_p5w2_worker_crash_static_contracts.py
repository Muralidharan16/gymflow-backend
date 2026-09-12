from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "scripts" / "ci" / "p5w2_celery_fault_hooks.py"
RUNTIME = ROOT / "tests" / "test_p5w2_worker_crash_redelivery_runtime.py"
WORKFLOW = ROOT / ".github" / "workflows" / "p5w2-worker-crash-redelivery-pg16.yml"
SLICE = ROOT / "docs" / "architecture" / "P5W2_WORKER_CRASH_REDELIVERY.md"
CELERY = ROOT / "app" / "core" / "celery_app.py"
PROJECTION = ROOT / "app" / "tasks" / "branch_hours_projection.py"


def test_fault_hooks_are_explicitly_test_only_and_kill_prefork_children() -> None:
    source = HOOKS.read_text(encoding="utf-8")

    for phrase in (
        "Test-only process fault hooks",
        "P5W2_FAULT_MODE",
        "P5W2_TARGET_EVENT_ID",
        "P5W2_FAULT_SENTINEL",
        "before_db_commit_kill",
        "after_db_commit_before_task_ack_kill",
        "os.kill(os.getpid(), signal.SIGKILL)",
        "worker_process_init",
        "task_prerun",
        "outbox_poller._process_claimed_event",
        "branch_outbox_poller._process_event",
    ):
        assert phrase in source

    production_importers = []
    for path in (ROOT / "app").rglob("*.py"):
        if "p5w2_celery_fault_hooks" in path.read_text(encoding="utf-8"):
            production_importers.append(path)
    assert production_importers == []


def test_runtime_uses_real_celery_redis_processes_and_both_outboxes() -> None:
    source = RUNTIME.read_text(encoding="utf-8")

    for phrase in (
        "subprocess.Popen",
        "start_new_session=True",
        "--pool=prefork",
        "--concurrency=2",
        "--prefetch-multiplier=1",
        "scripts.ci.p5w2_celery_fault_hooks",
        "celery_app.send_task",
        "task_id=task_id",
        "redis.Redis.from_url",
        'client.hlen("unacked")',
        'client.zcard("unacked_index")',
        "public.transactional_outbox",
        "public.branch_outbox_events",
        "worker_test_runtime",
        "app_test_runtime",
        '"DOERS_PROCESS_PROFILE": "worker"',
        '"CELERY_WORKER_PROFILE": "worker"',
        '"NOTIFICATION_EMAIL_PROVIDER_MODE": "disabled"',
        '"SEARCH_PROVIDER_MODE": "disabled"',
        "test_real_worker_death_before_commit_is_reclaimed_by_replacement",
        "test_real_worker_death_after_commit_redelivers_without_repeating_effect",
        "test_sequential_duplicate_celery_delivery_converges_once",
        "test_concurrent_duplicate_celery_delivery_converges_once",
        "assert _projection(seed) == (1, 1)",
    ):
        assert phrase in source
    assert "unittest.mock" not in source
    assert "monkeypatch" not in source
    worker_environment = source.split("def _running_worker", 1)[1].split(
        "def _send", 1
    )[0]
    assert 'environment[forbidden_name] = ""' in worker_environment
    assert '"DATABASE_URL"' in worker_environment
    assert '"TEST_ADMIN_DATABASE_URL"' in worker_environment


def test_runtime_seed_uses_canonical_auth_branch_state_bootstrap() -> None:
    source = RUNTIME.read_text(encoding="utf-8")
    helper = source.split(
        "def _insert_canonical_initial_branch_state", 1
    )[1].split("def _seed", 1)[0]
    seed = source.split("def _seed", 1)[1].split("def _telemetry", 1)[0]

    for phrase in (
        '_AUTH_LOGIN = "auth_p5w2_runtime"',
        'with _connect(_AUTH_LOGIN, "AUTH_RUNTIME_PASSWORD")',
        "INSERT INTO public.org_branch_state",
        "pg_catalog.set_config('app.current_role','owner',true)",
        "pg_catalog.set_config('app.current_org_id',%s,true)",
        "pg_catalog.set_config('app.current_user_id',%s,true)",
        "pg_catalog.set_config('app.current_principal_type','owner',true)",
        "'active',true,true,true,'active',true",
        "RETURNING status_changed_at,updated_at",
    ):
        assert phrase in source

    assert "_ADMIN_LOGIN" not in helper
    assert seed.index("connection.commit()") < seed.index(
        "_insert_canonical_initial_branch_state("
    ) < seed.index('with _connect(_APP_LOGIN, "APP_RUNTIME_PASSWORD")')


def test_fixture_repair_does_not_weaken_active_branch_projection_guard() -> None:
    source = PROJECTION.read_text(encoding="utf-8")
    rebuild = source.split("async def rebuild_branch_hours_projection", 1)[1]

    for phrase in (
        ".join(\n            OrgBranchState,",
        "OrgBranchState.branch_id == OrgBranch.id",
        "OrgBranchState.org_id == OrgBranch.org_id",
        "OrgBranchState.deleted_at.is_(None)",
        "OrgBranchState.is_active.is_(True)",
        'raise LookupError(\n            f"Active branch',
    ):
        assert phrase in rebuild


def test_runtime_requires_an_explicit_disposable_topology() -> None:
    source = RUNTIME.read_text(encoding="utf-8")

    assert 'P5W2_PROCESS_FAULTS") != "1"' in source
    assert 'P5W2_DISPOSABLE_DATABASE") != _DATABASE' in source
    assert 'P5W2_DISPOSABLE_BROKER") != "redis-db-1"' in source
    assert 'runtime_topology[2] != _DATABASE' in source
    assert "client.flushdb()" in source


def test_p5w2_preserves_canonical_late_ack_and_worker_lost_requeue() -> None:
    source = CELERY.read_text(encoding="utf-8")

    assert "task_acks_late=True" in source
    assert "task_reject_on_worker_lost=True" in source
    assert "worker_prefetch_multiplier=1" in source


def test_workflow_is_real_pg16_redis_same_head_and_reproducible() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")

    for phrase in (
        "PostgreSQL 16",
        "redis:7-alpine",
        "requirements-test.lock",
        "pip==26.2.1",
        "diff -u requirements-test.lock /tmp/resolved.txt",
        "worker_test_runtime",
        "app_test_runtime",
        "auth_p5w2_runtime",
        "AUTH_RUNTIME_PASSWORD",
        "GRANT auth_runtime TO auth_p5w2_runtime WITH ADMIN FALSE, INHERIT TRUE, SET FALSE",
        "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS",
        "tests/test_p5w2_worker_crash_redelivery_runtime.py",
        "tests/test_p5w2_worker_crash_static_contracts.py",
        "tests/test_p5w_worker_fencing_runtime.py",
        "git rev-parse HEAD",
        "P5W2_SEPARATE_PROCESS_CRASH=PASS",
        "P5W2_REAL_REDIS_REDELIVERY=PASS",
        "P5W2_DUPLICATE_DELIVERY=PASS",
        "P5W_PROVIDER_REFUND_EXECUTION=DEFERRED_FAIL_CLOSED",
    ):
        assert phrase in source


def test_slice_is_bounded_and_does_not_claim_later_p5_work() -> None:
    source = SLICE.read_text(encoding="utf-8").lower()

    for phrase in (
        "5ae630dd4239b8ce9e5345f362b8ecd3d7bf5623",
        "7a0d9db704f9db7c621e5297d9dcb89c176a494d",
        "worker death before database commit",
        "worker death after database commit and before broker acknowledgement",
        "sequential and concurrent duplicate delivery",
        "real redis",
        "real prefork celery",
        "p5-w1",
        "does not complete p5",
        "provider-success/database-acknowledgement failure remains p5-e",
        "redis/network and database loss remain p5-d",
        "deadlock and finance/lifecycle races remain p5-r",
        "compensation crash/replay remains p5-c",
        "refund-provider execution remains deferred and fail-closed",
        "bf0a9119e70fb3abd7415ff1601a3a64c861e67d",
        "34702421005",
        "is not p5-w2 certified",
        "failed-candidate provenance",
    ):
        assert phrase in source
