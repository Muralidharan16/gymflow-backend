from __future__ import annotations

import ast
import re
from pathlib import Path


MIGRATION = Path(
    "alembic/versions/c67d8e9f0a20_p3a_branch_limit_trigger_boundary.py"
)
P2_TRIGGER_HARDENING = Path(
    "alembic/versions/8192a3b4c5d6_harden_legacy_branch_trigger_runtime_boundary.py"
)


def _source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _function_source(source: str, name: str) -> str:
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            segment = ast.get_source_segment(source, node)
            assert segment is not None
            return segment
    raise AssertionError(f"missing function {name}")


def test_c67_extends_c57_as_single_p3a_head() -> None:
    source = _source()
    assert 'revision = "c67d8e9f0a20"' in source
    assert 'down_revision = "c57d8e9f0a1f"' in source


def test_branch_limit_trigger_uses_reduced_security_owner_not_auth_update() -> None:
    source = _source()
    forward = _function_source(source, "_create_forward_objects")

    assert "SECURITY DEFINER" in forward
    assert "SET search_path = pg_catalog" in forward
    assert "SET row_security = on" in forward
    assert "ALTER FUNCTION public.enforce_max_branches() OWNER TO app_security_owner" in forward
    assert "REVOKE ALL ON FUNCTION public.enforce_max_branches() FROM PUBLIC" in forward

    assert "GRANT SELECT (max_branches)" in source
    assert "TO app_security_owner" in source
    assert "GRANT UPDATE" not in source
    assert "FOR UPDATE" not in forward


def test_branch_limit_and_existing_critical_guard_share_lock_domain() -> None:
    source = _source()
    p2_source = P2_TRIGGER_HARDENING.read_text(encoding="utf-8")

    c67_seed = re.search(r"_BRANCH_LOCK_SEED\s*=\s*(\d+)", source)
    p2_seed = re.search(r"_ADVISORY_LOCK_SEED\s*=\s*(\d+)", p2_source)
    assert c67_seed is not None
    assert p2_seed is not None
    assert c67_seed.group(1) == p2_seed.group(1)

    forward = _function_source(source, "_create_forward_objects")
    assert forward.count("pg_advisory_xact_lock") == 2
    assert "trg_serialize_max_branches_update" in forward
    assert "BEFORE UPDATE OF max_branches ON public.organizations" in forward
    assert "OLD.max_branches IS DISTINCT FROM NEW.max_branches" in forward


def test_hardened_limit_guard_is_tenant_bound_and_uses_only_bounded_reads() -> None:
    source = _source()
    forward = _function_source(source, "_create_forward_objects")

    assert "app.current_org_id" in forward
    assert "NEW.org_id IS DISTINCT FROM v_org_id" in forward
    assert "FROM public.organizations AS organization" in forward
    assert "organization.max_branches" in forward
    assert "FROM public.org_branches AS branch" in forward
    assert "pg_catalog.count(branch.id)" in forward

    # The migration explicitly proves that security_owner has no broad relation
    # access and that auth/API privilege sets remain unchanged.
    verify = _function_source(source, "_require_forward")
    assert "gained organizations relation-level privilege" in verify
    assert '!= {"INSERT", "SELECT"}' in verify
    assert "leaked organizations relation ACL to app_runtime" in verify


def test_downgrade_restores_legacy_locking_contract_exactly() -> None:
    source = _source()
    restore = _function_source(source, "_restore_predecessor")
    downgrade = _function_source(source, "downgrade")

    assert "SECURITY INVOKER" in restore
    assert "FOR UPDATE" in restore
    assert "GRANT EXECUTE ON FUNCTION public.enforce_max_branches() TO PUBLIC" in restore
    assert "REVOKE SELECT (max_branches)" in downgrade
    assert "DROP TRIGGER trg_serialize_max_branches_update" in downgrade
    assert "_require_predecessor(bind)" in downgrade
