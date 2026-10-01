from __future__ import annotations

import ast
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MEMBER_ROUTE = ROOT / "app/routers/member_subscriptions_v2.py"
MEMBER_CHECKOUT = (
    ROOT / "app/finance_core/services/member_subscription_checkout.py"
)
PROVIDER_OPERATIONS = ROOT / "app/finance_core/services/provider_operations.py"
PROVIDER_BOUNDARY = ROOT / "app/finance_core/domain/provider_boundary.py"
PAYMENT_DB = ROOT / "app/core/payment_database.py"
DATABASE = ROOT / "app/core/database.py"
RI1B3_MIGRATION = (
    ROOT
    / "alembic/versions/zz87d8e9f0a68_pay24b_ri1b3_claim_finish_contract.py"
)
PAY24A_MIGRATION = (
    ROOT / "alembic/versions/zz57d8e9f0a65_pay24a_durable_activation_authority.py"
)
PAY24B_BRIDGE = (
    ROOT / "alembic/versions/zz67d8e9f0a66_pay24b_checkout_admission_bridge.py"
)
RUNTIME_BINDINGS = ROOT / "security/runtime_identity/runtime_bindings.v1.json"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _tree(path: Path) -> ast.Module:
    return ast.parse(_source(path))


def _top_level_function(
    path: Path,
    name: str,
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in _tree(path).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == name:
                return node
    raise AssertionError(f"missing function: {name}")


def _class(path: Path, name: str) -> ast.ClassDef:
    for node in _tree(path).body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"missing class: {name}")


def _class_method(
    path: Path,
    class_name: str,
    method_name: str,
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in _class(path, class_name).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == method_name:
                return node
    raise AssertionError(f"missing method: {class_name}.{method_name}")


def _segment(path: Path, node: ast.AST) -> str:
    value = ast.get_source_segment(_source(path), node)
    assert value is not None
    return value


def _expression_path(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _expression_path(node.value)
        return f"{parent}.{node.attr}" if parent is not None else None
    return None


def _awaited_call_path(node: ast.stmt) -> str | None:
    value: ast.AST | None = None
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.Expr)):
        value = node.value
    if not isinstance(value, ast.Await) or not isinstance(value.value, ast.Call):
        return None
    return _expression_path(value.value.func)


def _contains_awaited_call_path(nodes: list[ast.stmt], path: str) -> bool:
    module = ast.Module(body=nodes, type_ignores=[])
    for node in ast.walk(module):
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            if _expression_path(node.value.func) == path:
                return True
    return False


def _parameter_default(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    parameter_name: str,
) -> ast.expr | None:
    parameters = function.args.args
    default_offset = len(parameters) - len(function.args.defaults)
    for index, parameter in enumerate(parameters):
        if parameter.arg != parameter_name:
            continue
        if index < default_offset:
            return None
        return function.args.defaults[index - default_offset]
    raise AssertionError(f"missing parameter: {parameter_name}")


def _depends_on(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    parameter_name: str,
) -> str | None:
    default = _parameter_default(function, parameter_name)
    if not isinstance(default, ast.Call):
        return None
    if _expression_path(default.func) != "Depends" or len(default.args) != 1:
        return None
    return _expression_path(default.args[0])


def _is_frozen_dataclass(node: ast.ClassDef) -> bool:
    return any(
        isinstance(decorator, ast.Call)
        and _expression_path(decorator.func) == "dataclass"
        and any(
            keyword.arg == "frozen"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in decorator.keywords
        )
        for decorator in node.decorator_list
    )


def _route_node() -> ast.AsyncFunctionDef:
    route = _top_level_function(
        MEMBER_ROUTE,
        "create_subscription_checkout_session",
    )
    assert isinstance(route, ast.AsyncFunctionDef)
    return route


def _route_source() -> str:
    return _segment(MEMBER_ROUTE, _route_node())


def _try_calling(path: str) -> ast.Try:
    for node in ast.walk(_route_node()):
        if isinstance(node, ast.Try) and _contains_awaited_call_path(
            node.body,
            path,
        ):
            return node
    raise AssertionError(f"missing route try block calling: {path}")


def _top_level_guard(test: str) -> ast.If:
    for node in _route_node().body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == test:
            return node
    raise AssertionError(f"missing route guard: {test}")


def test_member_route_injects_separate_app_and_payment_database_identities() -> None:
    route_node = _route_node()
    route = _route_source()

    assert _depends_on(route_node, "db") == "get_db"
    assert _depends_on(route_node, "payment_db") == "get_finance_payment_db"
    assert _depends_on(route_node, "staff") == "require_org_admin"
    assert "db: AsyncSession = Depends(get_db)" in route
    assert "payment_db: AsyncSession = Depends(get_finance_payment_db)" in route
    assert "SourceBoundMemberSubscriptionCheckoutService(db)" in route
    assert "SourceBoundMemberSubscriptionCheckoutService(payment_db)" not in route


def test_authenticated_tenant_context_flows_to_isolated_payment_dependency() -> None:
    route = _route_source()
    payment_db = _source(PAYMENT_DB)
    database = _source(DATABASE)
    enforce = _segment(
        MEMBER_ROUTE,
        _top_level_function(MEMBER_ROUTE, "_enforce_path_org"),
    )

    assert "_enforce_path_org(org_id, staff)" in route
    assert route.index("_enforce_path_org(org_id, staff)") < route.index(
        "prepare_local_checkout("
    )
    assert "staff.org_id != org_id" in enforce
    assert "status.HTTP_403_FORBIDDEN" in enforce
    assert "await update_session_context(\n        db," in route
    assert "update_session_context(\n        payment_db," not in route
    payment_dependency = _top_level_function(
        PAYMENT_DB,
        "get_finance_payment_db",
    )
    assert payment_dependency.args.args[0].arg == "request"
    assert ast.unparse(payment_dependency.args.args[0].annotation) == "Request"
    assert "initialize_request_session(" in payment_db
    assert "request," in payment_db
    assert "state = request.state" in database
    for field in (
        'getattr(state, "principal_id"',
        'getattr(state, "staff_id"',
        'getattr(state, "principal_type"',
        'getattr(state, "org_id"',
        'getattr(state, "role"',
    ):
        assert field in database
    assert 'timeout_profile="runtime_default"' in payment_db


def test_local_member_preparation_reserve_and_bindings_stay_on_app_db() -> None:
    route = _route_source()
    prepare = _segment(
        MEMBER_CHECKOUT,
        _class_method(
            MEMBER_CHECKOUT,
            "SourceBoundMemberSubscriptionCheckoutService",
            "prepare_local_checkout",
        ),
    )
    effects = _segment(
        MEMBER_CHECKOUT,
        _class(MEMBER_CHECKOUT, "MemberSubscriptionCheckoutProviderEffectService"),
    )

    prepare_call = route.index("service.prepare_local_checkout(")
    local_commit = route.index("await db.commit()", prepare_call)
    payment_bind = route.index("service.bind_provider_effects(payment_db)")
    payment_claim = route.index("payment_effects.claim_provider_operation(")

    assert prepare_call < local_commit < payment_bind < payment_claim
    assert route.count("await db.commit()") == 1
    assert "FinanceInvoiceEngine(session)" in _segment(
        MEMBER_CHECKOUT,
        _class_method(
            MEMBER_CHECKOUT,
            "SourceBoundMemberSubscriptionCheckoutService",
            "__init__",
        ),
    )
    assert "FinanceCheckoutIntentService(session)" in _segment(
        MEMBER_CHECKOUT,
        _class_method(
            MEMBER_CHECKOUT,
            "SourceBoundMemberSubscriptionCheckoutService",
            "__init__",
        ),
    )
    for operation in (
        "create_draft_invoice(",
        "issue_invoice(",
        "create_checkout_intent(",
        "record_refund_obligation_binding",
        "record_member_subscription_checkout_binding",
        "record_member_subscription_finance_binding",
        "reserve_checkout(",
    ):
        assert operation in prepare
    assert "payment_db" not in prepare
    assert "reserve_checkout(" not in effects
    assert "FinanceInvoiceEngine" not in effects
    assert "FinanceCheckoutIntentService" not in effects


def test_prepared_state_crossing_session_boundary_is_immutable_and_scalar() -> None:
    prepared_node = _class(
        MEMBER_CHECKOUT,
        "MemberSubscriptionCheckoutPreparation",
    )
    prepared = _segment(MEMBER_CHECKOUT, prepared_node)

    assert _is_frozen_dataclass(prepared_node)
    assert _is_frozen_dataclass(
        _class(PROVIDER_OPERATIONS, "ProviderOperationReservation")
    )
    assert _is_frozen_dataclass(
        _class(PROVIDER_OPERATIONS, "ProviderOperationClaim")
    )
    assert _is_frozen_dataclass(
        _class(PROVIDER_BOUNDARY, "ProviderCheckoutIntentRequest")
    )
    assert _is_frozen_dataclass(
        _class(PROVIDER_BOUNDARY, "ProviderCheckoutIntentResponse")
    )
    assert "FinanceInvoice" not in prepared
    for value_type in (
        "uuid.UUID",
        "Decimal",
        "str",
        "ProviderCheckoutIntentRequest",
        "ProviderOperationReservation | None",
        "bool",
    ):
        assert value_type in prepared


def test_member_provider_effect_service_is_narrow_and_payment_bound() -> None:
    route = _route_source()
    source_bound = _class(
        MEMBER_CHECKOUT,
        "SourceBoundMemberSubscriptionCheckoutService",
    )
    source_bound_methods = {
        node.name
        for node in source_bound.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    binder = _segment(
        MEMBER_CHECKOUT,
        _class_method(
            MEMBER_CHECKOUT,
            "SourceBoundMemberSubscriptionCheckoutService",
            "bind_provider_effects",
        ),
    )
    effects = _segment(
        MEMBER_CHECKOUT,
        _class(MEMBER_CHECKOUT, "MemberSubscriptionCheckoutProviderEffectService"),
    )

    assert "MemberSubscriptionCheckoutProviderEffectService(session)" in binder
    assert "FinanceProviderOperationService(session)" in effects
    assert "self._provider_operations.claim(" in effects
    assert effects.count("self._provider_operations.finish(") == 2
    assert "reserve_checkout(" not in effects
    assert "prepare_local_checkout" not in effects
    assert "attach_provider_order" not in effects
    assert "claim_provider_operation" not in source_bound_methods
    assert "finish_provider_success" not in source_bound_methods
    assert "finish_provider_error" not in source_bound_methods
    assert "payment_effects = service.bind_provider_effects(payment_db)" in route
    assert "payment_effects.claim_provider_operation(" in route
    assert "payment_effects.finish_provider_success(" in route
    assert route.count("payment_effects.finish_provider_error(") >= 2
    assert "service.claim_provider_operation(" not in route
    assert "service.finish_provider_success(" not in route
    assert "service.finish_provider_error(" not in route


def test_member_admission_binding_is_exact_pay8_operation_and_request_hash() -> None:
    binding_node = _class(
        MEMBER_CHECKOUT,
        "MemberCheckoutProviderAdmissionBinding",
    )
    method = _segment(
        MEMBER_CHECKOUT,
        _class_method(
            MEMBER_CHECKOUT,
            "SourceBoundMemberSubscriptionCheckoutService",
            "provider_admission_binding",
        ),
    )

    assert _is_frozen_dataclass(binding_node)
    assert "logical_operation_id: str" in _segment(MEMBER_CHECKOUT, binding_node)
    assert "operation_sha: str" in _segment(MEMBER_CHECKOUT, binding_node)
    prepared = _segment(
        MEMBER_CHECKOUT,
        _class(MEMBER_CHECKOUT, "MemberSubscriptionCheckoutPreparation"),
    )
    prepare = _segment(
        MEMBER_CHECKOUT,
        _class_method(
            MEMBER_CHECKOUT,
            "SourceBoundMemberSubscriptionCheckoutService",
            "prepare_local_checkout",
        ),
    )
    assert "provider_request_sha256: str | None" in prepared
    assert prepare.count("provider_checkout_request_hash(") == 1
    assert "provider_request_sha256 = provider_checkout_request_hash(" in prepare
    assert "request_hash_sha256=provider_request_sha256" in prepare
    assert "provider_request_sha256=provider_request_sha256" in prepare
    assert "prepared.provider_operation is None" in method
    assert "claim.operation_id != prepared.provider_operation.operation_id" in method
    assert "PROVIDER_OPERATION_IDENTITY_MISMATCH" in method
    assert '"member-subscription-checkout:"' in method
    assert 'f"{claim.operation_id}:{claim.lease_fence}"' in method
    assert "operation_sha=prepared.provider_request_sha256" in method
    assert "provider_checkout_request_hash(" not in method


def test_p1_claim_and_current_admission_share_one_payment_transaction() -> None:
    route = _route_source()
    p1_node = _try_calling("authority.request_current_provider_admission")
    p1 = _segment(MEMBER_ROUTE, p1_node)

    claim = p1.index("payment_effects.claim_provider_operation(")
    claimed_guard = p1.index("if claim.claimed:", claim)
    binding = p1.index("service.provider_admission_binding(", claimed_guard)
    request = p1.index("authority.request_current_provider_admission(", binding)
    commit = p1.index("await payment_db.commit()", request)

    assert claim < claimed_guard < binding < request < commit
    assert "prepared=prepared" in p1[binding:request]
    assert "claim=claim" in p1[binding:request]
    assert "payment_db.commit()" not in p1[claim:request]
    assert p1.count("await payment_db.commit()") == 1
    assert "await payment_db.rollback()" in p1
    assert "await db.commit()" not in p1
    assert "await db.rollback()" not in p1
    assert _awaited_call_path(p1_node.body[0]) == (
        "payment_effects.claim_provider_operation"
    )
    claimed_if = p1_node.body[1]
    assert isinstance(claimed_if, ast.If)
    assert ast.unparse(claimed_if.test) == "claim.claimed"
    assert _contains_awaited_call_path(
        claimed_if.body,
        "authority.request_current_provider_admission",
    )
    assert _awaited_call_path(p1_node.body[-1]) == "payment_db.commit"
    assert route.index("await db.commit()") < route.index(
        "payment_effects.claim_provider_operation("
    )


def test_replay_states_do_not_manufacture_admission_or_provider_call() -> None:
    route = _route_source()
    replay = _top_level_guard("not claim.claimed")
    replay_source = _segment(MEMBER_ROUTE, replay)

    for forbidden in (
        "authority.request_current_provider_admission",
        "authority.start_provider_admission",
        "service.call_provider",
    ):
        assert not _contains_awaited_call_path(replay.body, forbidden)
    assert "claim.status == \"succeeded\"" in replay_source
    assert "claim.provider_object_id" in replay_source
    assert "service.build_result(" in replay_source
    assert "service.operation_state_error(" in replay_source
    assert "PROVIDER_OUTCOME_UNKNOWN" in replay_source
    assert "PROVIDER_FINAL_FAILURE" in replay_source
    assert route.index("if not claim.claimed:") > route.index(
        "await payment_db.commit()", route.index("claim_provider_operation(")
    )


def test_pay24_authority_and_all_provider_mutations_use_payment_identity() -> None:
    route = _route_source()

    assert "DurableActivationAuthority(payment_db)" in route
    assert "DurableActivationAuthority(db)" not in route
    assert "authority.request_current_provider_admission(" in route
    assert "authority.start_provider_admission(" in route
    assert route.count("authority.finish_provider_admission(") == 2
    assert route.count("await db.commit()") == 1
    assert "await db.rollback()" in route
    assert route.index("await db.commit()") < route.index("payment_effects =")
    for provider_transaction in (
        _try_calling("authority.request_current_provider_admission"),
        _try_calling("authority.start_provider_admission"),
        _try_calling("service.call_provider"),
    ):
        provider_source = _segment(MEMBER_ROUTE, provider_transaction)
        assert "await db.commit()" not in provider_source
        assert "await db.rollback()" not in provider_source


def test_p2_start_commits_before_provider_io_and_requires_active_admission() -> None:
    route = _route_source()
    p2_node = _try_calling("authority.start_provider_admission")
    p2 = _segment(MEMBER_ROUTE, p2_node)

    start = p2.index("authority.start_provider_admission(")
    commit = p2.index("await payment_db.commit()", start)
    provider = route.index("service.call_provider(")

    assert start < commit
    assert route.index(
        "await payment_db.commit()",
        route.index("start_provider_admission("),
    ) < provider
    assert [
        path
        for statement in p2_node.body
        if (path := _awaited_call_path(statement)) is not None
    ] == ["authority.start_provider_admission", "payment_db.commit"]
    assert ".begin(" not in route

    admission_guard = _top_level_guard("admission is None")
    active_guard = _top_level_guard("started_admission.state != 'active'")
    provider_try = _try_calling("service.call_provider")
    for guard in (admission_guard, active_guard):
        assert (guard.end_lineno or guard.lineno) < provider_try.lineno
        assert any(isinstance(node, ast.Raise) for node in ast.walk(guard))
        assert not _contains_awaited_call_path(
            guard.body,
            "service.call_provider",
        )


def test_provider_call_is_adapter_only_with_no_database_transaction_scope() -> None:
    route = _route_source()
    provider_call = _segment(
        MEMBER_CHECKOUT,
        _class_method(
            MEMBER_CHECKOUT,
            "SourceBoundMemberSubscriptionCheckoutService",
            "call_provider",
        ),
    )
    provider_index = route.index("service.call_provider(")

    assert "provider_adapter.create_checkout_intent(" in provider_call
    assert "self._session" not in provider_call
    assert "self._provider_operations" not in provider_call
    assert route.rindex("await payment_db.commit()", 0, provider_index) < provider_index
    assert route.rindex("await db.commit()", 0, provider_index) < provider_index
    assert "async with" not in route
    assert ".begin(" not in route


def test_provider_error_and_admission_outcome_are_atomic_in_p3() -> None:
    provider_try = _try_calling("service.call_provider")
    assert len(provider_try.handlers) == 1
    handler_node = provider_try.handlers[0]
    handler = _segment(MEMBER_ROUTE, handler_node)

    operation_finish = handler.index("payment_effects.finish_provider_error(")
    admission_finish = handler.index("authority.finish_provider_admission(")
    commit = handler.index("await payment_db.commit()")

    assert operation_finish < admission_finish < commit
    assert "payment_db.commit()" not in handler[operation_finish:admission_finish]
    assert "payment_db.commit()" not in handler[admission_finish:commit]
    assert handler.count("await payment_db.commit()") == 1
    assert 'outcome=("unknown" if exc.requires_reconciliation else "completed")' in handler
    assert [
        path
        for statement in handler_node.body
        if (path := _awaited_call_path(statement)) is not None
    ] == [
        "payment_effects.finish_provider_error",
        "authority.finish_provider_admission",
        "payment_db.commit",
    ]
    assert "PROVIDER_OUTCOME_UNKNOWN" in handler
    assert "PROVIDER_RETRYABLE_FAILURE" in handler
    assert "PROVIDER_FINAL_FAILURE" in handler


def test_provider_success_and_completed_admission_are_atomic_in_p3() -> None:
    route_node = _route_node()
    provider_try = _try_calling("service.call_provider")
    assert [
        path
        for statement in provider_try.body
        if (path := _awaited_call_path(statement)) is not None
    ] == [
        "service.call_provider",
        "payment_effects.finish_provider_success",
    ]

    provider_try_index = next(
        index
        for index, statement in enumerate(route_node.body)
        if isinstance(statement, ast.Try) and statement.lineno == provider_try.lineno
    )
    continuation = route_node.body[provider_try_index + 1 :]
    assert [
        path
        for statement in continuation
        if (path := _awaited_call_path(statement)) is not None
    ][:2] == ["authority.finish_provider_admission", "payment_db.commit"]
    admission_finish = _segment(MEMBER_ROUTE, continuation[0])
    assert 'outcome="completed"' in admission_finish

    success = "\n".join(
        _segment(MEMBER_ROUTE, statement)
        for statement in [*provider_try.body, *continuation[:2]]
    )
    assert "payment_db.commit()" not in success[
        success.index("finish_provider_success(") : success.index(
            "finish_provider_admission("
        )
    ]


def test_malformed_provider_success_enters_atomic_unknown_path() -> None:
    provider_try = _try_calling("service.call_provider")
    provider_try_source = _segment(MEMBER_ROUTE, provider_try)
    success_finish = _segment(
        MEMBER_CHECKOUT,
        _class_method(
            MEMBER_CHECKOUT,
            "MemberSubscriptionCheckoutProviderEffectService",
            "finish_provider_success",
        ),
    )

    assert "payment_effects.finish_provider_success(" in provider_try_source
    assert "except FinanceProviderOperationError as exc:" in provider_try_source
    assert "response.provider_code != provider_adapter.provider_code" in success_finish
    assert "PROVIDER_CODE_MISMATCH" in success_finish
    assert "if not response.provider_order_ref" in success_finish
    assert "PROVIDER_ORDER_REQUIRED" in success_finish
    assert success_finish.count('failure_class="unknown"') == 2
    handler = _segment(MEMBER_ROUTE, provider_try.handlers[0])
    assert "payment_effects.finish_provider_error(" in handler
    assert 'outcome=("unknown" if exc.requires_reconciliation else "completed")' in handler
    assert "await payment_db.commit()" in handler


def test_provider_error_classes_preserve_pay8_terminal_semantics() -> None:
    finish_error = _segment(
        MEMBER_CHECKOUT,
        _class_method(
            MEMBER_CHECKOUT,
            "MemberSubscriptionCheckoutProviderEffectService",
            "finish_provider_error",
        ),
    )

    assert '"retryable": "failed_retryable"' in finish_error
    assert '"final": "failed_final"' in finish_error
    assert '"unknown": "unknown"' in finish_error
    assert "safe_provider_error_code(error)" in finish_error
    assert "lease_owner=lease_owner" in finish_error
    assert "lease_fence=claim.lease_fence" in finish_error


def test_admission_start_failure_terminalizes_no_effect_attempt_before_exit() -> None:
    route = _route_source()
    p2 = _segment(
        MEMBER_ROUTE,
        _try_calling("authority.start_provider_admission"),
    )
    active_guard = _segment(
        MEMBER_ROUTE,
        _top_level_guard("started_admission.state != 'active'"),
    )

    assert "except DBAPIError as exc:" in p2
    assert "await payment_db.rollback()" in p2
    assert "payment_effects.finish_provider_error(" in p2
    assert "await payment_db.commit()" in p2
    assert "PAY24_ADMISSION_START_DENIED" in p2
    assert "call_provider(" not in p2
    assert "payment_effects.finish_provider_error(" in active_guard
    assert "await payment_db.commit()" in active_guard
    assert "PAY24_ADMISSION_NOT_ACTIVE" in active_guard
    assert "call_provider(" not in active_guard
    assert route.index("start_provider_admission(") < route.index(
        "service.call_provider("
    )


def test_stage0_admission_denial_is_fail_closed_before_provider_io() -> None:
    route = _route_source()
    p1 = _segment(
        MEMBER_ROUTE,
        _try_calling("authority.request_current_provider_admission"),
    )
    pay24a = _source(PAY24A_MIGRATION)
    bridge = _source(PAY24B_BRIDGE)

    request = route.index("authority.request_current_provider_admission(")
    p1_commit = route.index("await payment_db.commit()", request)
    start = route.index("authority.start_provider_admission(", p1_commit)
    p2_commit = route.index("await payment_db.commit()", start)
    provider = route.index("service.call_provider(", p2_commit)

    assert request < p1_commit < start < p2_commit < provider
    assert "except DBAPIError as exc:" in p1
    assert "await payment_db.rollback()" in p1
    assert "raise _pay24b_member_checkout_admission_http_error() from exc" in p1
    assert "call_provider(" not in p1
    assert "FINANCE_PROVIDER_EGRESS_NOT_AUTHORIZED" in _source(MEMBER_ROUTE)
    assert "p_capability IS DISTINCT FROM 'checkout'" in bridge
    assert "finance_payment_runtime" in bridge
    assert "pay24a_request_provider_admission(" in bridge
    assert "IF v_authority.stage<>1" in pay24a
    assert "OR v_authority.provider_egress_state<>'open'" in pay24a
    assert "IF NOT v_capability_enabled THEN" in pay24a


def test_compatibility_helper_and_legacy_attach_cannot_bypass_http_protocol() -> None:
    route = _route_source()
    helper = _segment(
        MEMBER_CHECKOUT,
        _class_method(
            MEMBER_CHECKOUT,
            "SourceBoundMemberSubscriptionCheckoutService",
            "create_provider_order_after_commit",
        ),
    )
    attach = _segment(
        MEMBER_CHECKOUT,
        _class_method(
            MEMBER_CHECKOUT,
            "SourceBoundMemberSubscriptionCheckoutService",
            "attach_provider_order",
        ),
    )

    assert "compatibility helper" in helper.lower()
    assert (
        "provider_effects: MemberSubscriptionCheckoutProviderEffectService"
        in helper
    )
    assert "provider_effects.claim_provider_operation(" in helper
    assert "provider_effects.finish_provider_success(" in helper
    assert "provider_effects.finish_provider_error(" in helper
    assert "self.claim_provider_operation(" not in helper
    assert "self.finish_provider_success(" not in helper
    assert "self.finish_provider_error(" not in helper
    assert "create_provider_order_after_commit(" not in route
    assert "attach_provider_order(" not in route
    assert "attach_member_subscription_checkout_provider_order" in attach
    assert "payment_effects = service.bind_provider_effects(payment_db)" in route
    assert "DurableActivationAuthority(payment_db)" in route


def test_app_runtime_authority_is_not_restored_or_nested() -> None:
    migration = _source(RI1B3_MIGRATION)
    bindings = json.loads(_source(RUNTIME_BINDINGS))["bindings"]

    assert "finance_payment_runtime" in migration
    assert "app_runtime" in migration
    assert "claim_finance_provider_operation" in migration
    assert "finish_finance_provider_operation" in migration
    assert "REVOKE EXECUTE" in migration
    assert "GRANT EXECUTE" in migration
    assert bindings["api"]["direct_capabilities"] == [
        "app_runtime",
        "app_user",
    ]
    assert bindings["finance_payment"]["direct_capabilities"] == [
        "finance_payment_runtime"
    ]
    for source in (_source(RI1B3_MIGRATION), _source(MEMBER_ROUTE)):
        assert not re.search(
            r"GRANT\s+finance_payment_runtime\s+TO\s+app_runtime\b",
            source,
            re.IGNORECASE,
        )
        assert not re.search(
            r"GRANT\s+app_runtime\s+TO\s+finance_payment_runtime\b",
            source,
            re.IGNORECASE,
        )


def test_payment_database_has_no_ordinary_database_fallback_or_role_switch() -> None:
    payment_db = _source(PAYMENT_DB)
    validator = _top_level_function(
        PAYMENT_DB,
        "_validated_finance_payment_database_url",
    )

    raw_assignment = next(
        node
        for node in validator.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "raw"
            for target in node.targets
        )
    )
    assert ast.unparse(raw_assignment.value) == (
        "settings.FINANCE_PAYMENT_DATABASE_URL.strip()"
    )
    assert "FINANCE_PAYMENT_DATABASE_URL is required" in payment_db
    assert "or settings.DATABASE_URL" not in payment_db
    assert "settings.DATABASE_URL or" not in payment_db
    assert "SET ROLE" not in payment_db.upper()
    assert "GRANT FINANCE_PAYMENT_RUNTIME" not in payment_db.upper()


def test_member_http_composition_remains_sandbox_only_and_has_no_real_call_path() -> None:
    route = _route_source()

    assert _depends_on(_route_node(), "_sandbox_enabled") == (
        "require_finance_checkout_sandbox_enabled"
    )
    assert "RazorpayTestModeOrdersClient(" in route
    assert "RazorpaySandboxAdapter(" in route
    assert "CheckoutProviderRegistry((razorpay,))" in route
    assert "provider_adapter.create_checkout_intent(" not in route
    assert "requests." not in route
    assert "httpx." not in route
    assert "https://" not in route
    assert "live_mode" not in route.lower()
