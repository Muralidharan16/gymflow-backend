from __future__ import annotations

import json
import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "zc07d8e9f0a3d_p4d_refund_authority_boundary.py"
P4D2_MIGRATION = ROOT / "alembic" / "versions" / "zd07d8e9f0a3e_p4d_refund_obligation_resolution.py"
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
    migration = (_source(MIGRATION) + _source(P4D2_MIGRATION)).lower()
    poller = _source(POLLER)
    guards = _source(FINANCE_GUARDS)
    assert '"branch.refund_required"' in poller
    assert "_REFUND_EVALUATION_EVENT_TYPES" in poller
    assert "resolve_branch_refund_required" in poller
    assert "provider refund api call" in migration
    assert "requests." not in migration
    assert "httpx" not in migration
    # Provider-specific callback vocabulary is now legitimate, but
    # P4D must still contain no outbound provider/network execution.
    for forbidden_outbound_provider_token in (
        "import razorpay",
        "from razorpay",
        "razorpay.client",
        "requests.",
        "httpx",
        "aiohttp",
        "urllib.request",
        "http.client",
    ):
        assert forbidden_outbound_provider_token not in migration

    assert "app_secure.record_finance_checkout_callback" in migration
    assert "razorpay.checkout.callback.verified" in migration
    assert "razorpay_sandbox" in migration
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
    assert "_required_database_url" in runtime
    assert "TEST_DATABASE_URL" in runtime
    assert "fixed local database defaults are forbidden" in runtime
    assert "test_same_worker_expired_lease_reclaim_rotates_fence_and_rejects_stale_fence" in runtime
    assert "test_final_attempt_expired_processing_command_is_recoverable_without_incrementing_attempt" in runtime
    assert "test_cancelled_refund_command_is_not_claimable_but_history_remains" in runtime
    assert "test_migration_owner_cannot_read_nonempty_force_rls_command_table" in runtime


def test_p4d_runtime_harness_database_routing_is_environment_driven() -> None:
    runtime = _source(RUNTIME_TEST)
    routing_block = runtime.split("def _required_database_url", 1)[1].split("def _reset_state", 1)[0]
    assert 'os.environ.get(env_name)' in routing_block
    assert 'TEST_DATABASE_URL' in runtime
    assert 'TEST_ADMIN_DATABASE_URL' in runtime
    assert 'fixed local database defaults are forbidden' in runtime
    assert 'os.environ.get("P4D_REFUND_TEST_HOST", "127.0.0.1")' not in runtime
    assert 'os.environ.get("PGPORT", "5432")' not in runtime
    assert 'os.environ.get("P4D_REFUND_TEST_DATABASE", "gymflow_p4d_test")' not in runtime
    assert 'dbname="gymflow_p4d_test"' not in runtime
    assert '_assert_same_disposable_topology' in runtime



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


def test_p4d_downgrade_restores_exact_predecessor_outbox_select_acl() -> None:
    source = _source(MIGRATION)
    downgrade = source.split("def downgrade()", 1)[1]
    predecessor_columns = set(
        _tuple_assignment(source, "_OUTBOX_PREDECESSOR_SELECT_COLUMNS")
    )

    assert predecessor_columns == {
        "outbox_id",
        "tenant_id",
        "branch_id",
        "event_type",
        "status",
        "leased_by",
        "leased_until",
        "correlation_id",
        "attempt_count",
        "payload",
    }
    revoke_index = downgrade.index(
        'REVOKE SELECT ON TABLE public.branch_outbox_events FROM app_security_owner'
    )
    restore_index = downgrade.index(
        '"GRANT SELECT (" + ",".join(_OUTBOX_PREDECESSOR_SELECT_COLUMNS)'
    )
    assert revoke_index < restore_index
    assert "GRANT SELECT ON TABLE public.branch_outbox_events TO app_security_owner" not in (
        downgrade[revoke_index:]
    )


def test_p4d_pg16_workflow_is_dedicated_locked_and_unactivated() -> None:
    workflow = _source(WORKFLOW)
    runtime = _source(RUNTIME_TEST)
    assert "name: P4D Refund Authority PG16" in workflow
    assert "- hardening/p4c-durable-notifications" in workflow
    assert "requirements-test.lock" in workflow
    assert "PostgreSQL 16" in workflow
    assert "Prove PostgreSQL UTF8 text adapter contract" in workflow
    assert "SHOW server_encoding" in workflow
    assert "SHOW client_encoding" in workflow
    assert "test \"${server_encoding}\" = 'UTF8'" in workflow
    assert "test \"${client_encoding}\" = 'UTF8'" in workflow
    assert "SQL_ASCII" not in workflow
    assert (
        "test \"$(python -s -m alembic -c alembic.ini heads "
        "| awk '{print $1}')\" = 'ze07d8e9f0a3f'"
        in workflow
    )
    assert "downgrade zc07d8e9f0a3d" in workflow
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
    assert "ze07d8e9f0a3f" in workflow



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
        "app_test_runtime,worker_test_runtime,lifecycle_maintenance_test_runtime,finance_config_deployment;"
    ]
    assert "for database in gymflow_migration_test gymflow_test gymflow_p4d_test; do" in workflow
    p4d_database_url = (
        "DATABASE_URL=postgresql+asyncpg://migration_owner:ci-migration-owner@"
        "127.0.0.1:5432/gymflow_p4d_test"
    )
    assert workflow.count(p4d_database_url) == 2
    assert workflow.count("python -s -m alembic -c alembic.ini current --check-heads") == 3
    assert workflow.count("Run dedicated P4D refund runtime suite") == 1
    assert (
        "TEST_DATABASE_URL: "
        "postgresql+asyncpg://app_test_runtime:"
        "ci-app-test-runtime@127.0.0.1:5432/"
        "gymflow_p4d_test"
        in workflow
    )
    assert (
        "TEST_ADMIN_DATABASE_URL: "
        "postgresql+asyncpg://migration_owner:"
        "ci-migration-owner@127.0.0.1:5432/"
        "gymflow_p4d_test"
        in workflow
    )
    assert (
        "FINANCE_CONFIG_DATABASE_URL: "
        "postgresql://finance_config_deployment:"
        "ci-finance-config-runtime@127.0.0.1:5432/"
        "gymflow_p4d_test"
        in workflow
    )
    assert "python -m pytest --noconftest -q --maxfail=1" in workflow
    assert workflow.count(
        "tests/test_p4d_refund_authority_runtime.py"
    ) == 2
    assert workflow.count(
        "tests/test_p4d_refund_obligation_resolution_runtime.py"
    ) == 2
    assert workflow.count(
        "--ignore=tests/test_p4d_refund_authority_runtime.py"
    ) == 1
    assert workflow.count(
        "--ignore=tests/test_p4d_refund_obligation_resolution_runtime.py"
    ) == 1


def test_p4d2_workflow_proves_obligation_evidence_blocks_downgrade() -> None:
    workflow = _source(WORKFLOW)
    assert "Prove inherited P4D-1 protected evidence downgrade refusal" in workflow
    assert "Prove P4D-2 obligation evidence downgrade refusal" in workflow
    assert "test_exactly_one_refundable_payment_creates_authoritative_refund_and_command" in workflow
    assert "python -s -m alembic -c alembic.ini downgrade zc07d8e9f0a3d" in workflow
    assert "zd07 downgrade destroyed P4D-2-derived refund execution evidence" in workflow
    assert "branch-refund-required/%" not in workflow
    assert "idempotency_key LIKE 'branch-refund-required/%'" not in workflow
    assert "JOIN finance.refunds r ON r.id = c.refund_id" in workflow
    assert "r.reason_code = 'branch-refund:' || c.source_id::text" in workflow
    assert "c.logical_obligation_key = 'finance-refund/' || c.refund_id::text" in workflow


def test_p4d2_migration_identity_checks_do_not_require_migration_owner_app_secure_usage() -> None:
    source = _source(P4D2_MIGRATION)
    predecessor = source.split("def _require_predecessor", 1)[1].split("def _install_function", 1)[0]
    post_install = source.split("def _post_install_proof", 1)[1].split("def upgrade", 1)[0]
    assert "to_regprocedure" not in source
    assert "::regprocedure" not in source
    assert "oid::text" not in source
    assert "-> list[int]" in source
    assert "return [int(row) for row in rows]" in source
    assert "catalog OID must remain an integer" in source
    assert "pg_catalog.pg_proc" in source
    assert "pg_catalog.pg_namespace" in source
    assert "replace(pg_catalog.oidvectortypes(p.proargtypes), ' ', '') = :normalized_args" in source
    assert 'function_name="materialize_refund_execution_command"' in source
    assert 'normalized_args="uuid,text,uuid,text"' in source
    assert 'function_name="resolve_branch_refund_required"' in source
    assert 'normalized_args="uuid,uuid"' in source
    assert "has_function_privilege(:role_name, CAST(:function_oid AS oid), 'EXECUTE')" in source
    assert 'allowed_role="worker_runtime"' in post_install
    assert 'allowed_role="app_runtime"' in post_install
    assert "has_function_privilege('worker_runtime', :signature" not in source
    assert "has_function_privilege(:role_name, :signature" not in source
    assert "GRANT USAGE ON SCHEMA app_secure TO migration_owner" not in source
    assert "zd07 migration_owner must not have app_secure USAGE" in source
    assert "SET LOCAL ROLE app_security_owner" not in predecessor


def test_p4d2_refund_obligation_resolution_uses_finance_state_and_p4d1_command_boundary() -> None:
    source = _source(P4D2_MIGRATION)
    poller = _source(POLLER)
    assert 'revision = "zd07d8e9f0a3e"' in source
    assert 'down_revision = "zc07d8e9f0a3d"' in source
    assert "CREATE FUNCTION app_secure.resolve_branch_refund_required" in source
    assert "SECURITY DEFINER" in source
    assert "SET search_path=pg_catalog,public,finance" in source
    assert "SET row_security=on" in source
    assert "pg_has_role(session_user, 'worker_runtime', 'MEMBER')" in source
    assert "o.status = 'processing'" in source
    assert "o.leased_by = p_worker_id" in source
    assert "v_source.event_type <> 'branch.refund_required'" in source
    resolver_body = source.split("CREATE FUNCTION app_secure.resolve_branch_refund_required", 1)[1].split("$$;", 1)[0]
    assert "payload ? 'refund_required'" not in resolver_body
    assert "payload->>'refund_required'" not in resolver_body
    assert "FROM public.org_branches b" in source
    assert "b.id = v_source.branch_id" in source
    assert "b.org_id = v_source.tenant_id" in source
    assert "FROM finance.payments p" in source
    assert "p.organization_id = v_source.tenant_id" in source
    assert "p.status IN ('captured','settled')" in source
    assert "ORDER BY p.created_at, p.id" in source
    assert "FOR UPDATE" in source
    assert "CREATE TABLE finance.refund_obligation_bindings" in source
    assert "uq_refund_obligation_bindings_invoice" in source
    assert "FROM finance.payment_allocations a" in source
    assert "JOIN finance.invoices i ON i.id = a.invoice_id" in source
    assert "LEFT JOIN finance.refund_obligation_bindings b ON b.invoice_id = i.id" in source
    assert "b.branch_id = v_source.branch_id" in source
    assert "v_in_scope_allocated IS DISTINCT FROM v_allocated" in source
    assert "FROM finance.refunds r" in source
    assert "r.status <> 'cancelled'" in source
    assert "ambiguous across multiple refundable payments" in source
    assert "deterministic source refund reason is ambiguous across payments" in source
    assert "branch-refund:' || p_source_outbox_id::text" in source
    assert "app_secure.materialize_refund_execution_command" in source
    assert "branch-refund-required/" in source
    assert "provider_code::text" in source
    assert "provider_payment_ref::text" in source
    assert "_REFUND_EVALUATION_EVENT_TYPES" in poller
    assert "_process_refund_required_event" in poller
    assert "No production handler is configured for" in poller


def test_p4d2_privilege_surface_is_bounded() -> None:
    source = _source(P4D2_MIGRATION)
    upgrade_source = source.split("def upgrade()", 1)[0]
    assert "GRANT EXECUTE ON FUNCTION app_secure.resolve_branch_refund_required(uuid,uuid) TO worker_runtime" in source
    assert "REVOKE ALL ON FUNCTION" in source
    assert "TO PUBLIC" not in source
    assert "BYPASSRLS" not in source
    assert "GRANT ALL" not in source
    assert "DISABLE ROW LEVEL SECURITY" not in upgrade_source
    assert "GRANT SELECT ON TABLE finance.payment_allocations TO app_security_owner" in source
    assert "GRANT SELECT ON TABLE finance.invoices TO app_security_owner" in source
    assert '("finance.invoices", "SELECT")' in source
    assert "app_security_owner read-only ACL drift on {relation}" in source
    assert "REVOKE SELECT ON TABLE finance.invoices FROM app_security_owner" in source
    assert "ALTER TABLE finance.refund_obligation_bindings OWNER TO app_security_owner" not in source
    assert "ALTER TABLE finance.refund_obligation_bindings ENABLE ROW LEVEL SECURITY" in source
    assert "ALTER TABLE finance.refund_obligation_bindings FORCE ROW LEVEL SECURITY" in source
    assert "GRANT SELECT, INSERT ON TABLE finance.refund_obligation_bindings TO app_security_owner" in source
    assert "CREATE POLICY p4d_refund_obligation_security_owner_select" in source
    assert "CREATE POLICY p4d_refund_obligation_security_owner_insert" in source
    assert "CREATE POLICY p4d_refund_obligation_security_owner_all" not in source
    assert "GRANT INSERT ON TABLE finance.refunds TO app_security_owner" in source
    assert "GRANT SELECT ON TABLE public.member_subscriptions_v2 TO app_security_owner" in source
    assert "GRANT SELECT ON TABLE public.membership_plans TO app_security_owner" in source
    assert "CREATE POLICY p4d_membership_plans_security_owner_select ON public.membership_plans FOR SELECT TO app_security_owner USING (true)" in source
    assert "zd07 membership plan security-owner policy drift" in source
    assert "CREATE POLICY p4d_member_subscriptions_v2_security_owner_select" in source
    assert "refund obligation binding table owner/RLS/PUBLIC ACL drift" in source
    assert "app_security_owner binding DML ACL drift" in source
    assert "finance.invoices','SELECT'" in source
    assert "direct Finance table privilege leaked" in source
    assert "_require_predecessor_present_acl" in source
    assert "_require_predecessor_absent_acl" in source
    assert "zd07 predecessor privilege drift" in source
    assert "zd07 refuses to claim preexisting ACL" in source
    assert "downgrade failed to remove zd07-owned ACL" in source
    assert "has_column_privilege('app_security_owner','public.org_branches','id','SELECT')" in source
    assert "CASE WHEN pg_catalog.to_regclass(:relation) IS NULL THEN false" in source
    assert "REVOKE SELECT ON TABLE public.org_branches FROM app_security_owner" not in source
    assert "lifecycle_maintenance_runtime" in source
    assert "execute leaked" in source


def test_p4d2_static_contracts_cover_zero_one_many_and_concurrency_runtime_proofs() -> None:
    runtime = _source(ROOT / "tests" / "test_p4d_refund_obligation_resolution_runtime.py")
    for expected in (
        "test_payload_refund_required_false_cannot_suppress_valid_event",
        "test_actual_producer_payload_context_still_evaluates",
        "test_zero_refundable_payment_creates_no_fabricated_obligation",
        "test_cross_branch_same_org_payment_is_not_refunded_for_branch_a_event",
        "test_exact_branch_bound_payment_is_selected_despite_unrelated_org_payment",
        "test_mixed_branch_allocations_for_same_payment_fail_closed",
        "test_exactly_one_refundable_payment_creates_authoritative_refund_and_command",
        "test_multiple_refundable_payments_fail_closed",
        "test_partially_and_fully_refunded_payments_use_remaining_balance_only",
        "test_cancelled_deterministic_refund_suppresses_command_generation",
        "test_duplicate_source_reason_on_two_payments_fails_closed_without_first_row_selection",
        "test_duplicate_source_event_reuses_refund_and_command",
        "test_concurrent_duplicate_source_event_converges_to_one_command",
        "test_concurrent_different_events_do_not_over_refund_same_payment",
        "test_source_branch_tenant_mismatch_fails_closed",
        "test_fixture_symbol_topology_constants_are_defined_and_unique",
        "test_org_b_source_graph_uses_org_b_branch_and_parents",
        "test_cross_tenant_fixture_uses_org_a_invoice_and_org_b_source_with_tenant_isolation",
        "test_branch_mismatch_fixture_uses_same_org_distinct_branches",
        "test_payload_spoofing_financial_fields_is_ignored",
        "test_refund_obligation_binding_table_keeps_migration_owner_with_force_rls_zero_visibility",
        "test_worker_runtime_gets_resolver_only_without_direct_finance_privileges",
    ):
        assert expected in runtime



def test_p4d2_source_bound_obligation_producer_uses_member_subscription_authority_only() -> None:
    source = _source(P4D2_MIGRATION)
    model = _source(ROOT / "app" / "models" / "member_subscription_v2.py")
    assert "CREATE FUNCTION app_secure.record_refund_obligation_binding" in source
    producer_body = source.split("CREATE FUNCTION app_secure.record_refund_obligation_binding", 1)[1].split("$$;", 1)[0]
    assert "p_branch_id" not in producer_body
    assert "p_organization_id" not in producer_body
    assert "p_source_table" not in producer_body
    assert "pg_has_role(session_user, 'app_runtime', 'MEMBER')" in producer_body
    assert "current_setting('app.current_org_id', true)" in producer_body
    assert "FROM public.member_subscriptions_v2 s" in producer_body
    assert "FROM finance.invoices i" in producer_body
    assert "v_invoice_org_id IS DISTINCT FROM v_source.org_id" in producer_body
    assert "CONSTRAINT uq_refund_obligation_bindings_invoice UNIQUE (invoice_id)" in source
    assert "ON CONFLICT ON CONSTRAINT uq_refund_obligation_bindings_invoice DO NOTHING" in producer_body
    assert "ON CONFLICT (invoice_id)" not in producer_body
    assert "ON CONFLICT (invoice_id) DO UPDATE" not in producer_body
    assert "P4D refund obligation binding replay conflict" in producer_body
    assert "'member_subscriptions_v2'" in producer_body
    assert "subscription_terms" not in source
    assert "GRANT EXECUTE ON FUNCTION app_secure.record_refund_obligation_binding(uuid,uuid) TO app_runtime" in source
    assert "execute leaked to {role_name}" in source
    assert "fk_member_subscriptions_v2_branch_org" in source
    assert "FOREIGN KEY (branch_id, org_id) REFERENCES public.org_branches(id, org_id) ON DELETE RESTRICT" in source
    assert "uq_finance_invoices_id_org" in source
    assert "UNIQUE (id, organization_id)" in source
    assert "fk_refund_obligation_bindings_invoice_org" in source
    assert "FOREIGN KEY (invoice_id, organization_id) REFERENCES finance.invoices(id, organization_id) ON DELETE RESTRICT" in source
    assert "CHECK (source_table = 'member_subscriptions_v2')" in source
    assert "ForeignKeyConstraint" in model
    assert "fk_member_subscriptions_v2_branch_org" in model


def test_p4d2_zd07_plpgsql_uses_explicit_conflict_targets_without_parser_workarounds() -> None:
    source = _source(P4D2_MIGRATION)
    assert "plpgsql.variable_conflict" not in source
    assert "#variable_conflict" not in source

    refund_body = source.split("CREATE FUNCTION app_secure.record_refund_obligation_binding", 1)[1].split("$$;", 1)[0]
    billing_body = source.split("CREATE FUNCTION app_secure.upsert_member_billing_party", 1)[1].split("$$;", 1)[0]
    checkout_body = source.split("CREATE FUNCTION app_secure.record_member_subscription_checkout_binding", 1)[1].split("$$;", 1)[0]

    assert "CONSTRAINT uq_refund_obligation_bindings_invoice UNIQUE (invoice_id)" in source
    assert "ON CONFLICT ON CONSTRAINT uq_refund_obligation_bindings_invoice DO NOTHING" in refund_body
    assert "ON CONFLICT (invoice_id)" not in refund_body

    assert "CREATE UNIQUE INDEX uq_finance_billing_parties_member_buyer" in source
    assert "ON CONFLICT (organization_id, member_id)" not in billing_body
    assert "WHERE bp.organization_id = v_current_org_id" in billing_body
    assert "WHERE bp.id = v_billing_party_id" in billing_body
    assert "RETURNING bp.id INTO v_billing_party_id" in billing_body
    assert "RETURNING billing_parties.id INTO v_billing_party_id" in billing_body

    assert "CONSTRAINT uq_member_subscription_checkout_bindings_subscription UNIQUE (subscription_id)" in source
    assert "ON CONFLICT ON CONSTRAINT uq_member_subscription_checkout_bindings_subscription DO NOTHING" in checkout_body
    assert "ON CONFLICT (subscription_id)" not in checkout_body

    for body in (refund_body, billing_body, checkout_body):
        assert "SECURITY DEFINER" in body
        assert "SET search_path=pg_catalog,public,finance" in body
        assert "SET row_security=on" in body


def test_p4d2_app_secure_return_and_idempotency_contracts_are_explicit() -> None:
    source = _source(P4D2_MIGRATION)
    runtime = _source(ROOT / "tests" / "test_p4d_refund_obligation_resolution_runtime.py")
    service = _source(ROOT / "app" / "finance_core" / "services" / "member_subscription_checkout.py")
    members_router = _source(ROOT / "app" / "routers" / "members.py")
    poller = _source(POLLER)

    refund_body = source.split("CREATE FUNCTION app_secure.record_refund_obligation_binding", 1)[1].split("$$;", 1)[0]
    billing_body = source.split("CREATE FUNCTION app_secure.upsert_member_billing_party", 1)[1].split("$$;", 1)[0]
    checkout_resolver_body = source.split("CREATE FUNCTION app_secure.resolve_member_subscription_checkout_inputs", 1)[1].split("$$;", 1)[0]
    checkout_binding_body = source.split("CREATE FUNCTION app_secure.record_member_subscription_checkout_binding", 1)[1].split("$$;", 1)[0]
    provider_body = source.split("CREATE FUNCTION app_secure.attach_member_subscription_checkout_provider_order", 1)[1].split("$$;", 1)[0]
    refund_resolver_body = source.split("CREATE FUNCTION app_secure.resolve_branch_refund_required", 1)[1].split("$$;", 1)[0]

    assert "invoice_id uuid" in refund_body
    assert "source_id uuid" in refund_body
    assert "inserted boolean" in refund_body
    assert "replayed boolean" in refund_body
    assert "GET DIAGNOSTICS v_row_count = ROW_COUNT" in refund_body
    assert "P4D refund obligation binding replay conflict" in refund_body
    assert "source_table TEXT NOT NULL" in source
    assert "source_table text" in refund_body
    assert "v_binding.source_table, v_binding.source_id, v_row_count = 1, v_row_count = 0" in refund_body
    assert ".decode()" not in runtime

    assert "subscription_id uuid" in checkout_binding_body
    assert "checkout_intent_id uuid" in checkout_binding_body
    assert "inserted boolean" in checkout_binding_body
    assert "replayed boolean" in checkout_binding_body
    assert "GET DIAGNOSTICS v_row_count = ROW_COUNT" in checkout_binding_body
    assert "P4D member subscription checkout binding replay conflict" in checkout_binding_body
    assert "v_source_org, v_row_count = 1, v_row_count = 0" in checkout_binding_body

    assert "attached boolean" in provider_body
    assert "replayed boolean" in provider_body
    assert "RETURN QUERY SELECT p_checkout_intent_id, p_provider_order_ref, false, true" in provider_body
    assert "RETURN QUERY SELECT p_checkout_intent_id, p_provider_order_ref, true, false" in provider_body
    assert "P4D member subscription checkout provider replay conflict" in provider_body

    assert "billing_party_id uuid" in billing_body
    assert "RETURN QUERY SELECT v_billing_party_id, v_current_org_id, p_member_id, 'member'::text, 'individual'::text, 'b2c'::text" in billing_body
    assert "pricing_mode text" in checkout_resolver_body
    assert "reused_refund boolean" in refund_resolver_body
    assert "reused_command boolean" in refund_resolver_body

    assert "_semantic_row" in runtime
    assert "test_refund_obligation_binding_return_contract_first_replay_and_conflict" in runtime
    assert "test_refund_obligation_binding_cross_tenant_source_fails_closed" in runtime
    assert "test_member_subscription_checkout_binding_return_contract_first_replay_and_conflict" in runtime
    assert '"source_id": _SUBSCRIPTION_A' in runtime
    assert '"inserted": True' in runtime
    assert '"replayed": True' in runtime

    assert "app_secure.record_refund_obligation_binding" in service
    assert "app_secure.record_member_subscription_checkout_binding" in service
    assert "app_secure.attach_member_subscription_checkout_provider_order" in service
    assert "row = result.mappings().one()" in members_router
    assert "MemberBillingPartyResponse(**dict(row))" in members_router
    assert "resolution = dict(result.mappings().one())" in poller


def test_p4d2_binding_policies_are_split_and_do_not_allow_update_delete() -> None:
    source = _source(P4D2_MIGRATION)
    assert "CREATE POLICY p4d_refund_obligation_security_owner_select" in source
    assert "FOR SELECT" in source
    assert "CREATE POLICY p4d_refund_obligation_security_owner_insert" in source
    assert "FOR INSERT" in source
    assert "CREATE POLICY p4d_refund_obligation_security_owner_all" not in source
    assert '"command": "SELECT"' in source
    assert '"command": "INSERT"' in source
    assert '"command": "ALL"' not in source.split("expected_policy =", 1)[1].split("observed_policy", 1)[0]
    assert "NOT pg_catalog.has_table_privilege('app_security_owner','finance.refund_obligation_bindings','UPDATE')" in source
    assert "NOT pg_catalog.has_table_privilege('app_security_owner','finance.refund_obligation_bindings','DELETE')" in source


def test_p4d2_downgrade_refuses_binding_rows_and_exact_refund_intent_namespace() -> None:
    source = _source(P4D2_MIGRATION)
    downgrade = source.split("def downgrade()", 1)[1]
    assert "SELECT EXISTS(SELECT 1 FROM finance.refund_obligation_bindings LIMIT 1)" in downgrade
    assert "downgrade blocked: refund obligation binding authority/evidence exists" in downgrade
    assert "reason_code ~ '^branch-refund:" in downgrade
    assert "downgrade blocked: P4D-2 Finance refund intent evidence exists" in downgrade
    assert "DROP FUNCTION IF EXISTS app_secure.record_refund_obligation_binding(uuid,uuid)" in downgrade
    assert "DROP POLICY IF EXISTS p4d_refund_obligation_security_owner_insert" in downgrade
    assert "DROP POLICY IF EXISTS p4d_refund_obligation_security_owner_select" in downgrade
    assert "DROP POLICY IF EXISTS p4d_member_subscriptions_v2_security_owner_select" in downgrade
    assert "DROP POLICY IF EXISTS p4d_membership_plans_security_owner_select ON public.membership_plans" in downgrade
    assert "DROP CONSTRAINT IF EXISTS uq_finance_invoices_id_org RESTRICT" in downgrade
    assert "DROP CONSTRAINT IF EXISTS fk_member_subscriptions_v2_branch_org RESTRICT" in downgrade



def test_p4d2_membership_billing_party_foundation_is_member_bound_not_polymorphic() -> None:
    import ast
    source = _source(P4D2_MIGRATION)
    model = _source(ROOT / "app" / "finance_core" / "models" / "foundation.py")
    migration_tree = ast.parse(source)
    billing_party_sql_fragments = [
        sql
        for item in ast.walk(migration_tree)
        if isinstance(item, ast.Constant)
        and isinstance(item.value, str)
        for sql in (item.value,)
        if "billing_parties" in sql
    ]
    assert billing_party_sql_fragments
    assert all(
        "source_type" not in fragment
        for fragment in billing_party_sql_fragments
    )
    assert "source_id" in source  # refund obligation legacy source remains fixed by source_table check.
    assert "ALTER TABLE finance.billing_parties ADD COLUMN buyer_kind TEXT NOT NULL DEFAULT 'organization'" in source
    assert "ALTER TABLE finance.billing_parties ADD COLUMN member_id UUID NULL" in source
    assert "CHECK (buyer_kind IN ('organization','member'))" in source
    assert "fk_finance_billing_parties_member_org" in source
    assert "REFERENCES public.members(id, org_id) ON DELETE RESTRICT" in source
    assert "DROP CONSTRAINT uq_finance_billing_parties_organization" in source
    assert "CREATE UNIQUE INDEX uq_finance_billing_parties_org_buyer" in source
    assert "WHERE buyer_kind = 'organization'" in source
    assert "CREATE UNIQUE INDEX uq_finance_billing_parties_member_buyer" in source
    assert "WHERE buyer_kind = 'member'" in source
    assert "buyer_kind: Mapped[str]" in model
    assert "member_id: Mapped[uuid.UUID | None]" in model
    assert "fk_finance_billing_parties_member_org" in model


def test_p4d2_runtime_fixture_symbols_are_defined_and_topology_guarded() -> None:
    runtime_path = ROOT / "tests" / "test_p4d_refund_obligation_resolution_runtime.py"
    runtime = _source(runtime_path)
    tree = ast.parse(runtime)
    fixture_name = re.compile(
        r"^_(ORG|BRANCH|MEMBER|PLAN|SUBSCRIPTION|INVOICE|PAYMENT|SOURCE|WORKER|CORRELATION|GST|DIVISION|BRAND|BILLING)_[A-Z0-9_]+$"
    )
    imported: set[str] = set()
    declared: set[str] = set()
    referenced: set[str] = set()
    uuid_literals: dict[str, str] = {}

    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "tests.test_p4d_refund_authority_runtime":
            imported.update(alias.asname or alias.name for alias in node.names)
        if not isinstance(node, ast.Assign):
            continue
        call = node.value
        for target in node.targets:
            if not isinstance(target, ast.Name) or not fixture_name.match(target.id):
                continue
            declared.add(target.id)
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "UUID"
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "uuid"
                and len(call.args) == 1
                and isinstance(call.args[0], ast.Constant)
                and isinstance(call.args[0].value, str)
            ):
                uuid_literals[target.id] = call.args[0].value

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and fixture_name.match(node.id):
            referenced.add(node.id)

    unresolved = referenced - declared - imported
    assert unresolved == set()
    for required_import in ("_ORG_A", "_ORG_B", "_BRANCH_A", "_BRANCH_B", "_PAYMENT_A", "_SOURCE_A"):
        assert required_import in imported
    for required_local in (
        "_BRANCH_C",
        "_MEMBER_A",
        "_MEMBER_B",
        "_PLAN_A",
        "_PLAN_B",
        "_SUBSCRIPTION_A",
        "_SUBSCRIPTION_B",
        "_SUBSCRIPTION_C_A",
        "_SUBSCRIPTION_C_C",
        "_INVOICE_A",
        "_INVOICE_C",
    ):
        assert required_local in declared

    duplicates = {
        value: sorted(name for name, candidate in uuid_literals.items() if candidate == value)
        for value in set(uuid_literals.values())
        if list(uuid_literals.values()).count(value) > 1
    }
    assert duplicates == {}

    assert "test_fixture_symbol_topology_constants_are_defined_and_unique" in runtime
    assert "test_org_b_source_graph_uses_org_b_branch_and_parents" in runtime
    assert "test_cross_tenant_fixture_uses_org_a_invoice_and_org_b_source_with_tenant_isolation" in runtime
    assert "test_branch_mismatch_fixture_uses_same_org_distinct_branches" in runtime
    assert "def _tenant_fetchone(org_id: uuid.UUID" in runtime
    tenant_fetch = runtime.split("def _tenant_fetchone", 1)[1].split("def _seed_org_b_subscription", 1)[0]
    assert 'with _connect(_APP_LOGIN, "APP_RUNTIME_PASSWORD")' in tenant_fetch
    assert "app.current_org_id" in tenant_fetch
    assert 'with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD")' not in tenant_fetch

    assert '_seed_org_b_subscription' in runtime
    org_b_seed = runtime.split("def _seed_org_b_subscription", 1)[1].split("def _fixture_invoice_business_key", 1)[0]
    assert 'with _connect(_APP_LOGIN, "APP_RUNTIME_PASSWORD")' in org_b_seed
    assert 'with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD")' not in org_b_seed
    assert "_BRANCH_B" in org_b_seed
    assert "_ORG_B" in org_b_seed

    org_b_topology = runtime.split("def test_org_b_source_graph_uses_org_b_branch_and_parents", 1)[1].split(
        "def test_cross_tenant_fixture_uses_org_a_invoice_and_org_b_source_with_tenant_isolation", 1
    )[0]
    assert "_tenant_fetchone(_ORG_B" in org_b_topology
    assert "_security_owner_fetchone" not in org_b_topology
    assert "_CONFIG_LOGIN" not in org_b_topology
    assert "SELECT org_id, home_branch_id" in org_b_topology
    assert "SELECT org_id, branch_id FROM public.membership_plans" in org_b_topology

    cross_tenant_topology = runtime.split(
        "def test_cross_tenant_fixture_uses_org_a_invoice_and_org_b_source_with_tenant_isolation", 1
    )[1].split("def test_branch_mismatch_fixture_uses_same_org_distinct_branches", 1)[0]
    assert "_tenant_fetchone(_ORG_B" in cross_tenant_topology
    assert "_tenant_fetchone(_ORG_A" in cross_tenant_topology
    assert " is None" in cross_tenant_topology
    assert "public.members" in cross_tenant_topology
    assert "public.member_subscriptions_v2" in cross_tenant_topology
    assert "_CONFIG_LOGIN" not in cross_tenant_topology
    assert "SELECT org_id, branch_id FROM public.member_subscriptions_v2" in cross_tenant_topology

    branch_mismatch_topology = runtime.split("def test_branch_mismatch_fixture_uses_same_org_distinct_branches", 1)[1].split(
        "def test_refund_obligation_binding_return_contract_first_replay_and_conflict", 1
    )[0]
    assert "_tenant_fetchone(_ORG_A" in branch_mismatch_topology
    assert '_security_owner_fetchone("SELECT org_id' not in branch_mismatch_topology


def test_p4d2_runtime_fixture_business_identities_are_deterministic_and_collision_safe() -> None:
    runtime = _source(ROOT / "tests" / "test_p4d_refund_obligation_resolution_runtime.py")
    key_helper = runtime.split("def _fixture_invoice_business_key", 1)[1].split("def _seed_invoice", 1)[0]
    conflict_test = runtime.split("def test_refund_obligation_binding_return_contract_first_replay_and_conflict", 1)[1].split("def test_refund_obligation_binding_cross_tenant_source_fails_closed", 1)[0]
    assert "A-PRIMARY" in key_helper
    assert "C-SECONDARY" in key_helper
    assert "C-BRANCH-C" in key_helper
    assert "invoice_id.hex[:" not in key_helper
    assert "str(invoice_id)[:" not in key_helper
    assert "left(" not in runtime
    assert "create_invoice=False" in conflict_test
    assert "test_distinct_fixture_invoices_can_coexist_under_official_number_constraint" in runtime
    assert "test_same_invoice_different_source_fixture_does_not_create_second_invoice" in runtime


def test_p4d2_runtime_fixture_matches_zd07_billing_and_source_binding_shape() -> None:
    runtime = _source(ROOT / "tests" / "test_p4d_refund_obligation_resolution_runtime.py")
    seed_invoice = runtime.split("def _seed_invoice", 1)[1].split("def _allocate", 1)[0]
    assert "INSERT INTO finance.billing_parties" in seed_invoice
    assert "buyer_kind,member_id" in seed_invoice
    assert "'organization',NULL" in seed_invoice
    assert "INSERT INTO public.members(" in seed_invoice
    assert "INSERT INTO public.membership_plans(" in seed_invoice
    assert "INSERT INTO public.member_subscriptions_v2(" in seed_invoice
    assert "_fixture_invoice_business_key(invoice_id, branch_id)" in seed_invoice
    assert "left(%s::text,8)" not in seed_invoice
    assert "left(invoice_id::text" not in seed_invoice
    assert "P4D2-INV-{invoice_key}" in seed_invoice
    assert "P4D2-BR-{invoice_key}" in seed_invoice
    assert "_record_refund_obligation_binding(invoice_id, subscription_id)" in seed_invoice
    assert "app_secure.record_refund_obligation_binding" in runtime
    assert "member_subscriptions_v2" in seed_invoice
    assert "subscription_terms" not in runtime


def test_p4d2_runtime_fixture_uses_app_runtime_for_force_rls_tenant_domain_rows() -> None:
    runtime = _source(ROOT / "tests" / "test_p4d_refund_obligation_resolution_runtime.py")
    seed_invoice = runtime.split("def _seed_invoice", 1)[1].split("def _allocate", 1)[0]
    cleanup = runtime.split("def _cleanup_p4d2_rows", 1)[1].split("def _ensure_p4d2_base_state", 1)[0]
    base_state = runtime.split("def _ensure_p4d2_base_state", 1)[1].split("@pytest.fixture", 1)[0]
    fresh_state = runtime.split("def _fresh_obligation_state", 1)[1].split("def _subscription_fixture", 1)[0]
    app_block = seed_invoice.split('with _connect(_APP_LOGIN, "APP_RUNTIME_PASSWORD")', 1)[1].split('with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD")', 1)[0]
    assert "SELECT pg_catalog.set_config('app.current_org_id'" in app_block
    assert "INSERT INTO public.members(" in app_block
    assert "INSERT INTO public.membership_plans(" in app_block
    assert "INSERT INTO public.member_subscriptions_v2(" in app_block
    assert "ON CONFLICT (id) DO NOTHING" in app_block
    assert "INSERT INTO public.members(" not in seed_invoice.split('with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD")', 1)[1].split('with _connect(_APP_LOGIN, "APP_RUNTIME_PASSWORD")', 1)[0]
    assert "DELETE FROM public.member_subscriptions_v2" not in cleanup
    assert "DELETE FROM public.membership_plans" not in cleanup
    assert "DELETE FROM public.members" not in cleanup
    for protected_relation in (
        "finance.branch_accounting_profiles",
        "finance.membership_plan_tax_profiles",
        "finance.tax_codes",
        "finance.brands",
        "finance.divisions",
        "finance.gst_registrations",
        "finance.legal_entities",
    ):
        assert f"DELETE FROM {protected_relation}" not in cleanup
    assert "TRUNCATE TABLE finance.branch_accounting_profiles" not in runtime
    assert "TRUNCATE TABLE finance.membership_plan_tax_profiles" not in runtime
    assert "TRUNCATE TABLE finance.tax_codes" not in runtime
    assert "_ensure_p4d2_base_state()" in fresh_state
    assert "_reset_state()" not in runtime
    assert "_validate_safe_p4d_database()" in base_state
    assert "INSERT INTO public.organizations(" in base_state
    assert "INSERT INTO finance.legal_entities" in base_state
    assert "INSERT INTO finance.payments(" in base_state
    assert "INSERT INTO public.org_branches(" in base_state
    assert base_state.count("ON CONFLICT (id) DO NOTHING") >= 4
    assert "ON CONFLICT (id) DO UPDATE" not in base_state
    assert "assert cur.fetchall() ==" in base_state
    assert "P4D Runtime Org A" in base_state
    assert "runtime_payment_a" in base_state
    assert "P4D Runtime Branch A" in base_state
    assert cleanup.index("TRUNCATE TABLE finance.refund_execution_commands") < cleanup.index("DELETE FROM finance.refunds")
    assert cleanup.index("public.notification_delivery_attempts") < cleanup.index("public.notification_commands")
    assert cleanup.index("public.notification_operator_actions") < cleanup.index("public.notification_commands")
    assert cleanup.index("public.notification_provider_events") < cleanup.index("public.notification_commands")
    assert cleanup.index("public.branch_search_effect_attempts") < cleanup.index("public.branch_outbox_events")
    assert cleanup.index("public.notification_commands") < cleanup.index("public.branch_outbox_events")
    assert cleanup.index("TRUNCATE TABLE finance.member_subscription_checkout_bindings") < cleanup.index("DELETE FROM finance.invoices")
    assert cleanup.index("TRUNCATE TABLE finance.refund_obligation_bindings") < cleanup.index("DELETE FROM finance.invoices")
    assert cleanup.index("DELETE FROM finance.credit_notes") < cleanup.index("DELETE FROM finance.invoices")
    assert cleanup.index("DELETE FROM finance.tax_records") < cleanup.index("DELETE FROM finance.invoices")
    assert cleanup.index("DELETE FROM finance.invoice_lines") < cleanup.index("DELETE FROM finance.invoices")
    assert cleanup.index("DELETE FROM finance.payment_allocations") < cleanup.index("DELETE FROM finance.invoices")
    assert cleanup.index("DELETE FROM finance.payment_events") < cleanup.index("DELETE FROM finance.payments")
    assert "DELETE FROM finance.refund_execution_commands" not in cleanup
    assert "DELETE FROM public.branch_outbox_events" not in cleanup
    assert "TRUNCATE TABLE finance.refund_execution_commands" in cleanup
    assert "public.notification_delivery_attempts" in cleanup
    assert "public.notification_operator_actions" in cleanup
    assert "public.notification_provider_events" in cleanup
    assert "public.branch_search_effect_attempts" in cleanup
    assert "public.notification_commands" in cleanup
    assert "public.branch_outbox_events" in cleanup
    assert "CASCADE" not in cleanup
    helper = _source(RUNTIME_TEST).split("def _security_owner_fetchone", 1)[1].split("def _security_owner_execute", 1)[0]
    assert "if params:" in helper
    assert "cur.execute(sql, params)" in helper
    assert "cur.execute(sql)" in helper
    assert "SET row_security=off" not in runtime
    assert "DISABLE ROW LEVEL SECURITY" not in runtime
    assert "BYPASSRLS" not in runtime
    assert "SET LOCAL ROLE app_security_owner" not in seed_invoice


def test_p4d2_member_billing_party_api_is_bounded_admin_capability() -> None:
    route = _source(ROOT / "app" / "routers" / "members.py")
    schema = _source(ROOT / "app" / "schemas" / "member_billing_party.py")
    migration = _source(P4D2_MIGRATION)
    function_body = migration.split("CREATE FUNCTION app_secure.upsert_member_billing_party", 1)[1].split("$$;", 1)[0]
    assert '@modern_router.put("/{member_id}/billing-party"' in route
    assert "staff: Staff = Depends(require_org_admin)" in route
    assert "staff.org_id != org_id" in route
    assert "app_secure.upsert_member_billing_party" in route
    assert "billing_address" in schema
    assert "place_of_supply_state_code" in schema
    assert "extra=\"forbid\"" in schema
    for forbidden in ("organization_id", "buyer_kind", "party_type", "gst_treatment", "gstin", "member_id"):
        assert forbidden not in schema.split("class MemberBillingPartyUpsertRequest", 1)[1].split("class MemberBillingPartyResponse", 1)[0]
    assert "pg_has_role(session_user, 'app_runtime', 'MEMBER')" in function_body
    assert "current_setting('app.current_org_id', true)" in function_body
    assert "FROM public.members m" in function_body
    assert "m.id = p_member_id" in function_body
    assert "m.org_id = v_current_org_id" in function_body
    assert "v_member.name" in function_body
    assert "'individual'" in function_body
    assert "'b2c'" in function_body
    assert "p_place_of_supply_state_code !~ '^[0-9]{2}$'" in function_body
    assert "MEMBER_BILLING_PROFILE_REQUIRED" in function_body


def test_p4d2_profile_tables_and_checkout_binding_are_source_bound_and_force_rls() -> None:
    source = _source(P4D2_MIGRATION)
    model = _source(ROOT / "app" / "finance_core" / "models" / "foundation.py")
    for table in (
        "finance.branch_accounting_profiles",
        "finance.membership_plan_tax_profiles",
        "finance.member_subscription_checkout_bindings",
    ):
        assert f"CREATE TABLE {table}" in source
        assert f"ALTER TABLE {table.split('.', 1)[1]}" not in source  # avoid accidental unqualified alter expectation
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in source
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in source
        assert f"REVOKE ALL ON TABLE {table} FROM PUBLIC" in source
    assert "fk_branch_accounting_profiles_branch_org" in source
    assert "REFERENCES public.org_branches(id, org_id) ON DELETE RESTRICT" in source
    assert "fk_membership_plan_tax_profiles_plan_org" in source
    assert "REFERENCES public.membership_plans(id, org_id) ON DELETE RESTRICT" in source
    assert "pricing_mode IN ('tax_exclusive','tax_inclusive')" in source
    assert "prevent_branch_accounting_profile_invalidity" in source
    assert "P4D branch accounting profile master data mismatch" in source
    assert "P4D branch accounting profile active window overlaps" in source
    assert "prevent_membership_plan_tax_profile_overlap" in source
    assert "P4D membership plan tax profile active window overlaps" in source
    assert "existing.effective_from < coalesce(NEW.effective_until, 'infinity'::date)" in source
    assert "NEW.effective_from < coalesce(existing.effective_until, 'infinity'::date)" in source
    assert "fk_member_subscription_checkout_bindings_subscription_org" in source
    assert "fk_member_subscription_checkout_bindings_invoice_org" in source
    assert "fk_member_subscription_checkout_bindings_intent_org" in source
    assert "UNIQUE (subscription_id)" in source
    assert "UNIQUE (invoice_id)" in source
    assert "CHECK (source_table = 'member_subscriptions_v2')" in source
    assert "FinanceBranchAccountingProfile" in model
    assert "FinanceMembershipPlanTaxProfile" in model
    assert "FinanceMemberSubscriptionCheckoutBinding" in model
    assert "configured_by TEXT NOT NULL" in source
    assert "ex_branch_accounting_profiles_active_window" in source
    assert "ex_membership_plan_tax_profiles_active_window" in source
    assert "configured_by: Mapped[str] = mapped_column(Text, nullable=False)" in model


def test_p4d2_finance_configuration_control_plane_is_bounded_and_closed() -> None:
    source = _source(P4D2_MIGRATION)
    roles = _source(ROOT / "security" / "cluster_role_bootstrap" / "roles.v1.json")
    settings = _source(ROOT / "security" / "cluster_role_bootstrap" / "role_settings.v1.json")
    memberships = _source(ROOT / "security" / "cluster_role_bootstrap" / "memberships.v1.json")
    provisioner = _source(ROOT / "scripts" / "p4d2_apply_finance_configuration.py")
    runtime_identity = json.loads((ROOT / "security" / "runtime_identity" / "runtime_bindings.v1.json").read_text(encoding="utf-8"))


    finance_config_binding = runtime_identity["bindings"]["finance_config"]
    assert finance_config_binding == {
        "environment_variable": "FINANCE_CONFIG_DATABASE_URL",
        "runtime_capability": "finance_config_runtime",
        "direct_capabilities": ["finance_config_runtime"],
        "session_settings": {
            "row_security": "on",
            "statement_timeout": "15s",
            "lock_timeout": "2s",
            "idle_in_transaction_session_timeout": "30s",
        },
    }
    assert runtime_identity["membership_options"] == {
        "admin_option": False,
        "inherit_option": True,
        "set_option": False,
    }
    assert '"finance_config_runtime"' in roles
    assert '"can_login": false' in roles
    assert '"bypass_rls": false' in roles
    assert '"create_db": false' in roles
    assert '"create_role": false' in roles
    assert '"finance_config_runtime"' in settings
    assert '"finance_config_runtime"' in memberships
    assert '"granted_role": "finance_config_runtime"' not in memberships
    assert '"member_role": "finance_config_runtime"' not in memberships

    for signature in (
        "app_secure.establish_finance_tax_code(uuid,text,text,text,text,integer)",
        "app_secure.establish_branch_accounting_profile(uuid,uuid,uuid,uuid,uuid,uuid,date,date)",
        "app_secure.establish_membership_plan_tax_profile(uuid,uuid,uuid,text,date,date)",
    ):
        assert f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC" in source
        assert f"GRANT EXECUTE ON FUNCTION {signature} TO finance_config_runtime" in source
        assert signature in source

    assert "GRANT USAGE ON SCHEMA app_secure TO finance_config_runtime" in source
    assert "GRANT INSERT ON TABLE finance.branch_accounting_profiles TO finance_config_runtime" not in source
    assert "GRANT INSERT ON TABLE finance.membership_plan_tax_profiles TO finance_config_runtime" not in source
    assert "GRANT INSERT ON TABLE finance.tax_codes TO finance_config_runtime" not in source
    assert "GRANT SELECT ON TABLE finance.tax_codes TO finance_config_runtime" not in source
    assert "GRANT UPDATE ON TABLE finance.tax_codes TO finance_config_runtime" not in source
    assert "GRANT DELETE ON TABLE finance.tax_codes TO finance_config_runtime" not in source
    assert "GRANT CREATE ON SCHEMA finance TO finance_config_runtime" not in source
    assert "ALTER TABLE finance.tax_codes ENABLE ROW LEVEL SECURITY" in source
    assert "ALTER TABLE finance.tax_codes FORCE ROW LEVEL SECURITY" in source
    assert "REVOKE ALL ON TABLE finance.tax_codes FROM PUBLIC" in source
    assert "GRANT SELECT, INSERT ON TABLE finance.tax_codes TO app_security_owner" in source
    assert "GRANT UPDATE ON TABLE finance.tax_codes TO app_security_owner" not in source
    assert "GRANT DELETE ON TABLE finance.tax_codes TO app_security_owner" not in source
    assert "CREATE POLICY p4d_tax_codes_security_owner_select ON finance.tax_codes FOR SELECT TO app_security_owner USING (true)" in source
    assert "CREATE POLICY p4d_tax_codes_security_owner_insert ON finance.tax_codes FOR INSERT TO app_security_owner WITH CHECK (status = 'active' AND code ~ '^[A-Z0-9_]+$' AND description IS NOT NULL AND pg_catalog.btrim(description) <> '' AND tax_type IN ('gst','exempt','non_gst') AND gst_rate_basis_points >= 0)" in source
    assert "TO migration_owner" not in source.split("CREATE POLICY p4d_tax_codes_security_owner_insert", 1)[1].split("CREATE POLICY p4d_branch_accounting_profiles_security_owner_select", 1)[0]
    assert "p4d_branch_accounting_profiles_security_owner_insert" in source
    assert "p4d_membership_plan_tax_profiles_security_owner_insert" in source
    assert "status = 'active' AND (effective_until IS NULL OR effective_until > effective_from)" in source
    assert "pricing_mode IN ('tax_exclusive','tax_inclusive')" in source
    assert "FROM public.membership_plans p" in source
    assert "P4D membership plan tax configuration plan not found" in source
    assert "p4d_membership_plans_security_owner_select" in source
    assert "pg_has_role(session_user, 'finance_config_runtime', 'MEMBER')" in source
    assert "SET search_path=pg_catalog,public,finance" in source
    assert "SET row_security=on" in source
    assert "pg_advisory_xact_lock" in source
    assert "EXCLUDE USING gist" in source
    assert "INSERT INTO finance." not in provisioner
    assert "UPDATE finance." not in provisioner
    assert "DELETE FROM finance." not in provisioner
    assert "app_secure.establish_finance_tax_code" in provisioner
    assert "app_secure.establish_branch_accounting_profile" in provisioner
    assert "app_secure.establish_membership_plan_tax_profile" in provisioner
    assert '_EXPECTED_LOGIN = "finance_config_deployment"' in provisioner
    assert '_CAPABILITY_ROLE = "finance_config_runtime"' in provisioner
    assert "Finance configuration URL must authenticate as the dedicated Finance configuration login" in provisioner
    assert "Finance configuration login membership closure mismatch" in provisioner
    assert "Finance configuration identity has direct protected-table privilege" in provisioner
    assert "Finance configuration login can execute unrelated app_secure function" in provisioner
    assert "def _protected_table_oids(cur)" in provisioner
    assert "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace" in provisioner
    assert "jsonb_to_recordset" in provisioner
    assert "table_oid = protected_table_oids[table]" in provisioner
    assert "has_table_privilege(%s, %s, %s)" in provisioner
    assert "(role, table_oid, privilege)" in provisioner
    assert "(role, table, privilege)" not in provisioner
    assert "Protected table inventory mismatch" in provisioner
    assert "p4d2 finance configuration failed: {exc.__class__.__name__}" in provisioner
    assert "print(raw" not in provisioner
    assert 'print(f"{_URL_ENV}' not in provisioner


def test_p4d2_source_bound_checkout_resolver_uses_only_subscription_authority() -> None:
    source = _source(P4D2_MIGRATION)
    service = _source(ROOT / "app" / "finance_core" / "services" / "member_subscription_checkout.py")
    router = _source(ROOT / "app" / "routers" / "member_subscriptions_v2.py")
    resolver_body = source.split("CREATE FUNCTION app_secure.resolve_member_subscription_checkout_inputs", 1)[1].split("$$;", 1)[0]
    assert "p_subscription_id uuid" in resolver_body
    assert "FROM public.member_subscriptions_v2 s" in resolver_body
    assert "s.id = p_subscription_id" in resolver_body
    assert "s.org_id = v_current_org_id" in resolver_body
    assert "v_source.primary_member_id" in resolver_body
    assert "bp.member_id = v_source.primary_member_id" in resolver_body
    assert "v_source.price_snapshot" in resolver_body
    assert "v_source.currency_code" in resolver_body
    assert "v_source.start_date" in resolver_body
    assert "CONFIGURATION_REQUIRED" in resolver_body
    assert "CONFIGURATION_AMBIGUOUS" in resolver_body
    assert "ORDER BY" not in resolver_body
    assert "LIMIT 1" not in resolver_body
    assert "member-subscription-checkout:{subscription_id}" in service
    assert "billing_party_id=inputs[\"billing_party_id\"]" in service
    assert "unit_price=money(inputs[\"unit_price\"])" in service
    assert "discount_amount=Decimal(\"0.00\")" in service
    assert "app_secure.record_member_subscription_checkout_binding" in service
    assert "app_secure.record_refund_obligation_binding" in service
    assert '@router.post("/{subscription_id}/checkout-session"' in router
    assert "FinanceCheckoutCreateResponse" in router


def test_p4d2_provider_order_is_after_local_commit_and_bounded_sql_attach() -> None:
    router = _source(ROOT / "app" / "routers" / "member_subscriptions_v2.py")
    service = _source(ROOT / "app" / "finance_core" / "services" / "member_subscription_checkout.py")
    migration = _source(P4D2_MIGRATION)
    attach_body = migration.split("CREATE FUNCTION app_secure.attach_member_subscription_checkout_provider_order", 1)[1].split("$$;", 1)[0]
    provider_route = router.split("async def create_subscription_checkout_session", 1)[1]
    assert provider_route.index("await db.commit()") < provider_route.index("adapter.create_checkout_intent")
    assert "RazorpayTestModeOrdersClient" in provider_route
    assert "require_finance_checkout_sandbox_enabled" in provider_route
    assert "app_secure.attach_member_subscription_checkout_provider_order" in service
    assert "UPDATE finance.payments" in attach_body
    assert "provider_order_ref LIKE 'intent_%'" in attach_body
    assert "provider_order_ref NOT LIKE 'intent_%'" in attach_body
    assert "P4D member subscription checkout provider replay conflict" in attach_body
    assert "pg_has_role(session_user, 'app_runtime', 'MEMBER')" in attach_body
    assert "GRANT EXECUTE ON FUNCTION app_secure.attach_member_subscription_checkout_provider_order(uuid,uuid,text) TO app_runtime" in migration
    assert 'op.execute("GRANT SELECT, UPDATE ON TABLE finance.payments TO app_security_owner")' not in migration
    assert 'op.execute("REVOKE SELECT, UPDATE ON TABLE finance.payments FROM app_security_owner")' not in migration
    assert '("finance.payments", "SELECT")' in migration
    assert '("finance.payments", "UPDATE")' in migration
    assert "pg_catalog.has_table_privilege(:role,'finance.payments','UPDATE')" in migration


def test_p4d2_downgrade_blocks_new_membership_billing_authority_and_restores_org_party_shape() -> None:
    source = _source(P4D2_MIGRATION)
    downgrade = source.split("def downgrade()", 1)[1]
    for expected in (
        "member_subscription_checkout_bindings",
        "branch_accounting_profiles",
        "membership_plan_tax_profiles",
        "billing_parties WHERE buyer_kind = 'member'",
        "downgrade blocked: member subscription checkout binding authority/evidence exists",
        "downgrade blocked: branch accounting profile authority/evidence exists",
        "downgrade blocked: membership plan tax profile authority/evidence exists",
        "downgrade blocked: member billing-party authority/evidence exists",
        "DROP FUNCTION IF EXISTS app_secure.upsert_member_billing_party(uuid,text,text)",
        "DROP FUNCTION IF EXISTS app_secure.resolve_member_subscription_checkout_inputs(uuid)",
        "DROP FUNCTION IF EXISTS app_secure.record_member_subscription_checkout_binding(uuid,uuid,uuid)",
        "DROP FUNCTION IF EXISTS app_secure.attach_member_subscription_checkout_provider_order(uuid,uuid,text)",
        "DROP FUNCTION IF EXISTS finance.prevent_branch_accounting_profile_invalidity()",
        "DROP FUNCTION IF EXISTS finance.prevent_membership_plan_tax_profile_overlap()",
        "DROP POLICY IF EXISTS p4d_tax_codes_security_owner_insert ON finance.tax_codes",
        "DROP POLICY IF EXISTS p4d_tax_codes_security_owner_select ON finance.tax_codes",
        "ALTER TABLE finance.tax_codes NO FORCE ROW LEVEL SECURITY",
        "ALTER TABLE finance.tax_codes DISABLE ROW LEVEL SECURITY",
        "DROP COLUMN IF EXISTS member_id",
        "DROP COLUMN IF EXISTS buyer_kind",
        "ADD CONSTRAINT uq_finance_billing_parties_organization UNIQUE (organization_id) DEFERRABLE INITIALLY DEFERRED",
        "DROP CONSTRAINT IF EXISTS uq_members_id_org RESTRICT",
        "DROP CONSTRAINT IF EXISTS uq_membership_plans_id_org RESTRICT",
        "DROP CONSTRAINT IF EXISTS uq_member_subscriptions_v2_id_org RESTRICT",
        "DROP CONSTRAINT IF EXISTS uq_finance_payments_id_org RESTRICT",
    ):
        assert expected in downgrade



def test_p4d2_acl_provenance_distinguishes_predecessor_owned_from_zd07_owned() -> None:
    source = _source(P4D2_MIGRATION)
    predecessor = source.split("def _require_predecessor", 1)[1].split("def _install_function", 1)[0]
    downgrade = source.split("def downgrade()", 1)[1]

    assert "def _require_predecessor_present_acl" in source
    assert "def _require_predecessor_absent_acl" in source
    assert "zd07 predecessor privilege drift" in source
    assert "zd07 refuses to claim preexisting ACL" in source

    for relation, privilege in (
        ("finance.payments", "SELECT"),
        ("finance.payments", "UPDATE"),
        ("finance.refunds", "SELECT"),
        ("finance.refunds", "UPDATE"),
        ("public.branch_outbox_events", "SELECT"),
    ):
        needle = f'(\"{relation}\", \"{privilege}\")'
        assert needle in predecessor
        assert f'_require_predecessor_present_acl(bind, "app_security_owner", relation, privilege)' in predecessor

    for relation, privilege in (
        ("finance.payment_allocations", "SELECT"),
        ("finance.invoices", "SELECT"),
        ("finance.refunds", "INSERT"),
        ("public.member_subscriptions_v2", "SELECT"),
        ("finance.billing_parties", "SELECT"),
        ("finance.billing_parties", "INSERT"),
        ("finance.billing_parties", "UPDATE"),
        ("finance.legal_entities", "SELECT"),
        ("finance.gst_registrations", "SELECT"),
        ("finance.divisions", "SELECT"),
        ("finance.brands", "SELECT"),
        ("finance.tax_codes", "SELECT"),
        ("public.members", "SELECT"),
        ("public.membership_plans", "SELECT"),
    ):
        assert f'(\"{relation}\", \"{privilege}\")' in predecessor
        assert f'(\"{relation}\", \"{privilege}\")' in downgrade

    assert 'GRANT SELECT, UPDATE ON TABLE finance.payments TO app_security_owner' not in source
    assert 'REVOKE SELECT, UPDATE ON TABLE finance.payments FROM app_security_owner' not in source
    assert 'REVOKE SELECT ON TABLE finance.refunds FROM app_security_owner' not in source
    assert 'REVOKE UPDATE ON TABLE finance.refunds FROM app_security_owner' not in source
    assert 'REVOKE INSERT ON TABLE finance.refunds FROM app_security_owner' in source
    assert 'migration_owner\', \'app_secure\', \'USAGE' in source or "migration_owner', 'app_secure', 'USAGE" in source


def test_p4d2_refunds_acl_matrix_is_predecessor_select_update_zd07_insert() -> None:
    source = _source(P4D2_MIGRATION)
    predecessor = source.split("def _require_predecessor", 1)[1].split("def _install_function", 1)[0]
    post_install = source.split("def _post_install_proof", 1)[1].split("def upgrade", 1)[0]
    downgrade = source.split("def downgrade()", 1)[1]

    assert '("finance.refunds", "SELECT")' in predecessor
    assert '("finance.refunds", "UPDATE")' in predecessor
    assert '("finance.refunds", "INSERT")' in predecessor
    assert "GRANT INSERT ON TABLE finance.refunds TO app_security_owner" in source
    assert "REVOKE INSERT ON TABLE finance.refunds FROM app_security_owner" in downgrade
    assert "has_table_privilege('app_security_owner','finance.refunds','INSERT')" in post_install
    assert '("finance.refunds", "SELECT")' in downgrade
    assert '("finance.refunds", "UPDATE")' in downgrade
    assert '_require_predecessor_present_acl(bind, "app_security_owner", relation, privilege)' in downgrade
    assert '_has_table_privilege(bind, "app_security_owner", "finance.refunds", "INSERT")' in downgrade


def test_p4d2_finance_invoice_organization_resolver_is_bounded_master_data_authority() -> None:
    source = _source(P4D2_MIGRATION)
    invoice_repo = _source(ROOT / "app" / "finance_core" / "repositories" / "invoices.py")
    billing_repo = _source(ROOT / "app" / "finance_core" / "repositories" / "billing_parties.py")
    billing_service = _source(ROOT / "app" / "finance_core" / "services" / "billing_parties.py")
    invoice_tests = _source(ROOT / "tests" / "finance_core" / "test_phase5c_invoice_engine.py")
    billing_tests = _source(ROOT / "tests" / "finance_core" / "test_phase6an_p2b_billing_party_creation.py")
    body = source.split("CREATE FUNCTION app_secure.resolve_finance_invoice_organization", 1)[1].split("$$;", 1)[0]
    accounting_body = source.split("CREATE FUNCTION app_secure.resolve_finance_invoice_accounting_master_data", 1)[1].split("$$;", 1)[0]
    billing_body = source.split("CREATE FUNCTION app_secure.resolve_finance_billing_party_organization", 1)[1].split("$$;", 1)[0]

    assert source.count("CREATE FUNCTION app_secure.") == 16
    assert '("resolve_finance_invoice_organization", "uuid")' in source
    assert '("resolve_finance_invoice_organization", "uuid", "finance invoice organization resolver")' in source
    assert "CREATE FUNCTION app_secure.resolve_finance_invoice_organization" in source
    assert "RETURNS TABLE(" in body
    assert "organization_id uuid" in body
    assert "is_active boolean" in body
    assert "SECURITY DEFINER" in body
    assert "SET search_path=pg_catalog,public,finance" in body
    assert "SET row_security=on" in body
    assert "pg_has_role(session_user, 'app_runtime', 'MEMBER')" in body
    assert "NULLIF(pg_catalog.current_setting('app.current_org_id', true), '')::uuid" in body
    assert "p_organization_id IS NULL OR p_organization_id IS DISTINCT FROM v_current_org_id" in body
    assert "FROM public.organizations o" in body
    assert "SELECT o.is_active" in body
    assert "RETURN QUERY SELECT p_organization_id, v_is_active" in body
    assert "SELECT o.*" not in body
    assert "o.name" not in body
    assert "o.slug" not in body
    assert "o.default_currency_code" not in body
    assert "GRANT SELECT (is_active, name, business_type, description) ON TABLE public.organizations TO app_security_owner" in source
    assert "REVOKE SELECT (is_active) ON TABLE public.organizations FROM app_security_owner" in source
    assert "REVOKE ALL ON FUNCTION app_secure.resolve_finance_invoice_organization(uuid) FROM PUBLIC" in source
    assert "GRANT EXECUTE ON FUNCTION app_secure.resolve_finance_invoice_organization(uuid) TO app_runtime" in source
    assert "GRANT SELECT ON TABLE public.organizations TO app_runtime" not in source
    assert "GRANT SELECT ON TABLE public.organizations TO finance_test_runtime" not in source
    assert "pg_catalog.has_column_privilege('app_security_owner','public.organizations','id','SELECT')" in source
    assert "pg_catalog.has_column_privilege('app_security_owner','public.organizations','is_active','SELECT')" in source
    assert "NOT pg_catalog.has_table_privilege('app_runtime','public.organizations','SELECT')" in source
    assert 'for role_name in ("app_runtime", "worker_runtime", "lifecycle_maintenance_runtime", "finance_config_runtime")' in source
    assert "OR pg_catalog.has_table_privilege(:role,'public.organizations','SELECT')" in source

    assert '("resolve_finance_billing_party_organization", "uuid")' in source
    assert '("resolve_finance_billing_party_organization", "uuid", "finance billing-party organization resolver")' in source
    assert "CREATE FUNCTION app_secure.resolve_finance_billing_party_organization" in source
    assert "synthetic_billing_party_allowed boolean" in billing_body
    assert "SECURITY DEFINER" in billing_body
    assert "SET search_path=pg_catalog,public,finance" in billing_body
    assert "SET row_security=on" in billing_body
    assert "pg_has_role(session_user, 'app_runtime', 'MEMBER')" in billing_body
    assert "NULLIF(pg_catalog.current_setting('app.current_org_id', true), '')::uuid" in billing_body
    assert "p_organization_id IS NULL OR p_organization_id IS DISTINCT FROM v_current_org_id" in billing_body
    assert "SELECT o.id, o.is_active, o.name, o.slug, o.business_type, o.description" in billing_body
    assert "RETURN QUERY SELECT" in billing_body
    assert "synthetic_billing_party_allowed" not in body
    assert "REVOKE ALL ON FUNCTION app_secure.resolve_finance_billing_party_organization(uuid) FROM PUBLIC" in source
    assert "GRANT EXECUTE ON FUNCTION app_secure.resolve_finance_billing_party_organization(uuid) TO app_runtime" in source

    assert '("resolve_finance_invoice_accounting_master_data", "uuid,uuid,uuid,uuid,uuid,uuid,date")' in source
    assert '("resolve_finance_invoice_accounting_master_data", "uuid,uuid,uuid,uuid,uuid,uuid,date", "finance invoice accounting master-data resolver")' in source
    assert "CREATE FUNCTION app_secure.resolve_finance_invoice_accounting_master_data" in source
    assert "SECURITY DEFINER" in accounting_body
    assert "SET search_path=pg_catalog,public,finance" in accounting_body
    assert "SET row_security=on" in accounting_body
    assert "pg_has_role(session_user, 'app_runtime', 'MEMBER')" in accounting_body
    assert "NULLIF(pg_catalog.current_setting('app.current_org_id', true), '')::uuid" in accounting_body
    assert "p_organization_id IS DISTINCT FROM v_current_org_id" in accounting_body
    assert "FROM finance.branch_accounting_profiles p" in accounting_body
    assert "JOIN public.org_branches br" in accounting_body
    assert "JOIN finance.legal_entities le" in accounting_body
    assert "JOIN finance.gst_registrations g" in accounting_body
    assert "JOIN finance.divisions d" in accounting_body
    assert "JOIN finance.brands b" in accounting_body
    assert "JOIN finance.billing_parties bp" in accounting_body
    assert "p.effective_from <= p_supply_date" in accounting_body
    assert "p_supply_date < p.effective_until" in accounting_body
    assert "v_profile_count > 1" in accounting_body
    assert "seller_legal_name text" in accounting_body
    assert "seller_registered_address text" in accounting_body
    assert "buyer_billing_name text" in accounting_body
    assert "buyer_address text" in accounting_body
    assert "le.*" not in accounting_body
    assert "bp.*" not in accounting_body
    assert "REVOKE ALL ON FUNCTION app_secure.resolve_finance_invoice_accounting_master_data(uuid,uuid,uuid,uuid,uuid,uuid,date) FROM PUBLIC" in source
    assert "GRANT EXECUTE ON FUNCTION app_secure.resolve_finance_invoice_accounting_master_data(uuid,uuid,uuid,uuid,uuid,uuid,date) TO app_runtime" in source
    assert "DROP FUNCTION IF EXISTS app_secure.resolve_finance_invoice_accounting_master_data(uuid,uuid,uuid,uuid,uuid,uuid,date)" in source

    assert "app_secure.resolve_finance_invoice_organization(:organization_id)" in invoice_repo
    assert "app_secure.resolve_finance_invoice_accounting_master_data(" in invoice_repo
    assert "select(Organization)" not in invoice_repo
    assert "from app.models.organization import Organization" not in invoice_repo
    assert "FinanceInvoiceOrganization" in invoice_repo
    assert "FinanceInvoiceAccountingMasterData" in invoice_repo
    assert "organization_id, is_active" in invoice_repo
    assert "select(FinanceLegalEntity)" not in invoice_repo
    assert "select(FinanceGstRegistration)" not in invoice_repo
    assert "select(FinanceDivision)" not in invoice_repo
    assert "select(FinanceBrand)" not in invoice_repo
    assert "select(FinanceBillingParty)" not in invoice_repo
    assert "app_secure.resolve_finance_billing_party_organization(:organization_id)" in billing_repo
    assert "select(Organization)" not in billing_repo
    assert "from app.models.organization import Organization" not in billing_repo
    assert "FinanceBillingPartyOrganization" in billing_repo
    assert "organization.synthetic_billing_party_allowed" in billing_service
    assert "organization.name" not in billing_service
    assert "organization.slug" not in billing_service
    assert "set_current_org_context" in invoice_tests
    assert "set_current_org_context(session, command_.actor_organization_id)" in billing_tests
    assert "pg_catalog.set_config('app.current_org_id', :organization_id, true)" in invoice_tests
    assert "INSERT INTO org_branches" in invoice_tests
    assert "app_secure.establish_branch_accounting_profile" in invoice_tests
    assert "finance_config_deployment" in invoice_tests
    assert "INSERT INTO finance.branch_accounting_profiles" not in invoice_tests

def test_p4d2_finance_idempotency_is_bounded_control_plane_authority() -> None:
    source = _source(P4D2_MIGRATION)
    invoice_repo = _source(ROOT / "app" / "finance_core" / "repositories" / "invoices.py")
    payment_repo = _source(ROOT / "app" / "finance_core" / "repositories" / "payments.py")
    billing_repo = _source(ROOT / "app" / "finance_core" / "repositories" / "billing_parties.py")
    checkout_repo = _source(ROOT / "app" / "finance_core" / "repositories" / "checkout_intents.py")
    reserve_body = source.split("CREATE FUNCTION app_secure.reserve_finance_idempotency", 1)[1].split("$$;", 1)[0]
    complete_body = source.split("CREATE FUNCTION app_secure.complete_finance_idempotency", 1)[1].split("$$;", 1)[0]
    downgrade = source.split("def downgrade()", 1)[1]

    assert '("reserve_finance_idempotency", "text,text,text,uuid,timestampwithtimezone")' in source
    assert '("complete_finance_idempotency", "uuid,text")' in source
    assert '("reserve_finance_idempotency", "text,text,text,uuid,timestampwithtimezone", "finance idempotency reservation")' in source
    assert '("complete_finance_idempotency", "uuid,text", "finance idempotency completion")' in source
    assert "CREATE FUNCTION app_secure.reserve_finance_idempotency" in source
    assert "CREATE FUNCTION app_secure.complete_finance_idempotency" in source
    assert "SECURITY DEFINER" in reserve_body
    assert "SECURITY DEFINER" in complete_body
    assert "SET search_path=pg_catalog,public,finance" in reserve_body
    assert "SET search_path=pg_catalog,public,finance" in complete_body
    assert "SET row_security=on" in reserve_body
    assert "SET row_security=on" in complete_body
    assert "pg_has_role(session_user, 'app_runtime', 'MEMBER')" in reserve_body
    assert "pg_has_role(session_user, 'app_runtime', 'MEMBER')" in complete_body
    assert "NULLIF(pg_catalog.current_setting('app.current_org_id', true), '')::uuid" in reserve_body
    assert "NULLIF(pg_catalog.current_setting('app.current_org_id', true), '')::uuid" in complete_body
    assert "p_organization_id IS NOT NULL AND p_organization_id IS DISTINCT FROM v_current_org_id" in reserve_body
    assert "organization_id, scope, idempotency_key, request_hash_sha256, status, expires_at" in reserve_body
    assert "v_current_org_id, p_scope, p_idempotency_key, p_request_hash_sha256::char(64), 'processing', p_expires_at" in reserve_body
    assert "ON CONFLICT ON CONSTRAINT uq_finance_idempotency_keys_scope_key DO NOTHING" in reserve_body
    assert "FOR UPDATE" in reserve_body
    assert "v_existing.request_hash_sha256::text IS DISTINCT FROM p_request_hash_sha256" in reserve_body
    assert "UPDATE finance.idempotency_keys key_data" in complete_body
    assert "SET status = 'succeeded', response_ref = p_response_ref" in complete_body
    assert "key_data.organization_id = v_current_org_id" in complete_body
    assert "key_data.status = 'processing'" in complete_body
    for scope in (
        "finance.invoice.create",
        "finance.invoice.issue",
        "finance.billing_party.create",
        "finance.checkout_intent.create",
        "finance.checkout_callback.record",
        "finance.provider.capture.confirm",
        "finance.payment.record",
        "finance.payment.event.record",
        "finance.payment.allocate",
        "finance.payment.apply",
        "finance.payment.reconcile_settlement",
        "finance.credit_note.create",
        "finance.refund_intent.create",
        "finance.ledger.post",
    ):
        assert scope in reserve_body
    assert "p_request_hash_sha256 !~ '^[0-9a-f]{64}$'" in reserve_body
    assert "pg_catalog.length(p_idempotency_key) > 200" in reserve_body
    assert "p_expires_at <= pg_catalog.clock_timestamp()" in reserve_body
    assert "GRANT SELECT (id, organization_id, scope, idempotency_key, request_hash_sha256, status, response_ref, created_at, expires_at) ON TABLE finance.idempotency_keys TO app_security_owner" in source
    assert "GRANT INSERT (organization_id, scope, idempotency_key, request_hash_sha256, status, expires_at) ON TABLE finance.idempotency_keys TO app_security_owner" in source
    assert "GRANT UPDATE (status, response_ref) ON TABLE finance.idempotency_keys TO app_security_owner" in source
    assert "GRANT SELECT ON TABLE finance.idempotency_keys TO app_runtime" not in source
    assert "GRANT INSERT ON TABLE finance.idempotency_keys TO app_runtime" not in source
    assert "GRANT UPDATE ON TABLE finance.idempotency_keys TO app_runtime" not in source
    assert "GRANT DELETE ON TABLE finance.idempotency_keys TO app_runtime" not in source
    assert "GRANT SELECT ON TABLE finance.idempotency_keys TO finance_test_runtime" not in source
    assert "GRANT INSERT ON TABLE finance.idempotency_keys TO finance_test_runtime" not in source
    assert "GRANT UPDATE ON TABLE finance.idempotency_keys TO finance_test_runtime" not in source
    assert "GRANT DELETE ON TABLE finance.idempotency_keys TO finance_test_runtime" not in source
    assert "GRANT USAGE ON SCHEMA finance TO app_runtime" not in source
    assert "GRANT USAGE ON SCHEMA finance TO finance_test_runtime" not in source
    assert "REVOKE ALL ON FUNCTION app_secure.reserve_finance_idempotency(text,text,text,uuid,timestamp with time zone) FROM PUBLIC" in source
    assert "GRANT EXECUTE ON FUNCTION app_secure.reserve_finance_idempotency(text,text,text,uuid,timestamp with time zone) TO app_runtime" in source
    assert "REVOKE ALL ON FUNCTION app_secure.complete_finance_idempotency(uuid,text) FROM PUBLIC" in source
    assert "GRANT EXECUTE ON FUNCTION app_secure.complete_finance_idempotency(uuid,text) TO app_runtime" in source
    assert "DROP FUNCTION IF EXISTS app_secure.reserve_finance_idempotency(text,text,text,uuid,timestamp with time zone)" in downgrade
    assert "DROP FUNCTION IF EXISTS app_secure.complete_finance_idempotency(uuid,text)" in downgrade

    for repo in (invoice_repo, payment_repo, billing_repo, checkout_repo):
        assert "app_secure.reserve_finance_idempotency(" in repo
        assert "app_secure.complete_finance_idempotency" in repo
        assert "insert(FinanceIdempotencyKey)" not in repo
        assert "select(FinanceIdempotencyKey)" not in repo
        assert "ON CONFLICT" not in repo
        assert "SET ROLE" not in repo
        assert "migration_owner" not in repo
        assert "_ADMIN_LOGIN" not in repo


def test_p4d2_finance_invoice_draft_persistence_is_bounded_atomic_authority() -> None:
    source = _source(P4D2_MIGRATION)
    invoice_repo = _source(ROOT / "app" / "finance_core" / "repositories" / "invoices.py")
    invoice_service = _source(ROOT / "app" / "finance_core" / "services" / "invoice_engine.py")
    model = _source(ROOT / "app" / "finance_core" / "models" / "foundation.py")
    body = source.split("CREATE FUNCTION app_secure.persist_finance_draft_invoice", 1)[1].split("$$;", 1)[0]
    downgrade = source.split("def downgrade()", 1)[1]

    assert '("persist_finance_draft_invoice", "uuid,uuid,uuid,uuid,uuid,uuid,uuid,date,text,text,text,text,text,text,text,text,text,text,text,text,text,text,numeric,numeric,numeric,numeric,numeric,jsonb")' in source
    assert '("persist_finance_draft_invoice", "uuid,uuid,uuid,uuid,uuid,uuid,uuid,date,text,text,text,text,text,text,text,text,text,text,text,text,text,text,numeric,numeric,numeric,numeric,numeric,jsonb", "finance draft invoice persistence")' in source
    assert '("resolve_finance_invoice_result", "uuid")' in source
    assert '("resolve_finance_invoice_result", "uuid", "finance invoice result resolver")' in source
    assert "SECURITY DEFINER" in body
    assert "SET search_path=pg_catalog,public,finance" in body
    assert "SET row_security=on" in body
    assert "pg_has_role(session_user, 'app_runtime', 'MEMBER')" in body
    assert "NULLIF(pg_catalog.current_setting('app.current_org_id', true), '')::uuid" in body
    assert "p_organization_id IS NULL OR p_organization_id IS DISTINCT FROM v_current_org_id" in body
    assert "idem.scope = 'finance.invoice.create'" in body
    assert "idem.status = 'processing'" in body
    assert "FROM finance.billing_parties bp" in body
    assert "bp.organization_id = v_current_org_id" in body
    assert "FROM finance.branch_accounting_profiles profile" in body
    assert "JOIN public.org_branches br" in body
    assert "profile.organization_id = v_current_org_id" in body
    assert "profile.effective_from <= p_supply_date" in body
    assert "p_supply_date < profile.effective_until" in body
    assert "INSERT INTO finance.invoices(" in body
    assert "'draft'" in body
    assert "official_invoice_number" not in body.split("INSERT INTO finance.invoices(", 1)[1].split(") VALUES", 1)[0]
    assert "brand_reference" not in body.split("INSERT INTO finance.invoices(", 1)[1].split(") VALUES", 1)[0]
    assert "INSERT INTO finance.invoice_lines(" in body
    assert "INSERT INTO finance.tax_records" not in body
    assert "INSERT INTO finance.outbox_events" not in body
    assert "finance.invoice_series" not in body
    assert "finance.brand_ref_series" not in body
    assert "jsonb_typeof(p_lines) <> 'array'" in body
    assert "jsonb_object_keys" in body
    assert "unknown field" in body
    assert "assertions.line_count = assertions.distinct_line_count" in body
    assert "assertions.min_line_number = 1" in body
    assert "assertions.max_line_number = assertions.line_count" in body
    assert "v_sum_subtotal IS DISTINCT FROM p_subtotal_amount" in body
    assert "v_sum_tax IS DISTINCT FROM p_total_tax_amount" in body
    assert "v_sum_grand IS DISTINCT FROM p_grand_total_amount" in body
    assert "GRANT SELECT (id, organization_id, status, official_invoice_number, brand_reference) ON TABLE finance.invoices TO app_security_owner" in source
    assert "GRANT INSERT (organization_id, billing_party_id, legal_entity_id, gst_registration_id, division_id, brand_id, financial_year, status, currency_code, seller_legal_name, seller_gstin, seller_pan, seller_registered_address, seller_state_code, buyer_billing_name, buyer_address, buyer_gstin, buyer_pan, buyer_place_of_supply_state_code, buyer_gst_treatment, gst_supply_type, subtotal_amount, discount_amount, taxable_amount, total_tax_amount, grand_total_amount, metadata_json) ON TABLE finance.invoices TO app_security_owner" in source
    assert "GRANT INSERT (invoice_id, line_number, description, hsn_sac, quantity, unit_amount, discount_amount, taxable_amount, gst_rate_basis_points, cgst_amount, sgst_amount, igst_amount, total_tax_amount, line_total_amount, pricing_mode) ON TABLE finance.invoice_lines TO app_security_owner" in source
    assert "GRANT INSERT ON TABLE finance.invoices TO app_runtime" not in source
    assert "GRANT INSERT ON TABLE finance.invoice_lines TO app_runtime" not in source
    assert "GRANT USAGE ON SCHEMA finance TO app_runtime" not in source
    assert "REVOKE ALL ON FUNCTION app_secure.persist_finance_draft_invoice(uuid,uuid,uuid,uuid,uuid,uuid,uuid,date,text,text,text,text,text,text,text,text,text,text,text,text,text,text,numeric,numeric,numeric,numeric,numeric,jsonb) FROM PUBLIC" in source
    assert "GRANT EXECUTE ON FUNCTION app_secure.persist_finance_draft_invoice(uuid,uuid,uuid,uuid,uuid,uuid,uuid,date,text,text,text,text,text,text,text,text,text,text,text,text,text,text,numeric,numeric,numeric,numeric,numeric,jsonb) TO app_runtime" in source
    assert "REVOKE ALL ON FUNCTION app_secure.resolve_finance_invoice_result(uuid) FROM PUBLIC" in source
    assert "GRANT EXECUTE ON FUNCTION app_secure.resolve_finance_invoice_result(uuid) TO app_runtime" in source
    assert "DROP FUNCTION IF EXISTS app_secure.resolve_finance_invoice_result(uuid)" in downgrade
    assert "DROP FUNCTION IF EXISTS app_secure.persist_finance_draft_invoice(uuid,uuid,uuid,uuid,uuid,uuid,uuid,date,text,text,text,text,text,text,text,text,text,text,text,text,text,text,numeric,numeric,numeric,numeric,numeric,jsonb)" in downgrade
    assert "REVOKE INSERT ON TABLE finance.invoice_lines FROM app_security_owner" in downgrade
    assert "REVOKE INSERT ON TABLE finance.invoices FROM app_security_owner" in downgrade
    assert 'UniqueConstraint("organization_id", "scope", "idempotency_key", name="uq_finance_idempotency_keys_scope_key")' in model
    assert "ADD CONSTRAINT uq_finance_idempotency_keys_scope_key UNIQUE (organization_id, scope, idempotency_key)" in source
    assert "tenant-scoped finance idempotency evidence cannot restore global uniqueness" in source
    assert "app_secure.persist_finance_draft_invoice(" in invoice_repo
    assert "app_secure.resolve_finance_invoice_result(:invoice_id)" in invoice_repo
    assert "resolve_invoice_result(uuid.UUID(idem.response_ref))" in invoice_service
    assert "idempotency_key_id=idem.id" in invoice_service
    assert "self._session.add(invoice)" not in invoice_repo.split("async def create_invoice", 1)[1].split("async def replace_invoice_lines", 1)[0]
    assert "FinanceInvoiceLine(" not in invoice_repo.split("async def create_invoice", 1)[1].split("async def replace_invoice_lines", 1)[0]
    assert "official_invoice_number" in invoice_repo


def test_p4d2_pg_infrastructure_scripts_propagate_explicit_topology_only() -> None:
    provisioner = _source(ROOT / "scripts" / "ci" / "provision_infrastructure_extensions.sh")
    verifier = _source(ROOT / "scripts" / "ci" / "verify_pg_partman_privilege_boundary.sh")
    for script in (provisioner, verifier):
        assert 'PGHOST' in script
        assert 'PGPORT' in script
        assert 'sudo -u postgres env "${_pg_topology_env[@]}" psql' in script
        assert 'sudo -E' not in script
        assert 'PGPORT was explicitly supplied but is not a valid TCP port' in script
        assert 'database argument must not be empty' in script
        assert '127.0.0.1' not in script
        assert '55447' not in script
        assert '55448' not in script
        assert '55449' not in script
        assert '5432' not in script
    assert provisioner.count('_postgres_psql "$database"') == 1
    assert verifier.count('_postgres_psql "$database"') == 1




def test_p4d2_finance_regression_harness_separates_subject_observer_and_forbids_broad_runtime_grants() -> None:
    foundation = _source(ROOT / "tests" / "finance_core" / "test_phase5b_foundation.py")
    admin_database = _source(ROOT / "tests" / "finance_core" / "admin_database.py")
    p4d_workflow = _source(ROOT / ".github" / "workflows" / "p4d-refund-authority-pg16.yml")
    finance_workflow = _source(ROOT / ".github" / "workflows" / "finance-hardening-ci.yml")
    runner_guard = _source(ROOT / "scripts" / "ci" / "verify_p4d2_config_runner_scope.py")

    assert 'ADMIN_DATABASE_URL = os.environ.get("FINANCE_CORE_TEST_ADMIN_DATABASE_URL") or os.environ.get("TEST_ADMIN_DATABASE_URL")' in foundation
    assert 'FINANCE_CORE_TEST_DATABASE_URL' in admin_database
    assert 'FINANCE_CORE_TEST_ADMIN_DATABASE_URL' in admin_database
    assert 'if runtime_url == admin_url or runtime.username == admin.username' in admin_database
    assert 'Finance Core admin cleanup must use a distinct database identity' in admin_database
    assert '"member_subscription_checkout_bindings"' in admin_database
    assert '"refund_obligation_bindings"' in admin_database
    assert '"membership_plan_tax_profiles"' in admin_database
    assert '"branch_accounting_profiles"' in admin_database
    assert 'TRUNCATE TABLE {relations} RESTART IDENTITY' in admin_database
    truncate_body = admin_database.split('TRUNCATE TABLE {relations} RESTART IDENTITY', 1)[1]
    assert ' CASCADE' not in truncate_body

    for source in (p4d_workflow, finance_workflow):
        assert "GRANT USAGE ON SCHEMA finance TO finance_test_runtime" not in source
        assert "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA finance TO finance_test_runtime" not in source
        assert "GRANT ALL" not in source
        assert "GRANT app_security_owner TO finance_test_runtime" not in source
        assert "GRANT migration_owner TO finance_test_runtime" not in source
        role_clause = source.split("CREATE ROLE finance_test_runtime", 1)[1].split(";", 1)[0]
        assert "NOBYPASSRLS" in role_clause
        assert " NOSUPERUSER" in role_clause
        assert " BYPASSRLS" not in role_clause
        assert "GRANT USAGE ON SCHEMA finance TO synthetic_test_runtime" not in source
        assert "GRANT test_runner TO finance_test_runtime" not in source
        assert (
            "CREATE ROLE finance_core_test_subject LOGIN PASSWORD "
            in source
        )
        subject_clause = source.split(
            "CREATE ROLE finance_core_test_subject",
            1,
        )[1].split(";", 1)[0]
        assert "NOSUPERUSER" in subject_clause
        assert "NOCREATEDB" in subject_clause
        assert "NOCREATEROLE" in subject_clause
        assert "NOINHERIT" in subject_clause
        assert "NOREPLICATION" in subject_clause
        assert "NOBYPASSRLS" in subject_clause
        assert (
            "GRANT test_runner TO finance_core_test_subject "
            "WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;"
            in source
        )
        assert (
            "GRANT app_runtime TO finance_core_test_subject "
            "WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;"
            in source
        )
        assert (
            "GRANT app_user TO finance_core_test_subject "
            "WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;"
            in source
        )
        assert (
            "FINANCE_CORE_TEST_DATABASE_URL: "
            "postgresql+asyncpg://finance_core_test_subject:"
            in source
        )
        assert (
            "TEST_DATABASE_URL: "
            "postgresql+asyncpg://finance_test_runtime:"
            in source
        )
        assert (
            "GRANT USAGE ON SCHEMA finance TO finance_core_test_subject"
            not in source
        )
        assert (
            "GRANT migration_owner TO finance_core_test_subject"
            not in source
        )
        assert (
            "GRANT app_security_owner TO finance_core_test_subject"
            not in source
        )

    assert "GRANT USAGE ON SCHEMA finance TO finance_test_runtime" in runner_guard
    assert "GRANT SELECT ON ALL TABLES" in runner_guard
    assert "GRANT ALL" in runner_guard
    assert "GRANT app_security_owner TO finance_test_runtime" in runner_guard
    assert "GRANT migration_owner TO finance_test_runtime" in runner_guard

def test_p4d2_pg_partman_provisioner_keeps_bounded_privilege_closure() -> None:
    provisioner = _source(ROOT / "scripts" / "ci" / "provision_infrastructure_extensions.sh")
    verifier = _source(ROOT / "scripts" / "ci" / "verify_pg_partman_privilege_boundary.sh")
    for script in (provisioner, verifier):
        assert 'GRANT ALL' not in script
        assert 'GRANT EXECUTE ON ALL FUNCTIONS' not in script
        assert 'GRANT EXECUTE ON ALL PROCEDURES' not in script
        assert 'GRANT USAGE ON SCHEMA partman TO migration_owner' in script or 'has_schema_privilege' in script
    assert 'REVOKE CREATE ON SCHEMA partman FROM migration_owner' in provisioner
    assert 'REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA partman FROM migration_owner' in provisioner
    assert 'REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA partman FROM PUBLIC' in provisioner
    assert 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE partman.part_config TO migration_owner' in provisioner
    assert 'GRANT SELECT ON TABLE partman.part_config_sub TO migration_owner' in provisioner
    assert verifier.count('allowed_signatures text[] := ARRAY[') == 1
    assert 'unexpected migration_owner routine EXECUTE' in verifier
    assert 'unexpected migration_owner sequence privilege' in verifier
def test_p4d2_invoice_issue_uses_bounded_atomic_database_authority():
    root = Path(__file__).resolve().parents[1]

    migration = (
        root
        / "alembic"
        / "versions"
        / "zd07d8e9f0a3e_p4d_refund_obligation_resolution.py"
    ).read_text()

    engine = (
        root
        / "app"
        / "finance_core"
        / "services"
        / "invoice_engine.py"
    ).read_text()

    repository = (
        root
        / "app"
        / "finance_core"
        / "repositories"
        / "invoices.py"
    ).read_text()

    assert (
        "CREATE OR REPLACE FUNCTION"
        in migration
    )

    assert (
        "app_secure.resolve_finance_invoice_issue_context("
        in migration
    )

    assert (
        "app_secure.issue_finance_invoice("
        in migration
    )

    assert (
        "FOR UPDATE"
        in migration
    )

    assert (
        "UPDATE finance.invoice_series"
        in migration
    )

    assert (
        "UPDATE finance.brand_ref_series"
        in migration
    )

    assert (
        "INSERT INTO finance.tax_records"
        in migration
    )

    assert (
        "UPDATE finance.invoices"
        in migration
    )

    assert (
        "INSERT INTO finance.outbox_events"
        in migration
    )

    assert (
        "finance.invoice.issued"
        in migration
    )

    assert (
        "resolve_finance_invoice_accounting_master_data"
        in migration
    )

    assert (
        "metadata_json ->> 'supply_date'"
        in migration
    )

    assert (
        "p4d2_invoice_issue_acl_delta"
        in migration
    )

    assert (
        "REVOKE ALL ON FUNCTION"
        in migration
    )

    assert (
        "app_secure.issue_finance_invoice(uuid,text)"
        in migration
    )

    assert (
        "TO app_runtime"
        in migration
    )

    assert (
        "GRANT USAGE ON SCHEMA finance TO app_runtime"
        not in migration
    )

    assert (
        "GRANT SELECT ON TABLE finance.invoices "
        "TO app_runtime"
        not in migration
    )

    assert (
        "GRANT UPDATE ON TABLE finance.invoices "
        "TO app_runtime"
        not in migration
    )

    assert (
        "GRANT INSERT ON TABLE finance.tax_records "
        "TO app_runtime"
        not in migration
    )

    assert (
        "GRANT INSERT ON TABLE finance.outbox_events "
        "TO app_runtime"
        not in migration
    )

    assert (
        "await self._repo.resolve_invoice_issue_context("
        in engine
    )

    assert (
        "await self._repo.issue_invoice_atomic("
        in engine
    )

    assert (
        "get_invoice(command.invoice_id, for_update=True)"
        not in engine
    )

    assert (
        "allocate_official_invoice_number("
        not in engine
    )

    assert (
        "allocate_brand_reference("
        not in engine
    )

    assert (
        "create_tax_records("
        not in engine
    )

    assert (
        "create_outbox_event("
        not in engine
    )

    assert (
        "FROM app_secure.resolve_finance_invoice_issue_context("
        in repository
    )

    assert (
        "FROM app_secure.issue_finance_invoice("
        in repository
    )



def test_p4d2_invoice_issue_app_secure_ddl_uses_existing_owner_context():
    root = Path(__file__).resolve().parents[1]

    migration_path = (
        root
        / "alembic"
        / "versions"
        / "zd07d8e9f0a3e_p4d_refund_obligation_resolution.py"
    )

    source = migration_path.read_text()
    tree = ast.parse(source)

    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }

    install = functions[
        "_p4d2_install_invoice_issue_authority"
    ]

    remove = functions[
        "_p4d2_remove_invoice_issue_authority"
    ]

    lines = source.splitlines()

    install_source = "\n".join(
        lines[install.lineno - 1:install.end_lineno]
    )

    remove_source = "\n".join(
        lines[remove.lineno - 1:remove.end_lineno]
    )

    assert (
        "P4D2_INVOICE_ISSUE_OWNER_CONTEXT_V3"
        in install_source
    )

    assert install_source.count(
        '_require_identity_contract(bind)'
    ) == 2

    assert install_source.count(
        'op.execute("SET LOCAL ROLE app_security_owner")'
    ) == 1

    assert install_source.count(
        'op.execute("RESET ROLE")'
    ) == 1

    install_set = install_source.index(
        'op.execute("SET LOCAL ROLE app_security_owner")'
    )

    resolve_create = install_source.index(
        "CREATE OR REPLACE FUNCTION\n"
        "                app_secure.resolve_finance_invoice_issue_context"
    )

    issue_create = install_source.index(
        "CREATE OR REPLACE FUNCTION\n"
        "                app_secure.issue_finance_invoice"
    )

    install_reset = install_source.index(
        'op.execute("RESET ROLE")'
    )

    assert (
        install_set
        < resolve_create
        < issue_create
        < install_reset
    )

    assert remove_source.count(
        '_require_identity_contract(bind)'
    ) == 2

    assert remove_source.count(
        'op.execute("SET LOCAL ROLE app_security_owner")'
    ) == 1

    assert remove_source.count(
        'op.execute("RESET ROLE")'
    ) == 1

    remove_set = remove_source.index(
        'op.execute("SET LOCAL ROLE app_security_owner")'
    )

    issue_drop = remove_source.index(
        "DROP FUNCTION IF EXISTS\n"
        "                app_secure.issue_finance_invoice"
    )

    resolve_drop = remove_source.index(
        "DROP FUNCTION IF EXISTS\n"
        "                app_secure.resolve_finance_invoice_issue_context"
    )

    remove_reset = remove_source.index(
        'op.execute("RESET ROLE")'
    )

    state_check = remove_source.index(
        "state_exists = bind.execute("
    )

    assert (
        remove_set
        < issue_drop
        < resolve_drop
        < remove_reset
        < state_check
    )

    assert (
        "GRANT USAGE ON SCHEMA app_secure TO migration_owner"
        not in source
    )

    assert (
        "GRANT CREATE ON SCHEMA app_secure TO migration_owner"
        not in source
    )



def test_p4d2_invoice_issue_does_not_schema_qualify_conditional_expressions():
    root = Path(__file__).resolve().parents[1]

    migration = (
        root
        / "alembic"
        / "versions"
        / "zd07d8e9f0a3e_p4d_refund_obligation_resolution.py"
    )

    source = migration.read_text()

    create_marker = (
        "CREATE OR REPLACE FUNCTION\n"
        "                app_secure.issue_finance_invoice("
    )

    end_marker = (
        "REVOKE ALL ON FUNCTION\n"
        "                app_secure.issue_finance_invoice(uuid,text)"
    )

    start = source.index(create_marker)
    end = source.index(end_marker, start)

    block = source[start:end]

    assert "pg_catalog.coalesce(" not in block
    assert "pg_catalog.greatest(" not in block

    assert block.count("COALESCE(") == 3
    assert block.count("GREATEST(") == 2

    assert "SET search_path = pg_catalog" in block


def test_p4d2_checkout_callback_secure_capability_contract():
    from pathlib import Path

    migration = Path(
        "alembic/versions/"
        "zd07d8e9f0a3e_p4d_refund_obligation_resolution.py"
    ).read_text()

    required = (
        "app_secure.record_finance_checkout_callback",
        "SECURITY DEFINER",
        "SET row_security=on",
        "pg_has_role",
        "session_user",
        "'app_runtime'",
        "uq_finance_payments_provider_order_ref",
        "finance.checkout_callback.record",
        "p4d2_checkout_callback_acl_delta",
        "finance.payment_events",
        "P4D checkout callback tenant conflict",
        "P4D checkout callback order unavailable",
        "P4D checkout callback payment mismatch",
        "P4D checkout callback payment conflict",
        "P4D checkout callback replay conflict",
        "P4D checkout callback payment state invalid",
        "REVOKE ALL ON FUNCTION",
        "TO app_runtime",
        "OWNER TO app_security_owner",
    )

    for token in required:
        assert token in migration

    assert (
        "GRANT SELECT ON TABLE finance.payments TO app_runtime"
        not in migration
    )
    assert (
        "GRANT UPDATE ON TABLE finance.payments TO app_runtime"
        not in migration
    )
    assert (
        "GRANT INSERT ON TABLE finance.payment_events "
        "TO app_runtime"
        not in migration
    )


def test_p4d2_checkout_callback_service_uses_bounded_secure_adapter():
    from pathlib import Path

    repository = Path(
        "app/finance_core/repositories/payments.py"
    ).read_text()

    service = Path(
        "app/finance_core/services/checkout_callbacks.py"
    ).read_text()

    assert (
        "app_secure.record_finance_checkout_callback"
        in repository
    )
    assert (
        "record_verified_checkout_callback"
        in repository
    )
    assert (
        "record_verified_checkout_callback"
        in service
    )

    method_start = service.index(
        "    async def record_verified_callback("
    )
    method_end = service.index(
        "\ndef _required_idempotency_key",
        method_start,
    )
    method = service[method_start:method_end]

    assert "self._signature_verifier.verify" in method
    assert "canonical_hash(payload)" in method
    assert "record_verified_checkout_callback" in method

    assert "get_payment_by_provider_order_ref" not in method
    assert "get_payment_by_provider_ref" not in method
    assert "create_payment_event" not in method
    assert "update_payment_status" not in method
    assert "create_outbox_event" not in method


def test_p4d2_checkout_callback_function_ddl_has_no_sqlalchemy_literal_bind_parameters() -> None:
    from sqlalchemy import text

    source = _source(P4D2_MIGRATION)

    start = source.index(
        "CREATE FUNCTION\n"
        "                app_secure.record_finance_checkout_callback("
    )

    opening = source.index(
        "AS $function$",
        start,
    )

    closing = source.index(
        "$function$",
        opening + len("AS $function$"),
    )

    callback_ddl = source[
        start:closing + len("$function$")
    ]

    # SQLAlchemy text() recognizes :name tokens even inside SQL literals
    # and PostgreSQL dollar-quoted function bodies. Any such token in
    # this CREATE FUNCTION statement is therefore a migration defect.
    compiled = text(callback_ddl).compile()

    assert compiled.params == {}
    assert "':state'" not in callback_ddl
    assert "pg_catalog.chr(58)" in callback_ddl
    assert (
        "v_event_id || pg_catalog.chr(58) || 'state'"
        in callback_ddl
    )


def test_p4d2_checkout_callback_payment_event_replay_lookup_remains_read_only() -> None:
    source = _source(P4D2_MIGRATION)

    start = source.index(
        "CREATE FUNCTION\n"
        "                app_secure.record_finance_checkout_callback("
    )

    opening = source.index(
        "AS $function$",
        start,
    )

    closing = source.index(
        "$function$",
        opening + len("AS $function$"),
    )

    callback_ddl = source[start:closing]

    event_start = callback_ddl.index(
        "FROM finance.payment_events pe"
    )
    event_end = callback_ddl.index(
        ";",
        event_start,
    )

    event_lookup = callback_ddl[
        event_start:event_end + 1
    ]

    # payment_events is append-only callback evidence. Replay lookup is
    # read-only and must not require UPDATE merely to take a row lock.
    assert "FOR UPDATE" not in event_lookup

    # The authoritative payment row remains the serialization boundary:
    # one lock for provider-order resolution and one for payment-ref fencing.
    assert callback_ddl.count("FOR UPDATE;") == 2

    assert (
        "uq_finance_payments_provider_order_ref"
        in source
    )

    # Callback-specific payment_events authority remains SELECT + INSERT.
    callback_acl_start = source.index(
        "_P4D2_CHECKOUT_CALLBACK_ACL_STATE"
    )
    callback_acl_end = source.index(
        "def _p4d2_install_checkout_callback_authority",
        callback_acl_start,
    )
    callback_acl = source[
        callback_acl_start:callback_acl_end
    ]

    assert "'SELECT'" in callback_acl
    assert "'INSERT'" in callback_acl
    assert (
        "'payment_events', 'UPDATE'"
        not in callback_acl
    )



def test_p4d2_provider_evidence_uses_bounded_security_definer_boundary():
    repo_root = Path(__file__).resolve().parents[1]

    migration = (
        repo_root
        / "alembic"
        / "versions"
        / "zd07d8e9f0a3e_p4d_refund_obligation_resolution.py"
    ).read_text(encoding="utf-8")

    service = (
        repo_root
        / "app"
        / "finance_core"
        / "services"
        / "provider_capture_confirmation.py"
    ).read_text(encoding="utf-8")

    repository = (
        repo_root
        / "app"
        / "finance_core"
        / "repositories"
        / "payments.py"
    ).read_text(encoding="utf-8")

    assert (
        "CREATE FUNCTION\n"
        "                app_secure.confirm_finance_provider_evidence("
        in migration
    )

    provider_body = migration.split(
        "CREATE FUNCTION\n"
        "                app_secure.confirm_finance_provider_evidence(",
        1,
    )[1].split("$function$", 2)[1]

    assert "SECURITY DEFINER" in migration
    assert "pg_catalog.set_config(" in provider_body
    assert "'app.current_org_id'" in provider_body
    assert (
        "app_secure.reserve_finance_idempotency("
        in provider_body
    )
    assert (
        "app_secure.complete_finance_idempotency("
        in provider_body
    )

    event_lookup = provider_body.split(
        "FROM finance.payment_events pe",
        1,
    )[1].split(
        "IF v_existing_event.id IS NOT NULL",
        1,
    )[0]

    assert "FOR UPDATE" not in event_lookup

    assert (
        "REVOKE ALL ON FUNCTION\n"
        "                app_secure.confirm_finance_provider_evidence("
        in migration
    )
    assert "TO app_runtime" in migration

    assert (
        "DROP FUNCTION IF EXISTS\n"
        "                app_secure.confirm_finance_provider_evidence("
        in migration
    )

    tree = ast.parse(service)
    provider_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name ==
            "FinanceProviderCaptureConfirmationService"
    )
    method = next(
        node
        for node in provider_class.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "confirm_provider_evidence"
    )

    method_source = ast.get_source_segment(
        service,
        method,
    )

    assert method_source is not None
    assert (
        "confirm_provider_evidence_capability"
        in method_source
    )
    assert "_locked_matching_payment" not in service
    assert "reserve_idempotency_key(" not in method_source
    assert "create_payment_event(" not in method_source
    assert "update_payment_status(" not in method_source
    assert "create_outbox_event(" not in method_source

    assert (
        "FROM app_secure.confirm_finance_provider_evidence("
        in repository
    )


def test_p4d2_secure_finance_database_failures_are_translated_to_domain_errors():
    repo_root = Path(__file__).resolve().parents[1]

    billing_service = (
        repo_root
        / "app"
        / "finance_core"
        / "services"
        / "billing_parties.py"
    ).read_text(encoding="utf-8")

    invoice_service = (
        repo_root
        / "app"
        / "finance_core"
        / "services"
        / "invoice_engine.py"
    ).read_text(encoding="utf-8")

    assert "except IntegrityError as exc:" in billing_service
    assert (
        "P4D finance idempotency request conflict"
        in billing_service
    )
    assert (
        "BILLING_PARTY_IDEMPOTENCY_CONFLICT"
        in billing_service
    )

    assert "except DBAPIError as exc:" in invoice_service
    assert (
        "P4D finance invoice accounting master data unavailable"
        in invoice_service
    )
    assert (
        "Invoice accounting master data is "
        in invoice_service
    )
    assert (
        "incomplete or unavailable"
        in invoice_service
    )
    assert (
        "raise FinanceInvoiceValidationError("
        in invoice_service
    )


def test_p4d2_invoice_issue_secure_db_errors_never_escape_raw():
    import ast
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "finance_core"
        / "services"
        / "invoice_engine.py"
    )

    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    issue = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "issue_invoice"
    )

    region = ast.get_source_segment(source, issue)

    assert region is not None
    assert "issue_invoice_atomic" in region
    assert "except DBAPIError as exc:" in region

    assert (
        "P4D finance invoice accounting master data unavailable"
        in region
    )

    assert (
        "Invoice cannot be issued because persisted "
        "finance data failed validation"
        in region
    )

    assert (
        "raise FinanceInvoiceValidationError("
        in region
    )


def test_p4d2_synthetic_org_d10_proves_finance_isolation_without_finance_visibility():
    import ast
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[1]
        / "tests"
        / "finance_core"
        / "test_phase6an_d10_synthetic_organization_service.py"
    )

    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    target = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name
        == "test_valid_synthetic_organization_creation_is_sanitized_and_isolated"
    )

    region = ast.get_source_segment(source, target)

    assert region is not None

    assert (
        "SELECT count(*) FROM finance.billing_parties"
        not in region
    )
    assert (
        "SELECT count(*) FROM finance.invoices"
        not in region
    )
    assert (
        "SELECT count(*) FROM finance.payments"
        not in region
    )

    assert "has_schema_privilege" in region
    assert "'finance'" in region
    assert "'USAGE'" in region
    assert "pg_has_role" in region
    assert "'app_runtime'" in region
    assert '"synthetic_test_runtime"' in region


def test_p4d2_payment_application_uses_bounded_security_definer_boundary():
    import ast
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]

    migration = (
        repo_root
        / "alembic"
        / "versions"
        / "zd07d8e9f0a3e_p4d_refund_obligation_resolution.py"
    ).read_text(encoding="utf-8")

    service = (
        repo_root
        / "app"
        / "finance_core"
        / "services"
        / "payment_application_gate.py"
    ).read_text(encoding="utf-8")

    repository = (
        repo_root
        / "app"
        / "finance_core"
        / "repositories"
        / "payments.py"
    ).read_text(encoding="utf-8")

    phase6r = (
        repo_root
        / "tests"
        / "finance_core"
        / "test_phase6r_sandbox_internal_apply_route_enablement.py"
    ).read_text(encoding="utf-8")

    assert (
        "app_secure.apply_finance_confirmed_payment"
        in migration
    )
    assert "SECURITY DEFINER" in migration
    assert (
        "GRANT EXECUTE ON FUNCTION"
        in migration
    )
    assert (
        "TO app_runtime"
        in migration
    )
    assert (
        "p4d2_payment_application_acl_delta"
        in migration
    )
    assert (
        "p4d2_payment_application_evidence"
        in migration
    )
    migration_tree = ast.parse(migration)
    migration_string_constants = {
        node.value
        for node in ast.walk(migration_tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
    }

    assert (
        "zd07 downgrade blocked: payment application "
        "authority/evidence exists"
        in migration_string_constants
    )

    assert (
        "apply_confirmed_payment_capability"
        in repository
    )
    assert (
        "FROM app_secure.apply_finance_confirmed_payment"
        in repository
    )

    apply_start = service.index(
        "async def apply_confirmed_payment"
    )
    apply_end = service.index(
        "    def _validate_authority",
        apply_start,
    )
    apply_region = service[apply_start:apply_end]

    assert (
        "apply_confirmed_payment_capability"
        in apply_region
    )
    assert ".get_payment(" not in apply_region
    assert ".get_invoice(" not in apply_region
    assert "apply_payment_to_invoice(" not in apply_region
    assert "begin_nested()" in apply_region
    assert "except DBAPIError as exc:" in apply_region

    assert "str(ORG_ID)" in phase6r

    assert (
        "def downgrade() -> None:\n"
        "    bind = op.get_bind()"
        in migration
    )

    forbidden = (
        "GRANT SELECT ON TABLE finance.payments "
        "TO app_runtime",
        "GRANT INSERT ON TABLE finance.payment_allocations "
        "TO app_runtime",
        "GRANT INSERT ON TABLE finance.ledger_entries "
        "TO app_runtime",
        "GRANT INSERT ON TABLE finance.ledger_entry_lines "
        "TO app_runtime",
        "GRANT INSERT ON TABLE finance.outbox_events "
        "TO app_runtime",
        "GRANT UPDATE ON TABLE finance.invoices "
        "TO app_runtime",
    )

    for statement in forbidden:
        assert statement not in migration


def test_p4d2_payment_application_ledger_replay_does_not_require_update_or_full_row_select():
    source = _source(P4D2_MIGRATION)

    start = source.index(
        "CREATE FUNCTION\n"
        "                app_secure.apply_finance_confirmed_payment("
    )

    opening = source.index(
        "AS $function$",
        start,
    )

    closing = source.index(
        "$function$",
        opening + len("AS $function$"),
    )

    body = source[start:closing]

    assert "SELECT le.*" not in body

    lookup_start = body.index(
        "FROM finance.ledger_entries le"
    )

    lookup_end = body.index(
        ";",
        lookup_start,
    )

    replay_lookup = body[
        lookup_start:
        lookup_end + 1
    ]

    assert "FOR UPDATE" not in replay_lookup
    assert "le.id = v_ledger_entry_id" in replay_lookup
    assert (
        "le.source_type = 'payment_allocation'"
        in replay_lookup
    )
    assert (
        "le.source_id = v_allocation.id"
        in replay_lookup
    )
    assert "le.status = 'posted'" in replay_lookup

    acl_start = source.index(
        "_P4D2_PAYMENT_APPLICATION_COLUMN_PRIVILEGES"
    )

    acl_end = source.index(
        "def _p4d2_grant_payment_application_column_if_missing",
        acl_start,
    )

    acl = source[acl_start:acl_end]

    assert (
        '"ledger_entries",\n'
        '        "SELECT",'
        in acl
    )

    assert (
        '"ledger_entries",\n'
        '        "INSERT",'
        in acl
    )

    assert (
        '"ledger_entries",\n'
        '        "UPDATE",'
        not in acl
    )


def test_p4d2_payment_application_uses_payment_invoice_serialization_without_allocation_update_authority():
    source = _source(P4D2_MIGRATION)

    start = source.index(
        "CREATE FUNCTION\n"
        "                app_secure.apply_finance_confirmed_payment("
    )

    opening = source.index(
        "AS $function$",
        start,
    )

    closing = source.index(
        "$function$",
        opening + len("AS $function$"),
    )

    body = source[start:closing]

    payment_start = body.index(
        "FROM finance.payments p"
    )

    payment_end = body.index(
        ";",
        payment_start,
    )

    payment_lookup = body[
        payment_start:
        payment_end + 1
    ]

    invoice_start = body.index(
        "FROM finance.invoices i"
    )

    invoice_end = body.index(
        ";",
        invoice_start,
    )

    invoice_lookup = body[
        invoice_start:
        invoice_end + 1
    ]

    assert "FOR UPDATE" in payment_lookup
    assert "FOR UPDATE" in invoice_lookup

    allocation_queries = []

    cursor = 0

    while True:
        try:
            query_start = body.index(
                "FROM finance.payment_allocations pa",
                cursor,
            )
        except ValueError:
            break

        query_end = body.index(
            ";",
            query_start,
        )

        allocation_queries.append(
            body[
                query_start:
                query_end + 1
            ]
        )

        cursor = query_end + 1

    assert len(allocation_queries) == 4

    for query in allocation_queries:
        assert "FOR UPDATE" not in query

    acl_start = source.index(
        "_P4D2_PAYMENT_APPLICATION_COLUMN_PRIVILEGES"
    )

    acl_end = source.index(
        "def _p4d2_grant_payment_application_column_if_missing",
        acl_start,
    )

    acl = source[
        acl_start:
        acl_end
    ]

    assert """
    (
        "payment_allocations",
        "INSERT",
    """ in acl

    assert """
    (
        "payment_allocations",
        "UPDATE",
    """ not in acl


def test_p4d2_payment_application_function_ddl_has_no_sqlalchemy_literal_bind_parameters():
    from sqlalchemy import text

    source = _source(P4D2_MIGRATION)

    start = source.index(
        "CREATE FUNCTION\n"
        "                app_secure.apply_finance_confirmed_payment("
    )

    opening = source.index(
        "AS $function$",
        start,
    )

    closing = source.index(
        "$function$",
        opening + len("AS $function$"),
    )

    ddl = source[
        start:
        closing + len("$function$")
    ]

    compiled = text(ddl).compile()

    assert compiled.params == {}

    assert "':ledger'" not in ddl

    assert (
        "p_idempotency_key "
        "|| pg_catalog.chr(58) "
        "|| 'ledger'"
        in ddl
    )

    assert ddl.count(
        "pg_catalog.chr(58)"
    ) >= 2


def test_p4d2_payment_application_uses_postgresql_native_sha256_namespace():
    import re
    from pathlib import Path

    migration_source = Path(
        "alembic/versions/"
        "zd07d8e9f0a3e_p4d_refund_obligation_resolution.py"
    ).read_text()

    anchor = migration_source.index(
        "app_secure.apply_finance_confirmed_payment("
    )

    function_start = migration_source.rfind(
        "CREATE FUNCTION",
        0,
        anchor,
    )

    assert function_start >= 0

    body_start = migration_source.index(
        "AS $function$",
        anchor,
    )

    body_end = migration_source.index(
        "$function$",
        body_start + len("AS $function$"),
    )

    body = migration_source[
        function_start:
        body_end
    ]

    assert "public.digest(" not in body
    assert "public.encode(" not in body

    targets = (
        "v_apply_hash",
        "v_ledger_hash",
        "v_allocation_hash",
        "v_invoice_hash",
        "v_ledger_outbox_hash",
    )

    for variable in targets:
        assert re.search(
            rf"\b{re.escape(variable)}\s*:=\s*"
            r"pg_catalog\.encode\s*\(\s*"
            r"pg_catalog\.sha256\s*\(",
            body,
            re.DOTALL,
        ), variable


def test_p4d2_zd07_does_not_schema_qualify_sql_special_forms():
    import re
    from pathlib import Path

    migration_source = Path(
        "alembic/versions/"
        "zd07d8e9f0a3e_p4d_refund_obligation_resolution.py"
    ).read_text()

    forbidden = (
        "pg_catalog.coalesce(",
        "pg_catalog.nullif(",
        "pg_catalog.greatest(",
        "pg_catalog.least(",
    )

    for expression in forbidden:
        assert expression not in migration_source

    pattern = re.compile(
        r"CREATE\s+FUNCTION\s+"
        r"app_secure\.apply_finance_confirmed_payment\s*\(",
        re.MULTILINE,
    )

    matches = list(
        pattern.finditer(
            migration_source
        )
    )

    assert len(matches) == 1

    function_start = matches[0].start()

    body_start = migration_source.index(
        "AS $function$",
        matches[0].end(),
    )

    body_end = migration_source.index(
        "$function$",
        body_start + len("AS $function$"),
    )

    payment_body = migration_source[
        function_start:
        body_end
    ]

    assert "pg_catalog.coalesce(" not in payment_body
    assert "pg_catalog.nullif(" not in payment_body
    assert "pg_catalog.greatest(" not in payment_body
    assert "pg_catalog.least(" not in payment_body

    assert payment_body.count("COALESCE(") == 4

def test_p4d2_zd07_downgrade_preserves_predecessor_organization_select_acl() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    migration = (
        root
        / "alembic"
        / "versions"
        / "zd07d8e9f0a3e_p4d_refund_obligation_resolution.py"
    )
    source = migration.read_text()

    forward_grant = (
        'op.execute("GRANT SELECT '
        '(is_active, name, business_type, description) '
        'ON TABLE public.organizations '
        'TO app_security_owner")'
    )

    destructive_revoke = (
        'op.execute("REVOKE SELECT '
        '(is_active, name, business_type, description) '
        'ON TABLE public.organizations '
        'FROM app_security_owner")'
    )

    owned_revoke = (
        'op.execute("REVOKE SELECT (is_active) '
        'ON TABLE public.organizations '
        'FROM app_security_owner")'
    )

    assert source.count(forward_grant) == 1
    assert destructive_revoke not in source
    assert source.count(owned_revoke) == 1
