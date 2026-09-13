from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs/architecture/P5E_PROVIDER_ACK_AMBIGUITY.md"
RUNTIME = ROOT / "tests/test_p5e_provider_ack_ambiguity_runtime.py"
RUNNER = ROOT / "scripts/ci/p5e_provider_ack_worker.py"
WORKFLOW = ROOT / ".github/workflows/p5e-provider-ack-ambiguity-pg16.yml"
MIGRATION = ROOT / "alembic/versions/zi07d8e9f0a43_p5e_provider_capability_fences.py"
SEARCH_WORKER = ROOT / "app/tasks/branch_outbox_poller.py"
NOTIFICATION_WORKER = ROOT / "app/services/notification_delivery_service.py"
P5_WORKFLOWS = (
    ROOT / ".github/workflows/p5-governance-fault-matrix.yml",
    ROOT / ".github/workflows/p5w-worker-fencing-pg16.yml",
    ROOT / ".github/workflows/p5w2-worker-crash-redelivery-pg16.yml",
)


def test_certified_p5w2_predecessor_is_recorded_exactly() -> None:
    source = DOC.read_text(encoding="utf-8")
    for phrase in (
        "67902b07163d7d45b0e7bda6b4cc5f46f672e8dc",
        "4ac45a1341dc00ff5179cd399091a9db37d45af8",
        "34735124480",
        "103665059403",
        "34735124481",
        "103665059641",
        "34735124496",
        "103665059480",
        "all 9 real worker death/redelivery/duplicate cases",
        "91 inherited P5-W1 and worker-boundary tests",
    ):
        assert phrase in source


def test_scope_covers_both_admitted_boundaries_and_both_search_operations() -> None:
    source = DOC.read_text(encoding="utf-8").lower()
    for phrase in (
        "provider_success_db_ack_failure",
        "search index",
        "search delete",
        "notification acceptance",
        "sqlstate `08006`",
        "same logical identity",
        "exactly one downstream effect",
        "same-worker aba",
        "lease fence",
    ):
        assert phrase in source


def test_runtime_is_fail_closed_to_one_disposable_database() -> None:
    source = RUNTIME.read_text(encoding="utf-8")
    for phrase in (
        'P5E_PROCESS_FAULTS") != "1"',
        'P5E_DISPOSABLE_DATABASE") != _DATABASE',
        '_DATABASE = "gymflow_p5e_test"',
        'runtime_topology[0] not in {"127.0.0.1", "localhost"}',
        "runtime_topology != admin_topology",
        "TEST_DATABASE_URL",
        "TEST_ADMIN_DATABASE_URL",
    ):
        assert phrase in source


def test_real_postgresql_acknowledgement_transactions_are_forced_to_fail() -> None:
    source = RUNTIME.read_text(encoding="utf-8")
    for phrase in (
        "CREATE TRIGGER p5e_reject_search_ack",
        "BEFORE UPDATE OF search_provider_ack_version",
        "CREATE TRIGGER p5e_reject_notification_ack",
        "BEFORE UPDATE OF status",
        "OLD.status='processing'",
        "NEW.status='provider_accepted'",
        "USING ERRCODE='08006'",
        "ALTER TABLE public.org_branch_state",
        "ALTER TABLE public.notification_commands",
    ):
        assert phrase in source
    assert "monkeypatch" not in source
    assert "_acknowledge_search_effect =" not in source
    assert "_ack_delivery =" not in source


def test_fault_harness_splits_schema_and_table_ddl_authority() -> None:
    source = RUNTIME.read_text(encoding="utf-8")
    install = source[
        source.index("def _install_fault_triggers()") :
        source.index("def _drop_fault_triggers()")
    ]
    drop = source[
        source.index("def _drop_fault_triggers()") :
        source.index("def _set_fault_trigger(")
    ]
    toggle = source[
        source.index("def _set_fault_trigger(") :
        source.index("def _run_worker(")
    ]
    grant_usage = install.index(
        "GRANT USAGE ON SCHEMA app_secure TO migration_owner"
    )
    grant_execute = install.index(
        "GRANT EXECUTE ON FUNCTION"
    )
    first_reset = install.index('cursor.execute("RESET ROLE")')
    create_trigger = install.index("CREATE TRIGGER p5e_reject_search_ack")
    revoke_execute = install.index(
        "REVOKE EXECUTE ON FUNCTION", create_trigger
    )
    revoke_usage = install.index(
        "REVOKE USAGE ON SCHEMA app_secure FROM migration_owner"
    )
    assert install.index("_as_security_owner(cursor)") < grant_usage
    assert grant_usage < grant_execute < first_reset < create_trigger
    assert create_trigger < revoke_execute < revoke_usage
    assert install.count('cursor.execute("RESET ROLE")') == 2
    assert "REVOKE ALL ON FUNCTION" in install
    assert "FROM PUBLIC" in install
    assert drop.index("DROP TRIGGER IF EXISTS p5e_reject_search_ack") < drop.index(
        "_as_security_owner(cursor)"
    ) < drop.index("DROP FUNCTION IF EXISTS app_secure.p5e_reject_search_ack()")
    assert "_as_security_owner(cursor)" not in toggle
    assert "pg_catalog.aclexplode(" in source
    assert "acl_data.grantee = 0" in source
    assert "'PUBLIC',%s,'EXECUTE'" not in source


def test_delete_seed_separates_lifecycle_fixture_from_security_owner_evidence() -> None:
    source = RUNTIME.read_text(encoding="utf-8")
    infrastructure = source[
        source.index("def _set_branch_publicity_with_ci_infrastructure(") :
        source.index("def _as_security_owner(")
    ]
    seed = source[
        source.index("def _seed_search(") :
        source.index("def _seed_notification(")
    ]
    assert infrastructure.index("_safe_database_topology()") < infrastructure.index(
        "subprocess.run("
    )
    for phrase in (
        '"sudo"',
        '"-u"',
        '"postgres"',
        '"psql"',
        '"ON_ERROR_STOP=1"',
        '"-d"',
        "_DATABASE",
        '"UPDATE 1"',
        '"-A"',
        '"-t"',
        '"-F"',
        "search_visibility_version::text",
        "search_provider_ack_version::text",
        "search_last_synced_at::text",
        "return verified.stdout.strip()",
    ):
        assert phrase in infrastructure
    assert "app_security_owner" not in infrastructure
    provider_seed = seed.index("SET search_provider_ack_version=1")
    infrastructure_change = seed.index(
        "_set_branch_publicity_with_ci_infrastructure("
    )
    publicity_argument = seed.index("is_public=False", infrastructure_change)
    state_assertion = seed.index('assert fixture_state == "false|2|1|NULL"')
    outbox_insert = seed.index("INSERT INTO public.branch_outbox_events")
    assert (
        provider_seed
        < infrastructure_change
        < publicity_argument
        < state_assertion
        < outbox_insert
    )
    security_seed = seed[provider_seed:infrastructure_change]
    assert "is_public=false" not in security_seed
    assert "SET search_visibility_version=2" not in security_seed
    assert "app.current_role','owner" not in seed


def test_replacement_processes_reuse_the_persisted_provider_store() -> None:
    source = RUNTIME.read_text(encoding="utf-8")
    for phrase in (
        "subprocess.run(",
        '"-m",',
        '"scripts.ci.p5e_provider_ack_worker",',
        '"--store",',
        '"WORKER_DATABASE_URL": os.environ["TEST_DATABASE_URL"]',
        '"ENVIRONMENT": "production"',
        "_run_worker(store)",
        'first["retry"] == 1',
        'first["lease_lost"] == 1',
        'second["delivered"] == 1',
        'second["provider_accepted"] == 1',
    ):
        assert phrase in source
    assert source.count("_run_worker(store)") >= 4


def test_notification_member_fixture_uses_canonical_auth_app_runtime_identity() -> None:
    runtime = RUNTIME.read_text(encoding="utf-8")
    seed = runtime[
        runtime.index("def _seed_notification()") :
        runtime.index("def _install_fault_triggers()")
    ]
    auth_connection = seed.index(
        'with _connect(_AUTH_LOGIN, "AUTH_RUNTIME_PASSWORD") as connection:'
    )
    member_insert = seed.index("INSERT INTO public.members(")
    admin_connection = seed.index(
        'with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as connection:',
        member_insert,
    )
    security_owner = seed.index("_as_security_owner(cursor)", admin_connection)
    assert auth_connection < member_insert < admin_connection < security_owner
    assert seed.count(
        'with _connect(_AUTH_LOGIN, "AUTH_RUNTIME_PASSWORD") as connection:'
    ) == 1
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert (
        "GRANT app_runtime TO auth_p5e_runtime "
        "WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;"
    ) in workflow


def test_runtime_step_uses_only_non_routable_broker_placeholders() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["provider-ack-ambiguity"]
    assert "services" not in job
    runtime_step = next(
        step
        for step in job["steps"]
        if step.get("name")
        == "Prove provider success and real acknowledgement transaction failure"
    )
    expected = {
        "REDIS_URL": "redis://p5e-cache.invalid:6379/0",
        "CELERY_BROKER_URL": "redis://p5e-broker.invalid:6379/1",
        "CELERY_RESULT_BACKEND": "redis://p5e-result.invalid:6379/2",
    }
    assert runtime_step.get("env") == expected
    for step in job["steps"]:
        if step is runtime_step:
            continue
        step_env = step.get("env") or {}
        assert not set(expected) & set(step_env)
    runtime_source = RUNTIME.read_text(encoding="utf-8")
    assert "environment = os.environ.copy()" in runtime_source
    assert "env=environment" in runtime_source
    for value in expected.values():
        assert ".invalid:" in value
        assert "localhost" not in value
        assert "127.0.0.1" not in value


def test_durable_provider_double_enforces_search_version_and_notification_key() -> None:
    source = RUNNER.read_text(encoding="utf-8")
    for phrase in (
        "sqlite3.connect",
        "PRAGMA journal_mode=WAL",
        "CREATE TABLE IF NOT EXISTS search_effects",
        "logical_key TEXT PRIMARY KEY",
        "provider_version INTEGER NOT NULL",
        "effect_count INTEGER NOT NULL",
        "mutation_calls INTEGER NOT NULL",
        "CREATE TABLE IF NOT EXISTS notification_effects",
        "idempotency_key TEXT PRIMARY KEY",
        "reference_id TEXT NOT NULL UNIQUE",
        "send_calls INTEGER NOT NULL",
        'request.headers.get("Idempotency-Key"',
    ):
        assert phrase in source
    assert "dict[str, Search" not in source
    assert "dict[str, Notification" not in source


def test_runner_exercises_real_production_adapters_and_worker_dispatch() -> None:
    source = RUNNER.read_text(encoding="utf-8")
    for phrase in (
        "httpx.MockTransport",
        "OpenSearchProvider(",
        "ResendEmailProvider(",
        'mode="opensearch"',
        'mode="resend"',
        "branch_outbox_poller._poll_outbox()",
        "P5E_WORKER_RESULT",
    ):
        assert phrase in source
    assert "refund" not in source.lower()


def test_postconditions_count_one_effect_and_keep_acceptance_nonterminal() -> None:
    source = RUNTIME.read_text(encoding="utf-8")
    for phrase in (
        'expected_deleted = 0 if operation == "index" else 1',
        "provider_after_failure[2:] == (expected_deleted, 1, 1)",
        "provider_final[2:] == (expected_deleted, 1, 2)",
        "provider_after_failure[2:] == (1, 1)",
        "provider_final[2:] == (1, 2)",
        '["ambiguous_outcome", "provider_accepted_nonterminal"]',
        "c.completed_at IS NULL",
        '"provider_accepted",',
    ):
        assert phrase in source
    assert "provider_after_failure[2:] == (1, 1, 1)" not in source
    assert "provider_final[2:] == (1, 1, 2)" not in source


def test_workflow_uses_pg16_reduced_worker_and_same_head_markers() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    parsed = yaml.safe_load(source)
    assert isinstance(parsed, dict)
    for phrase in (
        "scripts/ci/install_pg16_test_stack.sh",
        "scripts/ci/bootstrap_cluster_roles.sh",
        "worker_test_runtime",
        "GRANT app_runtime TO auth_p5e_runtime",
        "GRANT worker_runtime TO worker_test_runtime",
        "NOBYPASSRLS",
        "python -s -m alembic -c alembic.ini upgrade head",
        "tests/test_p5e_provider_ack_ambiguity_runtime.py",
        'test "$(git rev-parse HEAD)" = "${GITHUB_SHA}"',
        "P5E_SEARCH_INDEX_ACK_FAILURE=PASS",
        "P5E_SEARCH_DELETE_ACK_FAILURE=PASS",
        "P5E_NOTIFICATION_ACK_FAILURE=PASS",
        "P5E_PROVIDER_CAPABILITY_FENCE=PASS",
        "P5E_SINGLE_PROVIDER_EFFECT=PASS",
        "P5E_PROVIDER_REFUND_EXECUTION=DEFERRED_FAIL_CLOSED",
        'expected_head="${CERTIFICATION_HEAD:-zi07d8e9f0a43}"',
        "downgrade zh07d8e9f0a42",
        "zi07d8e9f0a43_p5e_provider_capability_fences.py",
    ):
        assert phrase in source
    assert "continue-on-error" not in source
    assert "|| true" not in source


def test_all_predecessor_p5_jobs_run_on_the_p5_branch() -> None:
    branch = "hardening/p5-concurrency-crash-fault-tolerance"
    assert branch in WORKFLOW.read_text(encoding="utf-8")
    for workflow in P5_WORKFLOWS:
        source = workflow.read_text(encoding="utf-8")
        assert "push:" in source
        assert branch in source


def test_migration_wraps_every_provider_capability_with_exact_live_fence() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'revision = "zi07d8e9f0a43"' in source
    assert 'down_revision = "zh07d8e9f0a42"' in source
    for phrase in (
        "CREATE FUNCTION app_secure.require_p5_provider_fence(",
        "outbox_data.status='processing'",
        "outbox_data.leased_by=p_worker_id",
        "outbox_data.lease_fence=p_lease_fence",
        "outbox_data.leased_until>pg_catalog.clock_timestamp()",
        "FOR UPDATE",
        "USING ERRCODE='42501'",
        "claim_branch_search_projection(uuid,uuid,bigint)",
        "acknowledge_branch_search_effect(uuid,uuid,bigint,bigint",
        "record_branch_search_failure(uuid,uuid,bigint,bigint",
        "repair_branch_search_provider_drift(uuid,uuid,bigint,bigint",
        "materialize_branch_member_notifications(uuid,uuid,bigint)",
        "claim_notification_delivery_v2(uuid,uuid,bigint)",
        "acknowledge_notification_provider_acceptance(uuid,uuid,bigint",
        "record_notification_delivery_failure(uuid,uuid,bigint",
        "claim_notification_reconciliation(uuid,uuid,bigint)",
        "complete_notification_reconciliation(uuid,uuid,bigint",
        "record_notification_reconciliation_failure(uuid,uuid,bigint",
    ):
        assert phrase in source
    assert source.count("PERFORM app_secure.require_p5_provider_fence(") == 11


def test_migration_removes_unfenced_worker_authority_and_is_reversible() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    for phrase in (
        "REVOKE EXECUTE ON FUNCTION {old_signature} FROM {_WORKER}",
        "REVOKE ALL ON FUNCTION {new_signature} FROM PUBLIC",
        "GRANT EXECUTE ON FUNCTION {new_signature} TO {_WORKER}",
        "REVOKE ALL ON FUNCTION {_HELPER} FROM {_WORKER}",
        "DROP FUNCTION {new_signature}",
        "GRANT EXECUTE ON FUNCTION {old_signature} TO {_WORKER}",
        "DROP FUNCTION {_HELPER}",
    ):
        assert phrase in source


def test_migration_treats_public_as_acl_pseudorole_not_database_role() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    for phrase in (
        "pg_catalog.aclexplode(",
        "pg_catalog.acldefault('f', proc_data.proowner)",
        "acl_data.grantee = 0",
        "_public_execute(bind, old_signature)",
        "_public_execute(bind, _HELPER)",
        "_public_execute(bind, new_signature)",
    ):
        assert phrase in source
    assert '_has_execute(bind, "PUBLIC"' not in source
    assert "_has_execute(bind, 'PUBLIC'" not in source


def test_migration_resolves_private_schema_only_after_security_role_switch() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    upgrade = source[source.index("def upgrade()") : source.index("def downgrade()")]
    downgrade = source[source.index("def downgrade()") :]
    for body, verification in (
        (upgrade, "_require_predecessor(bind)"),
        (downgrade, "_verify_upgrade(bind)"),
    ):
        assert body.index("_require_identity(bind)") < body.index(
            'op.execute(f"SET LOCAL ROLE {_SECURITY_OWNER}")'
        )
        assert body.index('op.execute(f"SET LOCAL ROLE {_SECURITY_OWNER}")') < body.index(
            verification
        )
        assert body.index(verification) < body.rindex('op.execute("RESET ROLE")')


def test_application_passes_claim_generation_to_every_provider_capability() -> None:
    search = SEARCH_WORKER.read_text(encoding="utf-8")
    notification = NOTIFICATION_WORKER.read_text(encoding="utf-8")
    assert search.count("CAST(:lease_fence AS bigint)") >= 4
    assert search.count('"lease_fence": event["lease_fence"]') >= 4
    assert notification.count("CAST(:lease_fence AS bigint)") >= 7
    assert notification.count('"lease_fence": event["lease_fence"]') >= 7


def test_runtime_proves_unfenced_acl_revocation_and_same_worker_aba_rejection() -> None:
    source = RUNTIME.read_text(encoding="utf-8")
    for phrase in (
        "test_provider_capabilities_reject_same_worker_aba_fence",
        "pg_catalog.has_function_privilege",
        "worker retained unfenced provider capability",
        "worker lacks fenced provider capability",
        "stale provider capability unexpectedly succeeded",
        "InsufficientPrivilege",
    ):
        assert phrase in source


def test_slice_does_not_claim_later_faults_or_refund_execution() -> None:
    source = DOC.read_text(encoding="utf-8").lower()
    for phrase in (
        "refund-provider execution remains deferred and fail-closed",
        "real dependency stop",
        "network interruption",
        "redis loss",
        "database restart belong to p5-d",
        "not a pr, merge, tag, release, deployment, p5-d,",
        "p5-r, p5-c, or p5-f authorization",
    ):
        assert phrase in source
