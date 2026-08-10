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
    assert "FORCE remains" in source
    assert "RLS is disabled" in source

    upgrade = _function_source("upgrade")
    downgrade = _function_source("downgrade")
    for relation in (
        "organization_addresses",
        "branch_geocode_attempts",
        "address_change_outbox",
        "branch_address_history",
        "branch_address_audit_log",
    ):
        assert relation in source
    assert "ENABLE ROW LEVEL SECURITY" in upgrade
    assert "DISABLE ROW LEVEL SECURITY" in downgrade
    assert "NO FORCE ROW LEVEL SECURITY" not in source


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

    create = source[source.index('@router.post("/addresses"') :]
    create = create[: create.index('@router.put("/addresses/{address_id}"')]
    assert "Depends(get_db)" in create
    assert "Depends(require_org_admin)" in create

    update = source[source.index('@router.put("/addresses/{address_id}"') :]
    assert "Depends(get_db)" in update
    assert "Depends(require_org_admin)" in update

    assert "SET ROLE branch_admin" not in source
    assert "BYPASSRLS" not in source
