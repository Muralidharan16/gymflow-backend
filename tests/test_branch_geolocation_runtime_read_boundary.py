from __future__ import annotations

import ast
from pathlib import Path


MIGRATION = Path(
    "alembic/versions/8192a3b4c5d6_branch_geolocation_runtime_read_boundary.py"
)


def _source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    module = ast.parse(_source(), filename=str(MIGRATION))
    node = next(
        item
        for item in module.body
        if isinstance(item, ast.FunctionDef) and item.name == name
    )
    return ast.unparse(node)


def test_geolocation_runtime_delta_is_read_only() -> None:
    source = _source()
    upgrade = _function_source("upgrade")

    assert (
        "GRANT SELECT ON TABLE public.branch_geolocation_state TO app_runtime"
        in upgrade
    )
    assert "GRANT ALL" not in source.upper()
    assert "BYPASSRLS" not in upgrade.upper()
    assert "OWNER TO app_runtime" not in source
    assert "ALTER ROLE" not in source
    assert '_FORBIDDEN = {"INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"}' in source


def test_geolocation_runtime_requires_existing_forced_rls_policy() -> None:
    source = _source()

    assert "must retain ENABLE + FORCE ROW LEVEL SECURITY" in source
    assert '"geolocation_state_tenant_isolation"' in source
    assert "policy[\"command\"] != \"*\"" in source
    assert "_TENANT_EXPR" in source
    assert "_require_no_public_table_privileges(bind)" in source


def test_geolocation_runtime_verifies_exact_forward_acl() -> None:
    forward = _function_source("_require_forward")

    assert 'observed != {"SELECT"}' in forward
    for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
        assert privilege in _source()
    assert "app_runtime must not have CREATE on public schema" in forward


def test_geolocation_runtime_downgrade_restores_empty_predecessor_acl() -> None:
    downgrade = _function_source("downgrade")

    assert "_require_forward(bind)" in downgrade
    assert (
        "REVOKE SELECT ON TABLE public.branch_geolocation_state FROM app_runtime"
        in downgrade
    )
    assert "_require_predecessor(bind)" in downgrade
