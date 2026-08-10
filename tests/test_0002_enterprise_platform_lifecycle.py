import ast
from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0002_enterprise_platform.py"
)


def _source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _upgrade_source(source: str) -> str:
    return source[source.index("def upgrade() -> None:") : source.index("def downgrade() -> None:")]


def _downgrade_source(source: str) -> str:
    return source[source.index("def downgrade() -> None:") :]


def _executed_sql(function_source: str) -> tuple[str, ...]:
    """Return literal SQL passed to op.execute(), excluding comments/docstrings."""
    statements: list[str] = []
    tree = ast.parse(function_source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        function = node.func
        if not (
            isinstance(function, ast.Attribute)
            and function.attr == "execute"
            and isinstance(function.value, ast.Name)
            and function.value.id == "op"
        ):
            continue
        sql = node.args[0]
        if isinstance(sql, ast.Constant) and isinstance(sql.value, str):
            statements.append(sql.value)
    return tuple(statements)


def test_0002_upgrade_refuses_silent_object_adoption() -> None:
    source = _source()
    upgrade = _upgrade_source(source)

    assert "_preflight_upgrade()" in upgrade
    assert "already exists; refusing adoption" in source
    assert "to_regclass('public.' || relation_name)" in source
    assert "IF NOT EXISTS" not in upgrade


def test_0002_downgrade_refuses_populated_revision_owned_state() -> None:
    source = _source()
    downgrade = _downgrade_source(source)

    assert "_preflight_downgrade()" in downgrade
    assert "downgrade would discard populated revision-owned relation" in source
    assert "SELECT EXISTS (SELECT 1 FROM public.%I LIMIT 1)" in source


def test_0002_downgrade_never_cascades_or_masks_missing_objects() -> None:
    source = _source()
    downgrade = _downgrade_source(source)
    executed_sql = _executed_sql(downgrade)
    normalized_sql = "\n".join(executed_sql).upper()

    # Inspect executable DDL rather than comments/docstrings. A comment that
    # explains why CASCADE is forbidden must not make this contract fail.
    assert "CASCADE" not in normalized_sql
    assert "IF EXISTS" not in normalized_sql
    assert normalized_sql.count(" RESTRICT") == 6
