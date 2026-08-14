from __future__ import annotations

import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/c37d8e9f0a1d_organization_profile_principal_binding.py"


def _source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    source = _source()
    tree = ast.parse(source, filename=str(MIGRATION))
    node = next(
        item
        for item in ast.walk(tree)
        if isinstance(item, ast.FunctionDef) and item.name == name
    )
    assert node.end_lineno is not None
    return "".join(source.splitlines(keepends=True)[node.lineno - 1 : node.end_lineno])


def test_principal_binding_is_the_final_p3a_head_delta() -> None:
    source = _source()
    assert 'revision = "c37d8e9f0a1d"' in source
    assert 'down_revision = "c27d8e9f0a1c"' in source
    assert "tenant GUC identifies request scope but is not, by itself, a business" in source


def test_profile_wrappers_bind_owner_or_active_org_user_to_current_tenant() -> None:
    source = _source()
    assert source.count("app.current_org_id") >= 2
    assert source.count("app.current_user_id") >= 2
    assert source.count("app.current_principal_type") >= 2

    for token in (
        "public.owners AS principal_owner",
        "principal_owner.id = v_user_id",
        "principal_owner.org_id = v_org_id",
        "public.organization_users AS principal_user",
        "principal_user.id = v_user_id",
        "principal_user.org_id = v_org_id",
        "principal_user.is_active",
        "principal_user.deleted_at IS NULL",
        "organization profile principal organization membership is required",
    ):
        assert token in source

    assert "ELSIF v_principal_type = 'organization_user'" in source
    assert "IF v_principal_type = 'owner'" in source


def test_internal_unbound_functions_are_not_executable_by_api_runtime() -> None:
    upgrade = _function_source("upgrade")
    forward = _function_source("_require_forward")

    assert "RENAME TO current_organization_profile_internal" in upgrade
    assert "RENAME TO update_current_organization_profile_internal" in upgrade
    # Long migration SQL is assembled from adjacent literals; assert the
    # executable function signature + recipient rather than source line layout.
    assert "current_organization_profile_internal() FROM app_runtime" in upgrade
    assert "update_current_organization_profile_internal(jsonb) FROM app_runtime" in upgrade
    assert "P3A internal function {name} leaked EXECUTE" in forward


def test_only_bounded_principal_lookup_columns_are_added_to_security_owner() -> None:
    source = _source()
    assert '_ORG_USER_SELECT_COLUMNS = ("id", "org_id", "is_active", "deleted_at")' in source
    assert "ON TABLE public.organization_users TO app_security_owner" in source
    for forbidden in (
        "password_hash",
        "email",
        "phone",
        "token_version",
        "GRANT SELECT ON TABLE public.organization_users",
    ):
        assert forbidden not in source


def test_public_wrappers_keep_security_definer_hardening_and_api_execute_only() -> None:
    source = _source()
    normalized = " ".join(source.split())

    assert source.count("\nSECURITY DEFINER\n") == 2
    assert source.count("\nSET search_path = pg_catalog\n") == 2
    assert source.count("\nSET row_security = on\n") == 2
    assert (
        "REVOKE ALL ON FUNCTION app_secure.current_organization_profile() FROM PUBLIC"
        in normalized
    )
    assert (
        "REVOKE ALL ON FUNCTION app_secure.update_current_organization_profile(jsonb) FROM PUBLIC"
        in normalized
    )
    assert (
        "GRANT EXECUTE ON FUNCTION app_secure.current_organization_profile() TO app_runtime"
        in normalized
    )
    assert (
        "GRANT EXECUTE ON FUNCTION app_secure.update_current_organization_profile(jsonb) TO app_runtime"
        in normalized
    )


def test_principal_binding_adds_no_table_dml_or_security_bypass() -> None:
    source = _source()
    upper = re.sub(r"\s+", " ", source).upper()

    assert not re.search(
        r"GRANT\s+(?:INSERT|UPDATE|DELETE|TRUNCATE|ALL).*?ON\s+(?:TABLE\s+)?PUBLIC\.",
        upper,
        flags=re.DOTALL,
    )
    assert "BYPASSRLS" not in _function_source("upgrade").upper()
    assert "ROW_SECURITY = OFF" not in upper
    assert "DISABLE ROW LEVEL SECURITY" not in upper
    assert "GRANT ALL" not in upper


def test_principal_binding_downgrade_restores_c27_function_names_and_acl() -> None:
    downgrade = _function_source("downgrade")
    assert "_require_forward(bind)" in downgrade
    assert "DROP FUNCTION app_secure.update_current_organization_profile(jsonb) RESTRICT" in downgrade
    assert "DROP FUNCTION app_secure.current_organization_profile() RESTRICT" in downgrade
    assert "RENAME TO current_organization_profile" in downgrade
    assert "RENAME TO update_current_organization_profile" in downgrade
    assert "TO app_runtime" in downgrade
    assert "FROM app_security_owner" in downgrade
    assert "_require_predecessor(bind)" in downgrade
    assert "CASCADE" not in downgrade
