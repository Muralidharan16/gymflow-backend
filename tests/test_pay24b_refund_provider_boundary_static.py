from __future__ import annotations

import ast
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    ROOT
    / "alembic"
    / "versions"
    / "zz97d8e9f0a69_pay24b_refund_admission_bridge.py"
)
CHECKOUT_MIGRATION = (
    ROOT
    / "alembic"
    / "versions"
    / "zz67d8e9f0a66_pay24b_checkout_admission_bridge.py"
)
AUTHORITY = ROOT / "app" / "payment_activation" / "authority.py"
WORKER = ROOT / "app" / "finance_core" / "services" / "refund_provider_worker.py"
EXECUTION = (
    ROOT / "app" / "finance_core" / "services" / "refund_provider_execution.py"
)
FINALIZATION = (
    ROOT
    / "app"
    / "finance_core"
    / "services"
    / "refund_financial_finalization.py"
)
RUNTIME_BINDINGS = ROOT / "security" / "runtime_identity" / "runtime_bindings.v1.json"
PROCESS_PROFILES = ROOT / "security" / "runtime_identity" / "process_profiles.v1.json"
PRODUCTION_IDENTITIES = ROOT / "deploy" / "docker-compose.production-identities.yml"
CELERY = ROOT / "app" / "core" / "celery_app.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _assignment(path: Path, name: str) -> object:
    tree = ast.parse(_source(path))
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and target.id == name:
                return ast.literal_eval(node.value)
    raise AssertionError(f"missing assignment {name!r} in {path}")


def _method(source: str, name: str, next_name: str) -> str:
    return source.split(f"async def {name}(", 1)[1].split(
        f"async def {next_name}(", 1
    )[0]


def _refund_bridge_sql() -> str:
    source = _source(MIGRATION)
    tail = source.split(
        "CREATE FUNCTION app_secure.pay24b_request_current_refund_admission",
        1,
    )[1]
    return (
        "CREATE FUNCTION app_secure.pay24b_request_current_refund_admission"
        + tail.split('op.execute(f"REVOKE ALL ON FUNCTION', 1)[0]
    )


def test_refund_bridge_is_linear_function_only_migration() -> None:
    assert _assignment(MIGRATION, "revision") == "zz97d8e9f0a69"
    assert _assignment(MIGRATION, "down_revision") == "zz87d8e9f0a68"
    source = _source(MIGRATION)
    for forbidden in (
        "CREATE TABLE",
        "ALTER TABLE",
        "DROP TABLE",
        "CREATE POLICY",
        "DROP POLICY",
        "GRANT SELECT",
        "GRANT INSERT",
        "GRANT UPDATE",
        "GRANT DELETE",
    ):
        assert forbidden not in source


def test_refund_bridge_is_security_definer_and_hardcodes_refund_capability() -> None:
    source = _source(MIGRATION)
    sql = _refund_bridge_sql()
    assert "CREATE FUNCTION app_secure.pay24b_request_current_refund_admission" in source
    assert "p_logical_operation_id text" in sql
    assert "p_operation_sha text" in sql
    assert "p_lease_seconds integer" in sql
    assert "p_capability" not in sql
    assert "SECURITY DEFINER" in sql
    assert "SET search_path=pg_catalog" in sql
    assert "SET row_security=on" in sql
    assert "pg_has_role(" in sql
    assert "session_user,'finance_refund_runtime','MEMBER'" in sql
    assert "requires finance_refund_runtime" in sql
    assert "pay24a_request_provider_admission(" in sql
    assert "'refund_execution'" in sql
    assert "finance_payment_runtime" not in sql


def test_refund_bridge_has_exact_refund_runtime_execute_partition() -> None:
    source = _source(MIGRATION)
    upgrade = source.split("def upgrade() -> None:", 1)[1].split(
        "def downgrade() -> None:", 1
    )[0]
    assert "REVOKE ALL ON FUNCTION {_FUNCTION} FROM PUBLIC" in upgrade
    assert "GRANT EXECUTE ON FUNCTION {_FUNCTION} TO finance_refund_runtime" in upgrade
    for role in (
        "finance_payment_runtime",
        "app_runtime",
        "worker_runtime",
        "finance_reconciliation_runtime",
    ):
        assert f"GRANT EXECUTE ON FUNCTION {{_FUNCTION}} TO {role}" not in upgrade
    assert "SET LOCAL ROLE app_security_owner" in source
    assert "RESET ROLE" in source
    assert "session_user=current_user=migration_owner" in source
    assert "PUBLIC" in source
    assert "prosecdef" in source
    assert "proconfig" in source
    assert "proacl" in source


def test_refund_bridge_does_not_weaken_checkout_bridge() -> None:
    checkout = _source(CHECKOUT_MIGRATION)
    refund = _source(MIGRATION)
    assert "pay24b_request_current_refund_admission" not in checkout
    assert "TO finance_refund_runtime" not in checkout
    assert (
        "CREATE FUNCTION app_secure.pay24b_request_current_provider_admission"
        not in refund
    )
    assert "GRANT EXECUTE ON FUNCTION {_CHECKOUT_BRIDGE}" not in refund
    assert "TO finance_payment_runtime" not in refund


def test_authority_has_dedicated_refund_current_generation_facade() -> None:
    source = _source(AUTHORITY)
    assert "_REQUEST_CURRENT_REFUND_ADMISSION_SQL" in source
    assert "pay24b_request_current_refund_admission" in source
    method = _method(
        source,
        "request_current_refund_admission",
        "start_provider_admission",
    )
    assert "ActivationCapability.REFUND_EXECUTION" in method
    assert "_REQUEST_CURRENT_REFUND_ADMISSION_SQL" in method
    assert '"logical_operation_id": logical_operation_id' in method
    assert '"operation_sha": operation_sha' in method
    assert '"lease_seconds": lease_seconds' in method
    assert '"capability"' not in method
    assert "_ACTIVATION_SNAPSHOT_SQL" not in method
    assert "expected_generation" not in method


def test_refund_worker_installs_tenant_context_on_each_payment_authority_session() -> None:
    source = _source(WORKER)
    assert "from app.core.database import update_session_context" in source
    install = _method(source, "_install_context", "_claim_one")
    assert "await self._session_context_installer(" in install
    assert "str(claim.organization_id) if claim else None" in install
    assert "worker_id=str(worker_id)" in source
    assert 'role="finance_refund_runtime"' in source
    assert source.count("await self._install_context(") >= 5


def test_r0_claim_is_durable_before_provider_resolution_and_p1() -> None:
    source = _source(WORKER)
    run = source.split("async def run_once(", 1)[1]
    claim = run.index("claim = await self._claim_one(")
    claim_fault = run.index('self._fault("after_claim_commit", claim)', claim)
    resolve = run.index("self._provider_resolver(claim.provider_code)", claim_fault)
    p1 = run.index("await self._bind_and_request_admission(", resolve)
    provider = run.index("await provider.submit_refund(", p1)
    assert claim < claim_fault < resolve < p1 < provider

    claim_method = _method(source, "_claim_one", "_bind_and_request_admission")
    assert "FinanceRefundProviderExecutionService(session)" in claim_method
    assert "service.claim(" in claim_method
    assert "await session.commit()" in claim_method


def test_pre_p1_provider_resolution_failure_does_not_finish_nonexistent_admission() -> None:
    source = _source(WORKER)
    run = source.split("async def run_once(", 1)[1]
    resolver_error = run.split("except FinanceProviderConfigError:", 1)[1].split(
        "# P1 denial", 1
    )[0]
    assert "await self._record_pay10_error_only(" in resolver_error
    assert "_record_provider_error(" not in resolver_error
    assert "admission=" not in resolver_error


def test_p1_binds_exact_pay10_request_and_requests_admission_in_one_transaction() -> None:
    source = _source(WORKER)
    p1 = _method(source, "_bind_and_request_admission", "_start_admission")
    bind = p1.index("await service.bind_request(")
    request = p1.index("await authority.request_current_refund_admission(", bind)
    commit = p1.index("await session.commit()", request)
    assert bind < request < commit
    assert p1.count("await session.commit()") == 1
    assert "self._activation_authority_factory(session)" in p1
    assert "DurableActivationAuthority" in source
    assert "capability=ActivationCapability.REFUND_EXECUTION" in p1
    assert "service.provider_admission_binding(" in p1
    assert "logical_operation_id=(" in p1
    assert "admission_binding.logical_operation_id" in p1
    assert "operation_sha=admission_binding.operation_sha" in p1
    assert "lease_seconds=PAY24B_REFUND_ADMISSION_LEASE_SECONDS" in p1


def test_p2_starts_and_commits_active_admission_before_provider_submission() -> None:
    source = _source(WORKER)
    p2 = _method(source, "_start_admission", "_record_pay10_error_only")
    start = p2.index("await authority.start_provider_admission(")
    inspect = p2.index("started_state = started.state", start)
    active = p2.index('if started_state == "active":', inspect)
    permit = p2.index("provider_execution_permitted = True", active)
    commit = p2.index("await session.commit()", start)
    assert start < inspect < active < permit < commit
    assert "execution_id=admission.admission_id" in p2
    assert p2.count("provider_execution_permitted = True") == 1
    for state_check in (
        'started_state in {"expired", "revoked"}',
        'started_state == "unknown"',
        'started_state == "completed"',
        'raise FinanceProviderConfigError(',
    ):
        assert p2.index(state_check, inspect) < commit

    run = source.split("async def run_once(", 1)[1]
    p1_call = run.index("await self._bind_and_request_admission(")
    p1_fault = run.index('self._fault("after_bind_commit", claim)', p1_call)
    p2_call = run.index("await self._start_admission(", p1_fault)
    p2_fault = run.index('self._fault("after_admission_start_commit", claim)', p2_call)
    permission_guard = run.index(
        "if not start_decision.provider_execution_permitted:",
        p2_fault,
    )
    provider = run.index("await provider.submit_refund(", permission_guard)
    assert (
        p1_call
        < p1_fault
        < p2_call
        < p2_fault
        < permission_guard
        < provider
    )


def test_p3_success_records_pay10_and_completes_pay24_atomically() -> None:
    source = _source(WORKER)
    outcome = _method(source, "_record_outcome", "run_once")
    pay10 = outcome.index("await service.record_outcome(")
    pay24 = outcome.index("await authority.finish_provider_admission(", pay10)
    commit = outcome.index("await session.commit()", pay24)
    assert pay10 < pay24 < commit
    assert outcome.count("await session.commit()") == 1
    assert 'outcome="completed"' in outcome
    assert "execution_id=admission.admission_id" in outcome
    assert "FinanceRefundFinancialFinalizationService" not in outcome


def test_p3_known_failure_and_unknown_finish_with_matching_pay24_outcome() -> None:
    source = _source(WORKER)
    error = _method(source, "_record_provider_error", "_record_outcome")
    unknown_branch = error.index("if error.requires_reconciliation:")
    pay10_unknown = error.index("await service.record_unknown(", unknown_branch)
    pay10_failure = error.index("await service.record_failure(", pay10_unknown)
    pay24 = error.index("await authority.finish_provider_admission(", pay10_failure)
    commit = error.index("await session.commit()", pay24)
    assert unknown_branch < pay10_unknown < pay10_failure < pay24 < commit
    assert error.count("await session.commit()") == 1
    assert '"unknown"' in error
    assert '"completed"' in error
    assert "permanent=not error.automatic_retry_allowed" in error


def test_malformed_provider_success_is_atomically_recorded_unknown() -> None:
    source = _source(WORKER)
    run = source.split("async def run_once(", 1)[1]
    provider = run.index("await provider.submit_refund(")
    outcome_try = run.index("await self._record_outcome(", provider)
    mismatch_handler = run.index("except FinanceProviderOperationError as error:", outcome_try)
    record_unknown = run.index("await self._record_provider_error(", mismatch_handler)
    assert provider < outcome_try < mismatch_handler < record_unknown
    assert "error.requires_reconciliation" in _method(
        source, "_record_provider_error", "_record_outcome"
    )


def test_stage0_or_non_active_admission_cannot_reach_provider_io() -> None:
    source = _source(WORKER)
    run = source.split("async def run_once(", 1)[1]
    start = run.index("await self._start_admission(")
    provider = run.index("await provider.submit_refund(", start)
    assert start < provider
    p2 = _method(source, "_start_admission", "_record_pay10_error_only")
    assert "submit_refund" not in p2
    assert p2.index("started_state = started.state") < p2.index(
        "await session.commit()"
    )
    assert "provider_execution_permitted = False" in p2
    assert (
        "if not start_decision.provider_execution_permitted:"
        in run[start:provider]
    )
    assert "return RefundProviderWorkerResult(" in run[start:provider]


def test_admission_replay_states_are_handled_before_provider_io() -> None:
    source = _source(WORKER)
    run = source.split("async def run_once(", 1)[1]
    provider = run.index("await provider.submit_refund(")
    before_provider = run[:provider]
    assert 'admission.state in {"active", "unknown"}' in before_provider
    assert "PAY24_REFUND_ADMISSION_ALREADY_STARTED" in before_provider
    assert 'admission.state == "completed"' in before_provider
    assert "PAY24_REFUND_COMPLETED_WITHOUT_PAY10_OUTCOME" in before_provider
    assert 'admission.state in {"expired", "revoked"}' in before_provider
    assert "_record_pay10_error_only(" in before_provider
    assert "_record_provider_error(" in before_provider


def test_provider_io_is_not_inside_a_worker_database_session_scope() -> None:
    source = _source(WORKER)
    run = source.split("async def run_once(", 1)[1]
    assert "async with self._session_factory() as session:" not in run
    assert "response = await provider.submit_refund(" in run
    assert run.index("await self._start_admission(") < run.index(
        "response = await provider.submit_refund("
    ) < run.index("await self._record_outcome(")


def test_attempt_identity_is_stable_on_reclaim_and_request_hash_is_exact() -> None:
    source = _source(EXECUTION)
    binding = source.split("def provider_admission_binding(", 1)[1].split(
        "async def claim(", 1
    )[0]
    result = binding.split("return RefundProviderAdmissionBinding(", 1)[1]
    assert 'f"refund:{claim.command_id}:{claim.attempt_count}"' in result
    assert "operation_sha=binding.request_sha256" in result
    assert "lease_fence" not in result
    for immutable_field in (
        "command_id",
        "refund_id",
        "payment_id",
        "organization_id",
        "provider_code",
        "provider_payment_ref",
        "amount",
        "currency_code",
        "status",
        "request_sha256",
    ):
        assert f"binding.{immutable_field}" in binding


def test_post_io_provider_response_validation_covers_malformed_success() -> None:
    source = _source(EXECUTION)
    validator = source.split("def validate_provider_response(", 1)[1].split(
        "async def claim(", 1
    )[0]
    outcome = _method(source, "record_outcome", "record_unknown")
    for binding in (
        "type(response) is not ProviderRefundResponse",
        "type(response.provider_code) is not str",
        "type(response.provider_refund_ref) is not str",
        "type(response.provider_payment_ref) is not str",
        "type(response.amount) is not Decimal",
        "type(response.currency_code) is not str",
        "type(response.receipt) is not str",
        "type(response.status) is not str",
        "response.provider_code != claim.provider_code",
        "response.provider_payment_ref != request.provider_payment_ref",
        "response.amount != request.amount",
        'response.status not in {"pending", "processed", "failed"}',
        'r"[A-Za-z0-9_-]{1,200}"',
        "response.amount.is_finite()",
        'response.amount.quantize(Decimal("0.01"))',
        'r"[A-Za-z]{3}"',
        "raise _authority_mismatch(claim)",
    ):
        assert binding in validator

    shape = validator.index("type(response.currency_code) is not str")
    upper = validator.index("response.currency_code.upper()")
    amount_type = validator.index("type(response.amount) is not Decimal")
    quantize = validator.index('response.amount.quantize(Decimal("0.01"))')
    assert shape < upper
    assert amount_type < quantize

    validate = outcome.index("self.validate_provider_response(")
    evidence_hash = outcome.index("refund_provider_evidence_hash(", validate)
    sql = outcome.index("await self._session.execute(", evidence_hash)
    assert validate < evidence_hash < sql


def test_malformed_response_handler_does_not_swallow_unrelated_errors() -> None:
    source = _source(WORKER)
    run = source.split("async def run_once(", 1)[1]
    outcome = run.index("await self._record_outcome(")
    handler = run.index(
        "except FinanceProviderOperationError as error:",
        outcome,
    )
    tail = run[handler:]
    assert "except Exception" not in tail
    assert "except BaseException" not in tail


def test_crash_fences_keep_one_attempt_identity_and_unknown_non_replay() -> None:
    source = _source(WORKER)
    execution = _source(EXECUTION)
    for fault in (
        "after_claim_commit",
        "after_bind_commit",
        "after_admission_start_commit",
        "after_provider_effect",
        "after_outcome_commit",
    ):
        assert f'"{fault}"' in source
    assert 'f"refund:{claim.command_id}:{claim.attempt_count}"' in execution
    assert "operation_sha=binding.request_sha256" in execution
    assert "execution_id=admission.admission_id" in source
    assert "error.requires_reconciliation" in source
    assert "record_unknown" in source


def test_provider_execution_remains_separate_from_financial_finalization() -> None:
    worker = _source(WORKER)
    execution = _source(EXECUTION)
    finalization = _source(FINALIZATION)
    assert "FinanceRefundFinancialFinalizationService" not in worker
    assert "finalize_pay10_refund" not in worker
    assert "ledger_entries" not in worker
    assert "outbox_events" not in worker
    assert "record_pay10_refund_provider_outcome" in execution
    assert "finalize_pay10_refund" in finalization


def test_refund_runtime_remains_peer_isolated_and_production_unbound() -> None:
    bindings = json.loads(_source(RUNTIME_BINDINGS))
    profiles = _source(PROCESS_PROFILES)
    production = _source(PRODUCTION_IDENTITIES)
    celery = _source(CELERY)
    assert "finance_refund_runtime" in bindings["reserved_unbound_capabilities"]
    assert "FINANCE_REFUND_DATABASE_URL" not in profiles
    assert "FINANCE_REFUND_DATABASE_URL" not in production
    assert "refund_provider_worker" not in celery

    serialized = json.dumps(bindings, sort_keys=True)
    assert '"runtime_capability": "finance_refund_runtime"' not in serialized
    assert "finance_refund_runtime -> finance_payment_runtime" not in serialized
    assert "finance_payment_runtime -> finance_refund_runtime" not in serialized


def test_boundary_contains_no_provider_implementation_or_live_secret() -> None:
    combined = "\n".join(
        _source(path).lower()
        for path in (MIGRATION, AUTHORITY, WORKER)
    )
    for forbidden in (
        "rzp_live_",
        "razorpay_key_secret",
        "requests.post(",
        "httpx.post(",
        "aiohttp",
        "api.razorpay.com",
    ):
        assert forbidden not in combined
