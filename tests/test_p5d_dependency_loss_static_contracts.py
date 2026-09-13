from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs/architecture/P5D_DEPENDENCY_AND_DATABASE_LOSS.md"
RUNTIME = ROOT / "tests/test_p5d_dependency_loss_runtime.py"
WORKER = ROOT / "scripts/ci/p5d_fault_worker.py"
PROVIDER = ROOT / "scripts/ci/p5d_fake_opensearch.py"
CELERY_HOOK = ROOT / "scripts/ci/p5d_celery_fault_hooks.py"
WORKFLOW = ROOT / ".github/workflows/p5d-dependency-loss-pg16.yml"
POLLER = ROOT / "app/tasks/branch_outbox_poller.py"
SEARCH_PROVIDER = ROOT / "app/services/search_provider.py"


def test_certified_p5e_predecessor_is_recorded_exactly() -> None:
    source = DOC.read_text(encoding="utf-8")
    for phrase in (
        "9e1f576dce5d36844502f1f7450e87294a4f828d",
        "34757688362",
        "34757688264",
        "34757688301",
        "34757688342",
    ):
        assert phrase in source


def test_scope_covers_every_frozen_p5d_fault_boundary() -> None:
    source = DOC.read_text(encoding="utf-8").lower()
    for phrase in (
        "redis / broker loss",
        "before task delivery",
        "before broker acknowledgement",
        "provider network loss",
        "postgresql disconnect at claim",
        "postgresql disconnect during domain mutation",
        "postgresql disconnect during provider acknowledgement",
        "replacement worker",
        "pg_terminate_backend",
        "bounded retry",
        "dead-letter",
        "false terminal success",
    ):
        assert phrase in source


def test_runtime_requires_real_local_dependencies_and_explicit_destructive_enablement() -> None:
    source = RUNTIME.read_text(encoding="utf-8")
    for phrase in (
        'P5D_PROCESS_FAULTS") != "1"',
        'P5D_DISPOSABLE_DATABASE") != _DATABASE',
        "pg_terminate_backend",
        "pg_stat_activity",
        "subprocess.Popen",
        '"docker", "stop"',
        '"docker", "start"',
        "p5d_fake_opensearch",
        "127.0.0.1",
        "_run_replacement_worker",
    ):
        assert phrase in source
    assert "monkeypatch" not in source
    assert "MockTransport" not in source


def test_database_faults_name_claim_mutation_and_acknowledgement_boundaries() -> None:
    source = RUNTIME.read_text(encoding="utf-8")
    for phrase in (
        "p5d_claim_barrier",
        "p5d_mutation_barrier",
        "p5d_ack_barrier",
        "pending_to_processing",
        "transaction_b_started",
        "acknowledge_branch_search_effect",
        "test_database_disconnect_at_claim_rolls_back_and_recovers",
        "test_database_disconnect_during_domain_mutation_rolls_back_and_recovers",
        "test_database_disconnect_during_provider_ack_rebinds_without_duplicate_effect",
    ):
        assert phrase in source


def test_provider_network_fault_uses_production_adapter_and_persistent_effect_store() -> None:
    runtime = RUNTIME.read_text(encoding="utf-8")
    provider = PROVIDER.read_text(encoding="utf-8")
    adapter = SEARCH_PROVIDER.read_text(encoding="utf-8")
    for phrase in (
        "branch.search_index",
        "SEARCH_PROVIDER_MODE",
        "OPENSEARCH_URL",
        "provider_effect_count",
    ):
        assert phrase in runtime
    for phrase in (
        "ThreadingHTTPServer",
        "_version",
        "_source",
        "mutation_count",
        "state_path",
    ):
        assert phrase in provider
    assert "class OpenSearchProvider" in adapter
    assert "httpx.AsyncClient" in adapter


def test_fault_worker_runs_real_production_poller_in_fresh_process() -> None:
    source = WORKER.read_text(encoding="utf-8")
    for phrase in (
        "branch_outbox_poller._poll_outbox",
        "asyncio.run",
        "P5D_WORKER_RESULT",
        "P5D_PROCESS_FAULTS",
    ):
        assert phrase in source


def test_celery_hook_pauses_only_after_real_db_outcome_before_task_ack() -> None:
    source = CELERY_HOOK.read_text(encoding="utf-8")
    for phrase in (
        "worker_process_init",
        "branch_outbox_poller._process_event",
        "outcome = await original(event, worker_id)",
        "after_db_commit_before_task_ack",
        "P5D_RELEASE_PATH",
    ):
        assert phrase in source
    assert "SIGKILL" not in source


def test_production_retry_path_is_bounded_and_fenced() -> None:
    poller = POLLER.read_text(encoding="utf-8")
    for phrase in (
        "attempt_count < max_attempts",
        "lease_fence = outbox_data.lease_fence + 1",
        "delay_seconds = min(1800, 30 * (2 ** max(attempts - 1, 0)))",
        "status = 'dead_lettered'",
        "leased_until > pg_catalog.clock_timestamp()",
    ):
        assert phrase in poller


def test_search_transport_loss_is_classified_without_false_success() -> None:
    source = SEARCH_PROVIDER.read_text(encoding="utf-8")
    for phrase in (
        "httpx.TimeoutException",
        "httpx.TransportError",
        'outcome="ambiguous_outcome"',
        'error_code="mutation_transport_ambiguous"',
        'error_code="verification_transport_ambiguous"',
        'version_type": "external_gte" if operation == "delete" else "external"',
    ):
        assert phrase in source


def test_workflow_is_pg16_reduced_identity_real_dependency_and_same_head() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    parsed = yaml.safe_load(source)
    assert isinstance(parsed, dict)
    for phrase in (
        "ubuntu-24.04",
        "redis:7-alpine",
        "scripts/ci/install_pg16_test_stack.sh",
        "scripts/ci/bootstrap_cluster_roles.sh",
        "app_test_runtime",
        "worker_test_runtime",
        "NOBYPASSRLS",
        "tests/test_p5d_dependency_loss_runtime.py",
        "tests/test_p5d_dependency_loss_static_contracts.py",
        'test "$(git rev-parse HEAD)" = "${GITHUB_SHA}"',
        "P5D_REDIS_LOSS=PASS",
        "P5D_PROVIDER_NETWORK_LOSS=PASS",
        "P5D_DB_CLAIM_DISCONNECT=PASS",
        "P5D_DB_MUTATION_DISCONNECT=PASS",
        "P5D_DB_ACK_DISCONNECT=PASS",
        "P5D_PROVIDER_REFUND_EXECUTION=DEFERRED_FAIL_CLOSED",
    ):
        assert phrase in source
    assert "continue-on-error" not in source
    assert "|| true" not in source


def test_slice_does_not_broaden_privilege_or_later_phase_scope() -> None:
    executable_sources = "\n".join(
        path.read_text(encoding="utf-8").lower()
        for path in (RUNTIME, WORKER, PROVIDER, CELERY_HOOK, WORKFLOW)
    )
    for forbidden in (
        "grant all on",
        "alter role worker_test_runtime superuser",
        "refund provider call",
        "p5-r certified",
        "p5-c certified",
        "p5-f certified",
    ):
        assert forbidden not in executable_sources
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "NOBYPASSRLS" in workflow
    assert " BYPASSRLS" not in workflow.replace("NOBYPASSRLS", "")
