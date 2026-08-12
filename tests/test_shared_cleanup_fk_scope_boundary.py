from __future__ import annotations

import ast
from pathlib import Path


CONFTST_PATH = Path(__file__).with_name("conftest.py")


def _function_node(name: str) -> ast.AsyncFunctionDef:
    tree = ast.parse(CONFTST_PATH.read_text())
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name
    ]
    assert len(matches) == 1, f"expected exactly one async function named {name!r}"
    return matches[0]


def _executed_sql_literals(node: ast.AST) -> list[str]:
    statements: list[str] = []
    for call in ast.walk(node):
        if not isinstance(call, ast.Call) or not call.args:
            continue
        if not isinstance(call.func, ast.Attribute) or call.func.attr != "execute":
            continue
        argument = call.args[0]
        if not (
            isinstance(argument, ast.Call)
            and isinstance(argument.func, ast.Name)
            and argument.func.id == "text"
            and argument.args
        ):
            continue
        sql_argument = argument.args[0]
        if isinstance(sql_argument, ast.Constant) and isinstance(sql_argument.value, str):
            statements.append(sql_argument.value)
        elif isinstance(sql_argument, ast.JoinedStr):
            literal_parts = [
                part.value
                for part in sql_argument.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            ]
            statements.append("".join(literal_parts))
    return statements


def test_shared_truncate_cleanup_is_explicit_and_fk_closed() -> None:
    node = _function_node("truncate_test_tables")
    source = ast.get_source_segment(CONFTST_PATH.read_text(), node)
    assert source is not None

    sql_literals = _executed_sql_literals(node)
    assert sql_literals, "cleanup helper must execute explicit catalog/DML SQL"
    assert all("CASCADE" not in sql.upper() for sql in sql_literals)

    assert "pg_catalog.pg_constraint" in source
    assert "constraint_data.contype = 'f'" in source
    assert "NOT (child.relname = ANY(:table_names))" in source
    assert "external_dependencies" in source
    assert "not FK-closed" in source
    assert "RESTART IDENTITY" in source


def test_shared_cleanup_uses_only_admin_test_identity() -> None:
    node = _function_node("cleanup_test_database_tables")
    source = ast.get_source_segment(CONFTST_PATH.read_text(), node)
    assert source is not None

    assert "AdminTestSessionLocal" in source
    assert "RESET ROLE" in source
    assert "truncate_test_tables" in source
    assert "TestSessionLocal" not in source
    assert "AuthTestSessionLocal" not in source
    assert "MaintenanceTestSessionLocal" not in source
