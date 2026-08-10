from __future__ import annotations

import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/6f708192a3b4_address_runtime_privilege_boundary.py"
ROUTER = ROOT / "app/routers/address.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    source = _source(MIGRATION)
    tree = ast.parse(source, filename=str(MIGRATION))
    node = next(
        item
        for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == name
    )
    assert node.end_lineno is not None
    lines = source.splitlines(keepends=True)
    return "".join(lines[node.lineno - 1 : node.end_lineno])


def test_address_revision_is_single_head_delta_and_restores_predecessor() -> None:
    source = _source(MIGRATION)
    assert 'revision = "6f708192a3b4"' in source
    assert 'down_revision = "5e6f708192a3"' in source

    # 00f owns ENABLE/FORCE RLS. 6f validates that predecessor state but must
    # never toggle it as a side effect of changing the runtime privilege model.
    predecessor = _function_source("_require_predecessor")
    forward = _function_source("_require_forward")
    upgrade = _function_source("upgrade")
    downgrade = _function_source("downgrade")
    assert "_require_rls_flags(bind, enabled=True)" in predecessor
    assert "_require_rls_flags(bind, enabled=True)" in forward
    for mutation in (
        "ENABLE ROW LEVEL SECURITY",
        "DISABLE ROW LEVEL SECURITY",
        "FORCE ROW LEVEL SECURITY",
        "NO FORCE ROW LEVEL SECURITY",
    ):
        assert mutation not in upgrade
        assert mutation not in downgrade

    for relation in (
        "organization_addresses",
        "branch_geocode_attempts",
        "address_change_outbox",
        "branch_address_history",
        "branch_address_audit_log",
    ):
        assert relation in source


def test_application_runtime_gets_only_user_facing_address_dml() -> None:
    source = _source(MIGRATION)
    upgrade = _function_source("upgrade")

    assert (
        "GRANT SELECT, INSERT, UPDATE ON TABLE "
        "public.organization_addresses TO app_runtime"
    ) in upgrade
    assert "_FORBIDDEN_RUNTIME" in source
    for privilege in ("DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
        assert f'"{privilege}"' in source

    for internal_relation in (
        "branch_address_history",
        "branch_address_audit_log",
        "address_change_outbox",
    ):
        assert not re.search(
            rf"GRANT[^\n;]*ON TABLE public\.{internal_relation} TO app_runtime",
            source,
            re.IGNORECASE,
        )

    assert "GRANT ALL" not in source.upper()
    assert "ALTER ROLE app_runtime" not in source
    assert "OWNER TO app_runtime" not in source


def test_internal_side_effects_use_hardened_security_definer_functions() -> None:
    source = _source(MIGRATION)
    create = _function_source("_create_functions")
    lock = _function_source("_lock_functions")

    assert "SET LOCAL ROLE app_security_owner" in source
    assert create.count("SECURITY DEFINER") == 2
    assert create.count("SET search_path = pg_catalog") == 2
    assert create.count("SET row_security = on") == 2
    assert "public.branch_address_history" in create
    assert "public.branch_address_audit_log" in create
    assert "public.address_change_outbox" in create
    assert "app.current_org_id" in create
    assert "organization address tenant context mismatch" in create

    # 4d5e establishes typed actor provenance before 6f. Moving the address
    # trigger functions into app_secure must preserve both members of that pair.
    assert create.count("changed_by_type") >= 3
    assert create.count("app.current_user_id") >= 3
    assert create.count("app.current_principal_type") >= 3
    hardened_contract = _function_source("_require_hardened_functions")
    assert "app.current_principal_type" in hardened_contract
    assert "changed_by_type" in hardened_contract

    assert lock.count("REVOKE ALL ON FUNCTION") == 2
    assert "FROM PUBLIC" in lock
    assert "REVOKE USAGE ON SCHEMA app_secure FROM migration_owner" in lock


def test_missing_audit_insert_policy_is_tenant_scoped_not_bypass() -> None:
    source = _source(MIGRATION)
    assert "CREATE POLICY tenant_isolation_audit_insert" in source
    assert "FOR INSERT WITH CHECK" in source
    assert "app.current_org_id" in source
    assert "BYPASSRLS" not in _function_source("upgrade")
    assert "row_security = off" not in source.lower()
    assert "DISABLE TRIGGER" not in source.upper()


def test_address_router_keeps_api_rbac_and_ordinary_runtime_pool() -> None:
    source = _source(ROUTER)

    maps = source[source.index('@router.patch("/{address_id}/maps"') :]
    maps = maps[: maps.index('@router.patch("/{address_id}"')]
    assert "Depends(get_db)" in maps
    assert "Depends(require_org_admin)" in maps

    update = source[source.index('@router.patch("/{address_id}"') :]
    update = update[: update.index("# =====================================================================\n# PRIMARY ROUTE SETTER")]
    assert "Depends(get_db)" in update
    assert "Depends(require_org_admin)" in update

    assert "SET ROLE branch_admin" not in source
    assert "BYPASSRLS" not in source