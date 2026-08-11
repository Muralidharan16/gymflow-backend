from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/8293a4b5c6d7_harden_lifecycle_history_correlation.py"
SERVICE = ROOT / "app/services/branch_lifecycle_service.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _literal_assignment(path: Path, name: str):
    module = ast.parse(_source(path), filename=str(path))
    for node in module.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"missing assignment {name}")


def _function_source(path: Path, name: str) -> str:
    source = _source(path)
    module = ast.parse(source, filename=str(path))
    matches = [
        item
        for item in module.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name == name
    ]
    assert len(matches) == 1, f"expected exactly one function named {name}, found {len(matches)}"
    node = matches[0]
    assert node.end_lineno is not None
    lines = source.splitlines(keepends=True)
    return "".join(lines[node.lineno - 1 : node.end_lineno])


def _op_execute_sql(path: Path) -> list[str]:
    module = ast.parse(_source(path), filename=str(path))
    statements: list[str] = []
    for node in ast.walk(module):
        if not isinstance(node, ast.Call) or len(node.args) != 1 or node.keywords:
            continue
        if not (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "op"
            and node.func.attr == "execute"
        ):
            continue
        try:
            value = ast.literal_eval(node.args[0])
        except (ValueError, TypeError):
            continue
        if isinstance(value, str):
            statements.append(" ".join(value.split()))
    return statements


def test_correlation_hardening_is_a_single_head_successor() -> None:
    assert _literal_assignment(MIGRATION, "revision") == "8293a4b5c6d7"
    assert _literal_assignment(MIGRATION, "down_revision") == "718293a4b5c6"


def test_validator_uses_bounded_security_owner_capability() -> None:
    source = _source(MIGRATION)
    statements = _op_execute_sql(MIGRATION)

    assert "app_security_owner" in source
    assert "NOLOGIN/NOINHERIT/NOBYPASSRLS" in source
    assert (
        "GRANT SELECT (branch_id, correlation_id, emitted_at) "
        "ON TABLE public.branch_lifecycle_events TO app_security_owner"
        in statements
    )
    assert any(
        statement.startswith("CREATE POLICY lifecycle_correlation_validator_event_read ")
        and "FOR SELECT TO app_security_owner" in statement
        for statement in statements
    )
    assert "REVOKE CREATE ON SCHEMA public FROM app_security_owner" in statements
    assert "8293 leaked public CREATE to app_security_owner" in source

    # The defect must not be repaired by expanding ordinary application or
    # worker event-read privileges.
    assert "GRANT SELECT ON TABLE public.branch_lifecycle_events TO app_runtime" not in statements
    assert "GRANT SELECT ON TABLE public.branch_lifecycle_events TO worker_runtime" not in statements
    assert "BYPASSRLS" not in source.replace("NOBYPASSRLS", "")


def test_trigger_is_created_before_security_owner_transfer_without_execute_grant() -> None:
    source = _source(MIGRATION)
    upgrade = _function_source(MIGRATION, "upgrade")
    statements = _op_execute_sql(MIGRATION)

    assert upgrade.index("_create_hardened_validator()") < upgrade.index("_replace_trigger()")
    assert upgrade.index("_replace_trigger()") < upgrade.index("_install_security_owner_boundary()")
    assert (
        "ALTER FUNCTION public.validate_history_correlation_hardened() "
        "OWNER TO app_security_owner"
        in statements
    )

    # migration_owner relies only on implicit owner EXECUTE before transfer.
    assert (
        "GRANT EXECUTE ON FUNCTION "
        "public.validate_history_correlation_hardened() TO migration_owner"
        not in statements
    )
    assert (
        "GRANT EXECUTE ON FUNCTION "
        "public.validate_history_correlation_hardened() TO PUBLIC"
        not in statements
    )
    assert "8293 leaked migration-owner EXECUTE on hardened validator" in source
    assert 'for runtime_role in _RUNTIME_ROLES:' in source
    assert "8293 leaked hardened validator EXECUTE to" in source


def test_validator_is_exact_branch_correlation_and_transaction_deferred() -> None:
    source = _source(MIGRATION)
    statements = _op_execute_sql(MIGRATION)

    for predicate in (
        "event_data.correlation_id = NEW.correlation_id",
        "event_data.branch_id = NEW.branch_id",
        "event_data.emitted_at = NEW.correlation_emitted_at",
    ):
        assert predicate in source

    trigger_statements = [
        statement for statement in statements
        if statement.startswith("CREATE CONSTRAINT TRIGGER trg_validate_history_correlation ")
    ]
    assert len(trigger_statements) == 1
    trigger = trigger_statements[0]
    assert "AFTER INSERT ON public.branch_status_history" in trigger
    assert "DEFERRABLE INITIALLY DEFERRED" in trigger
    assert "EXECUTE FUNCTION public.validate_history_correlation_hardened()" in trigger

    # Downgrade restores the predecessor immediate trigger contract.
    assert any(
        statement.startswith("CREATE TRIGGER trg_validate_history_correlation ")
        and "BEFORE INSERT" in statement
        for statement in statements
    )


def test_internal_trigger_functions_are_closed_to_public_at_head() -> None:
    source = _source(MIGRATION)
    statements = _op_execute_sql(MIGRATION)

    assert (
        "REVOKE ALL ON FUNCTION "
        "public.validate_history_correlation_hardened() FROM PUBLIC"
        in statements
    )
    assert (
        "REVOKE ALL ON FUNCTION public.validate_history_correlation() FROM PUBLIC"
        in statements
    )
    assert (
        "GRANT EXECUTE ON FUNCTION public.validate_history_correlation() TO PUBLIC"
        in statements
    )
    assert "predecessor PUBLIC EXECUTE" in source


def test_downgrade_removes_only_the_revision_owned_delta() -> None:
    source = _source(MIGRATION)
    statements = _op_execute_sql(MIGRATION)

    assert (
        "DROP POLICY lifecycle_correlation_validator_event_read "
        "ON public.branch_lifecycle_events"
        in statements
    )
    assert (
        "REVOKE SELECT (branch_id, correlation_id, emitted_at) "
        "ON TABLE public.branch_lifecycle_events FROM app_security_owner"
        in statements
    )
    assert "SET LOCAL ROLE app_security_owner" in statements
    assert "DROP FUNCTION public.validate_history_correlation_hardened() RESTRICT" in statements
    assert "RESET ROLE" in statements
    assert "CASCADE" not in source


def test_transaction_a_flushes_canonical_event_before_history_is_added() -> None:
    source = _source(SERVICE)
    start = source.index("async def initiate_transition")
    end = source.index("async def _record_checkpoint", start)
    method = source[start:end]

    event_add = method.index("self.db.add(event)")
    event_flush = method.index("await self.db.flush()", event_add)
    history_add = method.index("self.db.add(history)")
    assert event_add < event_flush < history_add

    # Atomicity is preserved: no intermediate commit may split the canonical
    # event from history/outbox Transaction A.
    assert "await self.db.commit()" not in method[:history_add]
