from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/h07d8e9f0a28_p3b_registration_replace_conflict_key_acl.py"


def _source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    source = _source()
    tree = ast.parse(source, filename=str(MIGRATION))
    node = next(
        item for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == name
    )
    assert node.end_lineno is not None
    return "".join(source.splitlines(keepends=True)[node.lineno - 1:node.end_lineno])


def test_conflict_key_acl_revision_follows_replace_capability() -> None:
    source = _source()
    assert 'revision = "h07d8e9f0a28"' in source
    assert 'down_revision = "g07d8e9f0a27"' in source


def test_security_owner_can_read_only_conflict_and_rls_tenant_keys() -> None:
    source = _source()
    forward = _function_source("_require_forward")
    assert '_EXPECTED_SECURITY_SELECT = {"registration_id", "tenant_id"}' in source
    assert "GRANT SELECT (registration_id, tenant_id)" in source
    assert "TO app_security_owner" in source
    assert "_select_columns(bind, _SECURITY_OWNER) != _EXPECTED_SECURITY_SELECT" in forward
    assert "_table_select(bind, _SECURITY_OWNER)" in forward

    for forbidden in (
        "GRANT SELECT ON TABLE public.organization_registration_payloads_secure",
        "GRANT SELECT (payload_encrypted)",
        "GRANT SELECT (key_version)",
        "GRANT SELECT (key_scope)",
        "GRANT SELECT (schema_version)",
        "GRANT ALL",
    ):
        assert forbidden not in source


def test_api_runtime_still_has_zero_direct_secure_payload_read() -> None:
    forward = _function_source("_require_forward")
    assert "_select_columns(bind, _API)" in forward
    assert "_table_select(bind, _API)" in forward
    assert "app_runtime leaked direct secure payload SELECT" in forward


def test_downgrade_restores_zero_payload_select_columns() -> None:
    source = _source()
    downgrade = _function_source("downgrade")
    assert "REVOKE SELECT (registration_id, tenant_id)" in downgrade
    assert "FROM app_security_owner" in downgrade
    assert "_require_predecessor(bind)" in downgrade
    assert "DISABLE ROW LEVEL SECURITY" not in source
    assert "NO FORCE ROW LEVEL SECURITY" not in source
    assert "BYPASSRLS" not in source
