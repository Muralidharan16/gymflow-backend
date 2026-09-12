from __future__ import annotations

import ast
from pathlib import Path


SOURCE = Path("app/core/database.py")


def _class_source(source: str, name: str) -> str:
    tree = ast.parse(source)
    node = next(
        item
        for item in tree.body
        if isinstance(item, ast.ClassDef) and item.name == name
    )
    assert node.end_lineno is not None
    lines = source.splitlines(keepends=True)
    return "".join(lines[node.lineno - 1 : node.end_lineno])


def test_request_context_is_session_owned_and_reapplied_on_every_transaction() -> None:
    source = SOURCE.read_text(encoding="utf-8")

    assert '_SESSION_CONTEXT_KEY = "doers.request_db_context"' in source
    assert '@event.listens_for(Session, "after_begin")' in source
    assert "session.info.get(_SESSION_CONTEXT_KEY)" in source
    assert "_apply_context_sync(connection, context)" in source
    assert "pg_catalog.set_config(:setting_name, :setting_value, true)" in source


def test_initializer_does_not_open_and_close_a_context_only_transaction() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    initializer = _class_source(source, "SessionContextInitializer")

    assert "async with session.begin()" not in initializer
    assert "session.info[_SESSION_CONTEXT_KEY]" in initializer


def test_dynamic_context_updates_current_and_future_transactions() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    start = source.index("async def update_session_context(")
    end = source.index("class SessionContextInitializer:", start)
    helper = source[start:end]

    assert "session.info[_SESSION_CONTEXT_KEY] = context" in helper
    assert "if session.in_transaction():" in helper
    assert "await session.execute(" in helper


def test_context_never_uses_session_level_gucs_or_privileged_deadlock_setting() -> None:
    source = SOURCE.read_text(encoding="utf-8").lower()

    assert "deadlock_timeout" in source  # documented as infrastructure-owned
    assert "set deadlock_timeout" not in source
    assert "set local deadlock_timeout" not in source
    assert "set_config(:setting_name, :setting_value, false)" not in source
    assert "app.current_principal_type" in source


def test_principal_namespace_is_explicit_and_fail_closed() -> None:
    source = SOURCE.read_text(encoding="utf-8")

    assert '"owner", "organization_user", "legacy_gym_owner"' in source
    assert "Unsupported database principal type" in source
    assert "normalized_principal_type = \"owner\"" in source
