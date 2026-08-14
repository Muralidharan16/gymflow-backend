from __future__ import annotations

import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/c27d8e9f0a1c_organization_onboarding_authorization.py"
SERVICE = ROOT / "app/services/onboarding_service.py"
REPOSITORY = ROOT / "app/repositories/organization_onboarding.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _function_source(path: Path, name: str) -> str:
    source = _source(path)
    tree = ast.parse(source, filename=str(path))
    node = next(
        item
        for item in tree.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name
    )
    assert node.end_lineno is not None
    return "".join(source.splitlines(keepends=True)[node.lineno - 1 : node.end_lineno])


def _assignment(name: str):
    tree = ast.parse(_source(MIGRATION), filename=str(MIGRATION))
    node = next(
        item
        for item in tree.body
        if isinstance(item, ast.Assign)
        and len(item.targets) == 1
        and isinstance(item.targets[0], ast.Name)
        and item.targets[0].id == name
    )
    return ast.literal_eval(node.value)


def test_onboarding_authorization_is_the_p3a_head_delta() -> None:
    source = _source(MIGRATION)
    assert 'revision = "c27d8e9f0a1c"' in source
    assert 'down_revision = "c17d8e9f0a1b"' in source
    assert "Column-scoping auth_runtime UPDATE is insufficient" in source


def test_auth_runtime_finishes_with_zero_direct_organization_update() -> None:
    source = _source(MIGRATION)
    upgrade = _function_source(MIGRATION, "upgrade")
    forward = _function_source(MIGRATION, "_require_forward")

    auth_columns = set(_assignment("_AUTH_UPDATE_COLUMNS"))
    assert {
        "phone",
        "address_line1",
        "address_line2",
        "city",
        "state",
        "pincode",
        "profile_completed",
    }.issubset(auth_columns)
    assert {"tier", "is_active", "max_branches", "verification_status"}.isdisjoint(
        auth_columns
    )

    assert "REVOKE UPDATE (" in upgrade
    assert "FROM auth_runtime" in upgrade
    assert 'if _direct_column_acl(bind, "public.organizations", _AUTH):' in forward
    assert "auth_runtime retained direct organizations column ACL" in forward
    assert "GRANT UPDATE ON TABLE public.organizations TO auth_runtime" not in upgrade


def test_auth_only_onboarding_helper_is_owner_tenant_pair_bound_and_fail_closed() -> None:
    source = _source(MIGRATION)
    normalized = " ".join(source.split())

    assert (
        "CREATE FUNCTION app_secure.complete_current_organization_onboarding_profile(p_patch jsonb)"
        in source
    )
    assert "SECURITY DEFINER" in source
    assert "SET search_path = pg_catalog" in source
    assert "SET row_security = on" in source

    for token in (
        "app.current_org_id",
        "app.current_user_id",
        "app.current_principal_type",
        "app.current_role",
        "app.current_gym_id",
        "v_role IS DISTINCT FROM 'owner'",
        "v_principal_type IS DISTINCT FROM 'owner'",
        "public.owners AS owner_data",
        "owner_data.id = v_user_id",
        "owner_data.org_id = v_org_id",
        "NOT owner_data.onboarding_completed",
        "WHERE organization.id = v_org_id",
        "unknown organization onboarding fields",
    ):
        assert token in source

    assert (
        "GRANT EXECUTE ON FUNCTION app_secure.complete_current_organization_onboarding_profile(jsonb) TO auth_runtime"
        in normalized
    )
    assert (
        "REVOKE ALL ON FUNCTION app_secure.complete_current_organization_onboarding_profile(jsonb) FROM PUBLIC"
        in normalized
    )
    assert "TO app_runtime" not in normalized[normalized.index("GRANT EXECUTE ON FUNCTION app_secure.complete_current_organization_onboarding_profile") :]


def test_security_owner_receives_only_onboarding_specific_extra_update_authority() -> None:
    extra = set(_assignment("_EXTRA_SECURITY_UPDATE_COLUMNS"))
    assert extra == {
        "phone",
        "address_line1",
        "address_line2",
        "city",
        "state",
        "pincode",
        "profile_completed",
    }
    assert extra.isdisjoint(
        {
            "id",
            "slug",
            "tier",
            "is_active",
            "max_branches",
            "default_currency_code",
            "website_verified",
            "document_url",
            "verification_status",
            "country",
            "logo_status",
            "cover_status",
        }
    )
    assert set(_assignment("_OWNER_SELECT_COLUMNS")) == {
        "id",
        "org_id",
        "onboarding_completed",
    }


def test_onboarding_service_has_no_direct_organization_orm_mutation() -> None:
    source = _source(SERVICE)
    complete = _function_source(SERVICE, "complete_onboarding")

    assert "complete_current_organization_onboarding_profile" in complete
    for mutation in (
        "org.phone =",
        "org.address_line1 =",
        "org.address_line2 =",
        "org.city =",
        "org.state =",
        "org.pincode =",
        "org.profile_completed =",
        "org.tagline =",
        "org.description =",
        "org.year_established =",
        "org.website_url =",
        "org.social_links =",
    ):
        assert mutation not in complete

    assert "update_session_context(" in complete
    assert 'principal_type="owner"' in complete
    assert 'role="owner"' in complete
    assert complete.index("update_session_context(") < complete.index(
        "complete_current_organization_onboarding_profile("
    )


def test_onboarding_repository_calls_only_the_auth_capability() -> None:
    source = _source(REPOSITORY)
    assert "app_secure.complete_current_organization_onboarding_profile" in source
    assert "public.organizations" not in source
    assert "Organization" not in source
    assert "org_id" not in source
    assert "json.dumps" in source


def test_onboarding_migration_does_not_weaken_other_process_boundaries() -> None:
    source = _source(MIGRATION)
    upper = re.sub(r"\s+", " ", source).upper()

    for role in ("APP_RUNTIME", "WORKER_RUNTIME", "LIFECYCLE_MAINTENANCE_RUNTIME"):
        assert not re.search(
            rf"GRANT\s+(?:SELECT|INSERT|UPDATE|DELETE|ALL).*?PUBLIC\.ORGANIZATIONS\s+TO\s+{role}",
            upper,
            flags=re.DOTALL,
        )
    assert "GRANT ALL" not in upper
    assert "ROW_SECURITY = OFF" not in upper
    assert "DISABLE ROW LEVEL SECURITY" not in upper
    assert not re.search(
        r"\b(?:CREATE|ALTER)\s+ROLE\b[^;]*\bBYPASSRLS\b",
        upper,
        flags=re.DOTALL,
    )


def test_onboarding_downgrade_restores_exact_c17_predecessor_contract() -> None:
    downgrade = _function_source(MIGRATION, "downgrade")
    assert "_require_forward(bind)" in downgrade
    assert "GRANT UPDATE (" in downgrade
    assert "TO auth_runtime" in downgrade
    assert (
        "DROP FUNCTION app_secure.complete_current_organization_onboarding_profile(jsonb) RESTRICT"
        in downgrade
    )
    assert "REVOKE USAGE ON SCHEMA app_secure FROM auth_runtime" in downgrade
    assert "FROM app_security_owner" in downgrade
    assert "_require_predecessor(bind)" in downgrade
    assert "CASCADE" not in downgrade
