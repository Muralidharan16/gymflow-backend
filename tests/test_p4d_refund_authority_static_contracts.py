from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "zc07d8e9f0a3d_p4d_refund_authority_boundary.py"
FOUNDATION = ROOT / "app" / "finance_core" / "models" / "foundation.py"
PAYMENT_REPO = ROOT / "app" / "finance_core" / "repositories" / "payments.py"
POLLER = ROOT / "app" / "tasks" / "branch_outbox_poller.py"
FINANCE_GUARDS = ROOT / "app" / "finance_core" / "api" / "guards.py"
RUNTIME_TEST = ROOT / "tests" / "test_p4d_refund_authority_runtime.py"
WORKFLOW = ROOT / ".github" / "workflows" / "p4d-refund-authority-pg16.yml"
GENERAL_WORKFLOW = ROOT / ".github" / "workflows" / "p4c-general-regression.yml"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _tuple_assignment(source: str, name: str) -> tuple[str, ...]:
    tree = ast.parse(source)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            continue
        value = ast.literal_eval(node.value)
        assert isinstance(value, tuple)
        return value
    raise AssertionError(f"assignment {name} not found")


def test_p4d_migration_is_append_only_after_certified_p4c_head() -> None:
    source = _source(MIGRATION)
    assert 'revision = "zc07d8e9f0a3d"' in source
    assert 'down_revision = "zb07d8e9f0a3c"' in source


def test_p4d_does_not_activate_lifecycle_refund_delivery_or_provider_execution() -> None:
    migration = _source(MIGRATION).lower()
    poller = _source(POLLER)
    guards = _source(FINANCE_GUARDS)
    assert '"branch.refund_required"' in poller
    assert "_DEFERRED_EXTERNAL_EVENT_TYPES" in poller
    assert "No production handler is configured" in poller
    assert "requests." not in migration
    assert "httpx" not in migration
    assert "razorpay" not in migration
    assert "provider_refund_ref varchar(200) null" in migration
    assert "status='succeeded'" not in migration
    assert "FINANCE_PAYMENT_API_ENABLED = False" in guards
    assert "live_money_movement_enabled: bool = False" in guards


def test_p4d_refund_intent_and_execution_command_state_are_separated() -> None:
    source = _source(MIGRATION)
    model = _source(FOUNDATION)
    assert "ALTER TABLE finance.refunds ADD COLUMN currency_code CHAR(3)" in source
    assert "CREATE TABLE finance.refund_execution_commands" in source
    assert "class FinanceRefundExecutionCommand" in model
    assert "uq_finance_refund_execution_refund" in source
    assert "uq_finance_refund_execution_logical_key" in source
    assert "logical_obligation_key" in source
    assert "'finance-refund/' || v_refund.id::text" in source


def test_p4d_amount_and_currency_authority_derive_from_locked_finance_rows() -> None:
    source = _source(MIGRATION)
    model = _source(FOUNDATION)
    repo = _source(PAYMENT_REPO)
    assert "FROM finance.payments p" in source
    assert "FOR UPDATE" in source
    assert "v_refund.amount" in source
    assert "v_payment.currency_code" in source
    assert "currency_code=payment.currency_code" in repo
    assert "ADD CONSTRAINT uq_finance_payments_id_currency" in source
    assert "UNIQUE (id, currency_code)" in source
    assert "ADD CONSTRAINT fk_finance_refunds_payment_currency" in source
    assert "FOREIGN KEY (payment_id, currency_code)" in source
    assert "REFERENCES finance.payments(id, currency_code)" in source
    assert "uq_finance_refunds_payment_reason_not_null" in source
    assert "uq_finance_refunds_payment_reason_not_null" in model
    assert 'postgresql_where=text("reason_code IS NOT NULL")' in model
    assert "p_idempotency_key" in source
    assert "p_idempotency_key" not in source.split("INSERT INTO finance.refund_execution_commands", 1)[1].split("RETURNING", 1)[0]


def test_p4d_lease_fence_source_binding_cancellation_and_error_code_contracts() -> None:
    source = _source(MIGRATION)
    model = _source(FOUNDATION)
    runtime = _source(RUNTIME_TEST)
    assert "lease_fence BIGINT NOT NULL DEFAULT 0" in source
    assert "lease_fence bigint" in source
    assert "p_lease_fence bigint" in source
    assert "AND c.lease_fence=p_lease_fence" in source
    assert "attempt_count=CASE WHEN candidates.reclaiming THEN c.attempt_count ELSE c.attempt_count + 1 END" in source
    assert "c.status = 'processing' AS reclaiming" in source
    assert "JOIN finance.refunds r ON r.id = c.refund_id" in source
    assert "r.status IN ('requested','approved','processing')" in source
    assert "FROM public.branch_outbox_events o" in source
    assert "v_source.tenant_id IS DISTINCT FROM v_payment.organization_id" in source
    assert "v_source.event_type <> 'branch.refund_required'" in source
    assert "last_error_code VARCHAR(64)" in source
    assert "last_error_code !~ '(bearer|secret|token)'" in source
    assert "v_error_code ~ '(bearer|secret|token)'" in source
    assert "^[a-z][a-z0-9_]{0,63}$" in source
    assert "lease_fence: Mapped[int]" in model
    assert "String(64)" in model
    assert "chk_finance_refund_execution_error_code" in model
    assert "_validate_safe_p4d_database" in runtime
    assert 'db_name != "gymflow_p4d_test"' in runtime
    assert "test_same_worker_expired_lease_reclaim_rotates_fence_and_rejects_stale_fence" in runtime
    assert "test_final_attempt_expired_processing_command_is_recoverable_without_incrementing_attempt" in runtime
    assert "test_cancelled_refund_command_is_not_claimable_but_history_remains" in runtime
    assert "test_migration_owner_cannot_read_nonempty_force_rls_command_table" in runtime


def test_p4d_capabilities_are_security_definer_fenced_and_public_revoked() -> None:
    source = _source(MIGRATION)
    signatures = _tuple_assignment(source, "_FUNCTIONS")
    assert set(signatures) == {
        "app_secure.materialize_refund_execution_command(uuid,text,uuid,text)",
        "app_secure.claim_refund_execution_command(uuid,integer)",
        "app_secure.record_refund_execution_failure(uuid,uuid,bigint,text,boolean)",
        "app_secure.discover_refund_execution_maintenance(integer)",
    }
    assert source.count("SECURITY DEFINER") >= len(signatures)
    assert source.count("SET search_path=pg_catalog,public,finance") >= len(signatures)
    assert source.count("SET row_security=on") >= len(signatures)
    assert "REVOKE ALL ON FUNCTION {signature} FROM PUBLIC" in source
    assert "GRANT EXECUTE ON FUNCTION app_secure.claim_refund_execution_command(uuid,integer) TO worker_runtime" in source
    assert "GRANT EXECUTE ON FUNCTION app_secure.record_refund_execution_failure(uuid,uuid,bigint,text,boolean) TO worker_runtime" in source
    assert "GRANT EXECUTE ON FUNCTION app_secure.discover_refund_execution_maintenance(integer) TO lifecycle_maintenance_runtime" in source
    assert "GRANT EXECUTE ON FUNCTION app_secure.claim_refund_execution_command(uuid,integer) TO lifecycle_maintenance_runtime" not in source
    assert "TO PUBLIC" not in source


def test_p4d_storage_is_force_rls_and_runtime_roles_have_no_direct_crud() -> None:
    source = _source(MIGRATION)
    assert "ALTER TABLE finance.refund_execution_commands ENABLE ROW LEVEL SECURITY" in source
    assert "ALTER TABLE finance.refund_execution_commands FORCE ROW LEVEL SECURITY" in source
    assert "REVOKE ALL ON TABLE finance.refund_execution_commands FROM PUBLIC" in source
    assert "GRANT SELECT, INSERT, UPDATE ON TABLE finance.refund_execution_commands TO app_security_owner" in source
    policy_block = source.split("CREATE POLICY p4d_refund_execution_security_owner_all", 1)[1].split("WITH CHECK (true)", 1)[0]
    assert "TO app_security_owner" in policy_block
    assert "migration_owner" not in policy_block
    assert "policy role/command drift" in source
    assert 'list(policy_row["roles"]) != [_SECURITY_OWNER]' in source
    assert "for role_name in _RUNTIME_ROLES:" in source
    for role in ("app_runtime", "auth_runtime", "worker_runtime", "lifecycle_maintenance_runtime"):
        assert f"TO {role}" not in source.replace("GRANT USAGE ON SCHEMA app_secure TO worker_runtime", "")\
            .replace("GRANT USAGE ON SCHEMA app_secure TO lifecycle_maintenance_runtime", "")\
            .replace("GRANT EXECUTE ON FUNCTION app_secure.materialize_refund_execution_command(uuid,text,uuid,text) TO worker_runtime", "")\
            .replace("GRANT EXECUTE ON FUNCTION app_secure.claim_refund_execution_command(uuid,integer) TO worker_runtime", "")\
            .replace("GRANT EXECUTE ON FUNCTION app_secure.record_refund_execution_failure(uuid,uuid,bigint,text,boolean) TO worker_runtime", "")\
            .replace("GRANT EXECUTE ON FUNCTION app_secure.discover_refund_execution_maintenance(integer) TO lifecycle_maintenance_runtime", "")
    assert "BYPASSRLS" not in source
    assert "GRANT ALL" not in source
    assert "DISABLE ROW LEVEL SECURITY" not in source


def test_p4d_downgrade_refuses_to_destroy_refund_execution_authority() -> None:
    source = _source(MIGRATION)
    downgrade = source.split("def downgrade()", 1)[1]
    evidence_check = downgrade.split("SELECT EXISTS(SELECT 1 FROM finance.refund_execution_commands LIMIT 1)", 1)[0]
    assert "SET LOCAL ROLE app_security_owner" in evidence_check
    assert "finally:" in downgrade
    assert "RESET ROLE" in downgrade.split("if has_refund_execution_evidence", 1)[0]
    assert "SELECT EXISTS(SELECT 1 FROM finance.refund_execution_commands LIMIT 1)" in downgrade
    assert "downgrade blocked: refund execution authority/evidence exists" in downgrade
    assert "DROP TABLE IF EXISTS finance.refund_execution_commands RESTRICT" in downgrade


def test_p4d_pg16_workflow_is_dedicated_locked_and_unactivated() -> None:
    workflow = _source(WORKFLOW)
    runtime = _source(RUNTIME_TEST)
    assert "name: P4D Refund Authority PG16" in workflow
    assert "- hardening/p4c-durable-notifications" in workflow
    assert "requirements-test.lock" in workflow
    assert "PostgreSQL 16" in workflow
    assert "zc07d8e9f0a3d" in workflow
    assert "current --check-heads" in workflow
    assert "tests/test_p4d_refund_authority_runtime.py" in workflow
    assert "tests/finance_core" in workflow
    assert "downgrade zb07d8e9f0a3c" in workflow
    assert "test_concurrent_materialization_creates_exactly_one_logical_command" in runtime
    assert "test_same_worker_expired_lease_reclaim_rotates_fence_and_rejects_stale_fence" in runtime
    assert "test_final_attempt_expired_processing_command_is_recoverable_without_incrementing_attempt" in runtime
    assert "test_source_outbox_tenant_and_type_are_authoritative" in runtime
    assert "test_record_failure_rejects_unsafe_error_codes_and_maintenance_exposes_machine_code_only" in runtime
    assert "test_cancelled_refund_command_is_not_claimable_but_history_remains" in runtime
    assert "discover_refund_execution_maintenance" in runtime
    maintenance_sql = _source(MIGRATION).split(
        "CREATE FUNCTION app_secure.discover_refund_execution_maintenance", 1
    )[1].split(
        "GRANT EXECUTE ON FUNCTION app_secure.discover_refund_execution_maintenance", 1
    )[0]
    assert "JOIN finance.refunds r ON r.id = c.refund_id" in maintenance_sql
    assert "r.status IN ('requested','approved','processing')" in maintenance_sql
    lowered = workflow.lower()
    assert "razorpay refund" not in lowered
    assert "branch.refund_required" not in lowered


def test_inherited_general_regression_grants_only_required_migration_database_connect() -> None:
    workflow = _source(GENERAL_WORKFLOW)
    statements = [
        line.strip()
        for line in workflow.splitlines()
        if "GRANT CONNECT ON DATABASE gymflow_migration_test" in line
    ]
    assert "- hardening/p4c-durable-notifications" in workflow
    assert "CREATE DATABASE gymflow_migration_test OWNER migration_owner;" in workflow
    assert "CREATE DATABASE gymflow_test OWNER migration_owner;" in workflow
    assert statements == [
        "GRANT CONNECT ON DATABASE gymflow_migration_test TO app_test_runtime;"
    ]
    assert "DATABASE_URL: postgresql+asyncpg://migration_owner:" in workflow
    assert "TEST_DATABASE_URL: postgresql+asyncpg://app_test_runtime:" in workflow
    assert "zc07d8e9f0a3d" in workflow



def test_inherited_general_regression_provisions_declared_p4d_runtime_database() -> None:
    workflow = _source(GENERAL_WORKFLOW)
    statements = [
        line.strip()
        for line in workflow.splitlines()
        if "GRANT CONNECT ON DATABASE gymflow_p4d_test" in line
    ]
    assert "APP_RUNTIME_PASSWORD: ci-app-test-runtime" in workflow
    assert "P4D_REFUND_TEST_DATABASE: gymflow_p4d_test" in workflow
    assert "CREATE DATABASE gymflow_p4d_test OWNER migration_owner;" in workflow
    assert "REVOKE ALL ON DATABASE gymflow_p4d_test FROM PUBLIC;" in workflow
    assert statements == [
        "GRANT CONNECT ON DATABASE gymflow_p4d_test TO "
        "app_test_runtime,worker_test_runtime,lifecycle_maintenance_test_runtime;"
    ]
    assert "for database in gymflow_migration_test gymflow_test gymflow_p4d_test; do" in workflow
    p4d_database_url = (
        "DATABASE_URL=postgresql+asyncpg://migration_owner:ci-migration-owner@"
        "127.0.0.1:5432/gymflow_p4d_test"
    )
    assert workflow.count(p4d_database_url) == 2
    assert workflow.count("python -s -m alembic -c alembic.ini current --check-heads") == 3
