from __future__ import annotations

import ast
from pathlib import Path


FINANCE_TEST_ROOT = Path(__file__).resolve().parent
ADMIN_DATABASE = FINANCE_TEST_ROOT / "admin_database.py"


def _executed_sql_literals(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    statements: list[str] = []
    for call in ast.walk(tree):
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


def test_finance_fixtures_never_execute_truncate_cascade() -> None:
    offenders: list[str] = []
    for path in sorted(FINANCE_TEST_ROOT.glob("*.py")):
        for sql in _executed_sql_literals(path):
            normalized = " ".join(sql.upper().split())
            if "TRUNCATE" in normalized and "CASCADE" in normalized:
                offenders.append(path.name)

    assert offenders == [], (
        "Finance fixtures must never execute TRUNCATE ... CASCADE; "
        f"offenders: {sorted(set(offenders))}"
    )


def test_finance_cleanup_uses_live_fk_graph_and_no_cascade() -> None:
    source = ADMIN_DATABASE.read_text(encoding="utf-8")

    assert "pg_catalog.pg_constraint" in source
    assert "constraint_data.contype = 'f'" in source
    assert "parent_namespace.nspname = 'finance'" in source
    assert "child_namespace.nspname = 'finance'" in source
    assert "not FK-closed" in source
    assert "RESTART IDENTITY" in source

    executed = _executed_sql_literals(ADMIN_DATABASE)
    truncate_statements = [
        sql for sql in executed if "TRUNCATE" in sql.upper()
    ]
    assert truncate_statements
    assert all("CASCADE" not in sql.upper() for sql in truncate_statements)


def test_finance_cleanup_is_explicitly_admin_identity_only() -> None:
    source = ADMIN_DATABASE.read_text(encoding="utf-8")

    assert "FINANCE_CORE_TEST_ADMIN_DATABASE_URL" in source
    assert "FINANCE_CORE_TEST_DATABASE_URL" in source
    assert "runtime_url == admin_url or runtime.username == admin.username" in source
    assert "distinct database identity" in source
    assert "non-test database" in source
