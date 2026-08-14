from __future__ import annotations

import ast
from pathlib import Path


MIGRATION = Path(
    "alembic/versions/a3b4c5d6e7f8_allow_legacy_branch_transition_reference_read.py"
)


def _source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    source = _source()
    module = ast.parse(source, filename=str(MIGRATION))
    node = next(
        item
        for item in module.body
        if isinstance(item, ast.FunctionDef) and item.name == name
    )
    return ast.unparse(node)


def test_revision_is_forward_only_after_branch_read_alignment() -> None:
    source = _source()
    assert 'revision: str = "a3b4c5d6e7f8"' in source
    assert 'down_revision: Union[str, None] = "92a3b4c5d6e7"' in source


def test_runtime_receives_only_transition_catalog_select() -> None:
    source = _source()
    verify = _function_source("_verify_forward")

    assert (
        "GRANT SELECT ON TABLE public.allowed_branch_transitions TO app_runtime"
        in source
    )
    assert "GRANT ALL" not in source.upper()
    assert "BYPASSRLS" not in source.upper()
    assert "auth_runtime" not in source

    for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
        assert privilege in verify


def test_preflight_rejects_owner_public_and_acl_drift() -> None:
    preflight = _function_source("_preflight")

    assert "migration_owner" in preflight
    assert "PUBLIC has unexpected transition-catalog privilege" in preflight
    assert "app_runtime already has transition-catalog SELECT" in preflight
    assert "allowed_branch_transitions" in preflight


def test_downgrade_revokes_exact_revision_owned_grant() -> None:
    downgrade = _function_source("downgrade")
    assert (
        "REVOKE SELECT ON TABLE public.allowed_branch_transitions FROM app_runtime"
        in downgrade
    )
