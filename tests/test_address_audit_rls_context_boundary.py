from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "tests/test_address.py"
DATABASE = ROOT / "app/core/database.py"
TEST_NAME = "test_audit_log_captured_on_update"


def _test_function() -> ast.AsyncFunctionDef:
    module = ast.parse(TARGET.read_text(encoding="utf-8"), filename=str(TARGET))
    for node in module.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == TEST_NAME:
            return node
    raise AssertionError(f"missing integration test: {TEST_NAME}")


def _string_constants(node: ast.AST) -> list[str]:
    return [
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    ]


def _runtime_block(test: ast.AsyncFunctionDef) -> ast.AsyncWith:
    for node in test.body:
        if not isinstance(node, ast.AsyncWith):
            continue
        for item in node.items:
            expression = item.context_expr
            if (
                isinstance(expression, ast.Call)
                and isinstance(expression.func, ast.Name)
                and expression.func.id == "AsyncSessionLocal"
            ):
                return node
    raise AssertionError("missing reduced-runtime AsyncSessionLocal block")


def _post_runtime_source(test: ast.AsyncFunctionDef) -> str:
    runtime = _runtime_block(test)
    runtime_index = test.body.index(runtime)
    tail = test.body[runtime_index + 1 :]
    assert tail, "missing post-runtime audit evidence phase"
    return "\n".join(ast.unparse(statement) for statement in tail)


def _context_calls(node: ast.AST) -> list[ast.Await]:
    return [
        child
        for child in ast.walk(node)
        if isinstance(child, ast.Await)
        and isinstance(child.value, ast.Call)
        and isinstance(child.value.func, ast.Name)
        and child.value.func.id == "update_session_context"
    ]


def test_address_audit_integration_uses_session_owned_typed_context():
    test = _test_function()
    source = ast.unparse(test)

    # Fixture administration, reduced runtime behavior, and the protected evidence
    # read must all use the centralized typed context API. No phase may duplicate
    # raw PostgreSQL GUC manipulation.
    assert "update_session_context" in source
    assert "set_tenant_context" not in source
    assert "pg_catalog.set_config" not in source

    assert "principal_id=str(owner_id)" in source
    assert "principal_type='legacy_gym_owner'" in source
    assert "org_id=str(org_id)" in source


def test_address_audit_fixture_seed_respects_forced_rls_and_runtime_is_reduced():
    test = _test_function()
    source = ast.unparse(test)

    assert any(arg.arg == "admin_db_session" for arg in test.args.args)
    for required_seed in (
        "admin_db_session.add(Organization",
        "admin_db_session.add(branch)",
        "admin_db_session.add(branch_state)",
        "admin_db_session.add(owner)",
    ):
        assert required_seed in source

    first_admin_commit = source.index("await admin_db_session.commit()")
    admin_context = source.index("await update_session_context(admin_db_session")
    branch_seed = source.index("admin_db_session.add(branch)")
    state_seed = source.index("admin_db_session.add(branch_state)")
    assert first_admin_commit < admin_context < branch_seed < state_seed
    assert source.count("await admin_db_session.commit()") == 2

    # Inspect the runtime scope itself rather than slicing to the end of the test:
    # the post-runtime admin evidence read is deliberately a separate trust phase.
    runtime = _runtime_block(test)
    runtime_source = ast.unparse(runtime)
    assert "admin_db_session" not in runtime_source
    assert "db.add(branch)" not in runtime_source
    assert "db.add(branch_state)" not in runtime_source
    assert "db.add(owner)" not in runtime_source
    assert "db.add(addr)" in runtime_source

    # Reduced runtime may cause audit writes indirectly through SECURITY DEFINER
    # triggers, but it must never gain direct SELECT capability on the immutable log.
    assert "branch_address_audit_log" in runtime_source
    assert "has_table_privilege" in runtime_source
    assert "'SELECT'" in runtime_source
    assert "runtime_can_read_audit is False" in runtime_source
    assert "select(AddressAuditLog)" not in runtime_source

    evidence_source = _post_runtime_source(test)
    assert "await update_session_context(admin_db_session" in evidence_source
    assert "select(AddressAuditLog)" in evidence_source
    assert "AddressAuditLog.org_id == org_id" in evidence_source
    assert "await admin_db_session.execute(stmt)" in evidence_source


def test_address_audit_context_survives_commit_without_manual_reapplication():
    test = _test_function()
    runtime = _runtime_block(test)
    runtime_source = ast.unparse(runtime)

    all_context_calls = _context_calls(test)
    runtime_context_calls = _context_calls(runtime)

    # There is exactly one explicit context attachment in the reduced runtime
    # session even though it commits twice. The Session.after_begin hook must
    # therefore restore transaction-local context after the first commit.
    assert len(runtime_context_calls) == 1
    runtime_call = runtime_context_calls[0]
    assert runtime_call.value.args
    assert ast.unparse(runtime_call.value.args[0]) == "db"
    assert runtime_source.count("await db.commit()") == 2

    # Two separate administrative context attachments are intentional: one before
    # FORCE-RLS fixture seeding and one after the runtime phase for protected audit
    # evidence verification. They do not substitute for runtime context replay.
    call_sessions = [
        ast.unparse(node.value.args[0])
        for node in all_context_calls
        if node.value.args
    ]
    assert call_sessions.count("db") == 1
    assert call_sessions.count("admin_db_session") == 2
    assert len(all_context_calls) == 3

    evidence_source = _post_runtime_source(test)
    assert "await update_session_context(admin_db_session" in evidence_source
    assert evidence_source.index("await update_session_context(admin_db_session") < evidence_source.index(
        "await admin_db_session.execute(stmt)"
    )

    database_source = DATABASE.read_text(encoding="utf-8")
    assert '@event.listens_for(Session, "after_begin")' in database_source
    assert "_apply_context_sync(connection, context)" in database_source
    assert "pg_catalog.set_config(:setting_name, :setting_value, true)" in database_source


def test_address_audit_integration_does_not_bypass_rls():
    test = _test_function()
    sql = "\n".join(_string_constants(test)).upper()

    assert "ROW_SECURITY = OFF" not in sql
    assert "DISABLE ROW LEVEL SECURITY" not in sql
    assert "SET ROLE POSTGRES" not in sql
    assert "SET ROLE MIGRATION_OWNER" not in sql
    assert "BYPASSRLS" not in sql
