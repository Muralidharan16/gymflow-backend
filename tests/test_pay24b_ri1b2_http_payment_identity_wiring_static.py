from __future__ import annotations

import ast
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PAYMENT_API = ROOT / "app/finance_core/api/payment_boundary.py"
CHECKOUT = ROOT / "app/finance_core/services/checkout_orchestration.py"
PROVIDER_OPERATIONS = ROOT / "app/finance_core/services/provider_operations.py"
PROVIDER_BOUNDARY = ROOT / "app/finance_core/domain/provider_boundary.py"
PAYMENT_DB = ROOT / "app/core/payment_database.py"
RI1B1_MIGRATION = (
    ROOT
    / "alembic/versions/zz77d8e9f0a67_pay24b_ri1b1_claim_finish_expand.py"
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


def _top_level_function(path: Path, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
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


def _try_calling(path: str) -> ast.Try:
    route = _top_level_function(PAYMENT_API, "create_checkout_session")
    for node in ast.walk(route):
        if isinstance(node, ast.Try) and _contains_awaited_call_path(
            node.body,
            path,
        ):
            return node
    raise AssertionError(f"missing try block calling: {path}")


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


def _route_source() -> str:
    return _segment(
        PAYMENT_API,
        _top_level_function(PAYMENT_API, "create_checkout_session"),
    )


def test_route_injects_both_identities_without_rebinding_global_service() -> None:
    route_node = _top_level_function(PAYMENT_API, "create_checkout_session")
    factory_node = _top_level_function(
        PAYMENT_API,
        "get_checkout_orchestration_service",
    )
    route = _route_source()
    factory = _segment(PAYMENT_API, factory_node)

    assert _depends_on(route_node, "db") == "get_db"
    assert _depends_on(route_node, "payment_db") == "get_finance_payment_db"
    assert _depends_on(route_node, "checkout_service") == (
        "get_checkout_orchestration_service"
    )
    assert _depends_on(factory_node, "db") == "get_db"
    assert "db: AsyncSession = Depends(get_db)" in route
    assert "payment_db: AsyncSession = Depends(get_finance_payment_db)" in route
    assert "db: AsyncSession = Depends(get_db)" in factory
    assert "get_finance_payment_db" not in factory
    assert "FinanceCheckoutOrchestrationService(\n        db," in factory


def test_local_preparation_reserve_and_audit_stay_on_ordinary_db() -> None:
    route = _route_source()
    prepare = _segment(
        CHECKOUT,
        _class_method(
            CHECKOUT,
            "FinanceCheckoutOrchestrationService",
            "prepare_checkout_session",
        ),
    )
    provider_effects = _segment(
        CHECKOUT,
        _class(CHECKOUT, "FinanceCheckoutProviderEffectService"),
    )

    prepare_call = route.index("checkout_service.prepare_checkout_session(command)")
    audit = route.index("FinanceSecurityAuditService(db).record(", prepare_call)
    local_commit = route.index("await db.commit()", audit)
    payment_bind = route.index("bind_provider_effects(payment_db)", local_commit)

    assert prepare_call < audit < local_commit < payment_bind
    assert route.count("await db.commit()") == 1
    assert "self._provider_operations.reserve_checkout(" in prepare
    assert "payment_db" not in prepare
    assert "reserve_checkout(" not in provider_effects


def test_payment_effect_service_is_narrow_and_bound_to_payment_db() -> None:
    route = _route_source()
    binder = _segment(
        CHECKOUT,
        _class_method(
            CHECKOUT,
            "FinanceCheckoutOrchestrationService",
            "bind_provider_effects",
        ),
    )
    provider_effects = _segment(
        CHECKOUT,
        _class(CHECKOUT, "FinanceCheckoutProviderEffectService"),
    )

    assert "FinanceCheckoutProviderEffectService(\n            session," in binder
    assert "FinanceProviderOperationService(session)" in provider_effects
    assert "self._provider_operations.claim(" in provider_effects
    assert provider_effects.count("self._provider_operations.finish(") == 2
    assert "payment_effects = checkout_service.bind_provider_effects(payment_db)" in route
    assert "payment_effects.claim_provider_operation(" in route
    assert "payment_effects.finish_provider_success(" in route
    assert route.count("payment_effects.finish_provider_error(") >= 3
    assert "checkout_service.claim_provider_operation(" not in route
    assert "checkout_service.finish_provider_success(" not in route
    assert "checkout_service.finish_provider_error(" not in route


def test_claim_and_current_admission_share_payment_transaction_p1() -> None:
    route_node = _top_level_function(PAYMENT_API, "create_checkout_session")
    route = _route_source()
    p1_node = _try_calling("authority.request_current_provider_admission")
    p1 = _segment(PAYMENT_API, p1_node)

    claim = p1.index("payment_effects.claim_provider_operation(")
    claimed_guard = p1.index("if claim.claimed:", claim)
    request = p1.index("authority.request_current_provider_admission(", claimed_guard)
    commit = p1.index("await payment_db.commit()", request)

    assert claim < claimed_guard < request < commit
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

    replay = next(
        node
        for node in route_node.body
        if isinstance(node, ast.If) and ast.unparse(node.test) == "not claim.claimed"
    )
    assert replay.lineno > (p1_node.end_lineno or p1_node.lineno)
    for forbidden in (
        "authority.request_current_provider_admission",
        "authority.start_provider_admission",
        "checkout_service.call_provider",
    ):
        assert not _contains_awaited_call_path(replay.body, forbidden)
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
    assert "await db.rollback()" not in route


def test_start_transaction_p2_commits_before_provider_io() -> None:
    route = _route_source()
    p2_node = _try_calling("authority.start_provider_admission")
    p2 = _segment(PAYMENT_API, p2_node)

    start = p2.index("authority.start_provider_admission(")
    commit = p2.index("await payment_db.commit()", start)
    provider = route.index("checkout_service.call_provider(prepared)")

    assert start < commit
    assert route.index("await payment_db.commit()", route.index("start_provider_admission(")) < provider
    assert [
        path
        for statement in p2_node.body
        if (path := _awaited_call_path(statement)) is not None
    ] == ["authority.start_provider_admission", "payment_db.commit"]
    assert ".begin(" not in route

    provider_call = _segment(
        CHECKOUT,
        _class_method(
            CHECKOUT,
            "FinanceCheckoutOrchestrationService",
            "call_provider",
        ),
    )
    assert "_provider_adapter.create_checkout_intent(" in provider_call
    assert "_session" not in provider_call
    assert "_provider_operations" not in provider_call


def test_provider_failure_and_admission_finish_are_atomic_in_p3() -> None:
    provider_try = _try_calling("checkout_service.call_provider")
    assert len(provider_try.handlers) == 1
    handler_node = provider_try.handlers[0]
    handler = _segment(PAYMENT_API, handler_node)

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


def test_provider_success_and_admission_finish_are_atomic_in_p3() -> None:
    route_node = _top_level_function(PAYMENT_API, "create_checkout_session")
    provider_try = _try_calling("checkout_service.call_provider")
    assert [
        path
        for statement in provider_try.body
        if (path := _awaited_call_path(statement)) is not None
    ] == [
        "checkout_service.call_provider",
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
    ] == ["authority.finish_provider_admission", "payment_db.commit"]
    admission_finish = _segment(PAYMENT_API, continuation[0])
    assert 'outcome="completed"' in admission_finish

    success = "\n".join(
        _segment(PAYMENT_API, statement)
        for statement in [*provider_try.body, *continuation[:2]]
    )
    assert "payment_db.commit()" not in success[
        success.index("finish_provider_success(") : success.index(
            "finish_provider_admission("
        )
    ]


def test_scalar_handoff_avoids_cross_session_orm_attachment() -> None:
    checkout = _source(CHECKOUT)
    prepared = checkout.split("class PreparedCheckoutSession:", 1)[1].split(
        "class FinanceCheckoutProviderEffectService:", 1
    )[0]

    assert _is_frozen_dataclass(_class(CHECKOUT, "PreparedCheckoutSession"))
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


def test_payment_identity_has_no_fallback_or_role_nesting() -> None:
    payment_db = _source(PAYMENT_DB)
    validator = _top_level_function(
        PAYMENT_DB,
        "_validated_finance_payment_database_url",
    )
    ri1b1 = _source(RI1B1_MIGRATION)
    bindings = json.loads(_source(RUNTIME_BINDINGS))["bindings"]

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
    assert {
        ast.unparse(node.value) if node.value is not None else "None"
        for node in ast.walk(validator)
        if isinstance(node, ast.Return)
    } == {"None", "raw"}
    assert "raw = settings.FINANCE_PAYMENT_DATABASE_URL.strip()" in payment_db
    assert "FINANCE_PAYMENT_DATABASE_URL is required" in payment_db
    assert "or settings.DATABASE_URL" not in payment_db
    assert "settings.DATABASE_URL or" not in payment_db
    assert bindings["api"]["direct_capabilities"] == ["app_runtime", "app_user"]
    assert bindings["finance_payment"]["direct_capabilities"] == [
        "finance_payment_runtime"
    ]
    assert not re.search(
        r"GRANT\s+finance_payment_runtime\s+TO\s+app_runtime\b",
        ri1b1,
        re.IGNORECASE,
    )
    assert not re.search(
        r"GRANT\s+app_runtime\s+TO\s+finance_payment_runtime\b",
        ri1b1,
        re.IGNORECASE,
    )


def test_stage0_admission_denial_fails_closed_before_provider_io() -> None:
    route_node = _top_level_function(PAYMENT_API, "create_checkout_session")
    route = _route_source()
    p1 = _segment(
        PAYMENT_API,
        _try_calling("authority.request_current_provider_admission"),
    )
    pay24a = _source(PAY24A_MIGRATION)
    bridge = _source(PAY24B_BRIDGE)

    request = route.index("authority.request_current_provider_admission(")
    p1_commit = route.index("await payment_db.commit()", request)
    start = route.index("authority.start_provider_admission(", p1_commit)
    start_commit = route.index("await payment_db.commit()", start)
    provider = route.index("checkout_service.call_provider(prepared)", start_commit)

    assert request < p1_commit < start < start_commit < provider
    assert "except DBAPIError as exc:" in p1
    assert "await payment_db.rollback()" in p1
    assert "raise _pay24b_checkout_admission_http_error() from exc" in p1
    assert "call_provider(" not in p1

    admission_guard = next(
        node
        for node in route_node.body
        if isinstance(node, ast.If) and ast.unparse(node.test) == "admission is None"
    )
    active_guard = next(
        node
        for node in route_node.body
        if isinstance(node, ast.If)
        and ast.unparse(node.test) == "started_admission.state != 'active'"
    )
    provider_try = _try_calling("checkout_service.call_provider")
    for guard in (admission_guard, active_guard):
        assert (guard.end_lineno or guard.lineno) < provider_try.lineno
        assert any(isinstance(node, ast.Raise) for node in ast.walk(guard))
        assert not _contains_awaited_call_path(
            guard.body,
            "checkout_service.call_provider",
        )

    assert "p_capability IS DISTINCT FROM 'checkout'" in bridge
    assert "finance_payment_runtime" in bridge
    assert "pay24a_request_provider_admission(" in bridge
    assert "IF v_authority.stage<>1" in pay24a
    assert "OR v_authority.provider_egress_state<>'open'" in pay24a
    assert "IF NOT v_capability_enabled THEN" in pay24a
