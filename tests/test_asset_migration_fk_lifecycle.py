from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOGO = ROOT / "alembic/versions/f6b19eae1a7c_add_logo_upload_and_audit.py"
COVER = ROOT / "alembic/versions/371b1a44a328_add_cover_upload_and_audit_.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _function_source(path: Path, name: str) -> str:
    source = _source(path)
    tree = ast.parse(source, filename=str(path))
    node = next(
        item
        for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == name
    )
    lines = source.splitlines()
    return "\n".join(lines[node.lineno - 1 : node.end_lineno])


def test_logo_updated_by_fk_is_named_and_validated_before_rollback() -> None:
    source = _source(LOGO)
    upgrade = _function_source(LOGO, "upgrade")
    downgrade = _function_source(LOGO, "downgrade")

    assert "_LOGO_UPDATED_BY_FK = 'organizations_logo_updated_by_fkey'" in source
    assert "op.create_foreign_key(_LOGO_UPDATED_BY_FK" in upgrade
    assert "_require_logo_updated_by_fk()" in downgrade
    assert "op.drop_constraint(_LOGO_UPDATED_BY_FK" in downgrade
    assert "op.drop_constraint(None" not in source
    assert '"delete_action": "n"' in source
    assert '"source_column": "logo_updated_by"' in source
    assert '"target_table": "gym_owners"' in source


def test_cover_updated_by_fk_is_named_and_validated_before_rollback() -> None:
    source = _source(COVER)
    upgrade = _function_source(COVER, "upgrade")
    downgrade = _function_source(COVER, "downgrade")

    assert "_COVER_UPDATED_BY_FK = 'organizations_cover_updated_by_fkey'" in source
    assert "op.create_foreign_key(_COVER_UPDATED_BY_FK" in upgrade
    assert "_require_cover_updated_by_fk()" in downgrade
    assert "op.drop_constraint(_COVER_UPDATED_BY_FK" in downgrade
    assert "op.drop_constraint(None" not in source
    assert '"delete_action": "n"' in source
    assert '"source_column": "cover_updated_by"' in source
    assert '"target_table": "gym_owners"' in source
