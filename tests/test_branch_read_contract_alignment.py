from __future__ import annotations

import ast
from pathlib import Path


MIGRATION = Path("alembic/versions/92a3b4c5d6e7_align_branch_read_contract.py")
DEPS = Path("app/core/deps.py")


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _function_source(path: Path, name: str) -> str:
    module = ast.parse(_source(path), filename=str(path))
    node = next(
        item
        for item in module.body
        if isinstance(item, ast.FunctionDef) and item.name == name
    )
    return ast.unparse(node)


def test_revision_extends_trigger_hardening_without_write_expansion() -> None:
    source = _source(MIGRATION)

    assert 'revision: str = "92a3b4c5d6e7"' in source
    assert 'down_revision: Union[str, None] = "8192a3b4c5d6"' in source
    assert "GRANT SELECT ON TABLE public.v_active_org_branches TO app_runtime" in source
    assert "GRANT ALL" not in source.upper()
    assert "BYPASSRLS" not in source.upper()
    assert "GRANT INSERT" not in source.upper()
    assert "GRANT UPDATE" not in source.upper()
    assert "GRANT DELETE" not in source.upper()
    assert "GRANT TRUNCATE" not in source.upper()


def test_view_grant_requires_security_invoker_and_is_select_only() -> None:
    preflight = _function_source(MIGRATION, "_preflight_upgrade")
    verify = _function_source(MIGRATION, "_verify_upgrade")

    assert "security_invoker=true" in preflight
    assert "app_runtime already has SELECT" in preflight
    assert "app_runtime lacks view SELECT" in verify
    assert "non-SELECT view privilege" in verify
    for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
        assert privilege in verify


def test_rls_status_matrix_is_explicit_and_fail_closed() -> None:
    policy = _function_source(MIGRATION, "_install_aligned_select_policy")

    assert "app.current_org_id" in policy
    assert "auth.role() = 'trainer'" in policy
    assert "status = 'active'" in policy
    assert "auth.role() = 'manager'" in policy
    assert "'temporarily_closed'" in policy
    assert "'under_renovation'" in policy
    assert "auth.role() IN ('owner', 'admin', 'org_admin')" in policy
    assert "'compliance_suspended'" in policy
    assert "'permanently_closed'" in policy
    assert "'saga_orchestrator'" in policy
    assert "'system_watchdog'" in policy

    # Trainer has its single explicit status equality; manager, owner/admin and
    # internal-system branches each carry a bounded status IN allowlist. This
    # rejects a newly introduced/corrupt status until both authorization layers
    # deliberately learn it.
    assert policy.count("status IN") == 3
    assert policy.count("status = 'active'") == 1


def test_application_guard_denies_unknown_status_explicitly() -> None:
    source = _source(DEPS)

    class_node = next(
        item
        for item in ast.parse(source, filename=str(DEPS)).body
        if isinstance(item, ast.ClassDef) and item.name == "BranchAccessGuard"
    )
    call_node = next(
        item
        for item in class_node.body
        if isinstance(item, ast.AsyncFunctionDef) and item.name == "__call__"
    )
    call_source = ast.unparse(call_node)

    assert "Branch lifecycle status is not recognized" in call_source
    assert "status.HTTP_403_FORBIDDEN" in call_source
    assert "else:" in call_source


def test_downgrade_restores_predecessor_policy_and_removes_view_grant() -> None:
    predecessor = _function_source(MIGRATION, "_restore_predecessor_select_policy")
    downgrade = _function_source(MIGRATION, "downgrade")

    assert "auth.role() IN ('manager', 'trainer')" in predecessor
    assert "is_operational = TRUE" in predecessor
    assert "status != 'permanently_closed'" in predecessor
    assert "_restore_predecessor_select_policy()" in downgrade
    assert "REVOKE SELECT ON TABLE public.v_active_org_branches FROM app_runtime" in downgrade
