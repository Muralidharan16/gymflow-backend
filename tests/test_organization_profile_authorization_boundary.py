from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas.organization import OrganizationUpdate


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/c17d8e9f0a1b_organization_profile_authorization.py"
ROUTER = ROOT / "app/routers/organizations.py"
REPOSITORY = ROOT / "app/repositories/organization_profile.py"
WORKER = ROOT / "app/tasks/base_image.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _function_source(path: Path, name: str) -> str:
    source = _source(path)
    tree = ast.parse(source, filename=str(path))
    node = next(
        item
        for item in tree.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name == name
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


def test_p3a_is_a_single_head_delta_with_exact_predecessor_acl_contract() -> None:
    source = _source(MIGRATION)
    assert 'revision = "c17d8e9f0a1b"' in source
    assert 'down_revision = "b06c7d8e9f0a"' in source

    assert set(_assignment("_PREDECESSOR_AUTH_RELATION_ACL")) == {
        "SELECT",
        "INSERT",
        "UPDATE",
    }
    assert set(_assignment("_FORWARD_AUTH_RELATION_ACL")) == {"SELECT", "INSERT"}
    assert set(_assignment("_PREDECESSOR_SECURITY_COLUMN_ACL")) == {
        ("id", "SELECT"),
        ("slug", "SELECT"),
        ("default_currency_code", "SELECT"),
    }

    # Table ACLs and direct per-column ACLs are deliberately inspected through
    # separate PostgreSQL catalogs. information_schema.column_privileges expands
    # table-wide grants into one row per column and cannot prove this boundary.
    assert "relation_data.relacl" in source
    assert "attribute_data.attacl" in source
    assert "pg_catalog.aclexplode" in source
    assert "information_schema.column_privileges" not in source


def test_profile_update_columns_are_narrow_and_exclude_control_plane_and_worker_state() -> None:
    update_columns = set(_assignment("_PROFILE_UPDATE_COLUMNS"))
    assert update_columns == {
        "name",
        "business_type",
        "tagline",
        "description",
        "year_established",
        "website_url",
        "social_links",
        "updated_at",
    }

    protected = {
        "id",
        "slug",
        "tier",
        "is_active",
        "max_branches",
        "default_currency_code",
        "website_verified",
        "document_url",
        "verification_status",
        "phone",
        "address_line1",
        "address_line2",
        "city",
        "state",
        "pincode",
        "country",
        "profile_completed",
        "logo_key",
        "logo_thumb_key",
        "logo_medium_key",
        "logo_full_key",
        "logo_meta",
        "logo_status",
        "logo_updated_at",
        "logo_updated_by",
        "cover_key",
        "cover_mobile_key",
        "cover_tablet_key",
        "cover_desktop_key",
        "cover_meta",
        "cover_status",
        "cover_updated_at",
        "cover_updated_by",
        "created_at",
    }
    assert update_columns.isdisjoint(protected)


def test_bootstrap_update_is_column_scoped_without_changing_creation_contract() -> None:
    source = _source(MIGRATION)
    auth_update = set(_assignment("_AUTH_UPDATE_COLUMNS"))
    assert auth_update == {
        "phone",
        "address_line1",
        "address_line2",
        "city",
        "state",
        "pincode",
        "profile_completed",
        "tagline",
        "description",
        "year_established",
        "website_url",
        "social_links",
        "updated_at",
    }
    assert auth_update.isdisjoint(
        {
            "tier",
            "is_active",
            "max_branches",
            "default_currency_code",
            "website_verified",
            "document_url",
            "verification_status",
            "country",
            "slug",
        }
    )

    upgrade = _function_source(MIGRATION, "upgrade")
    downgrade = _function_source(MIGRATION, "downgrade")
    assert "REVOKE UPDATE ON TABLE public.organizations FROM auth_runtime" in upgrade
    assert "_AUTH_UPDATE_COLUMNS" in upgrade
    assert "TO auth_runtime" in upgrade
    assert "GRANT UPDATE ON TABLE public.organizations TO auth_runtime" in downgrade
    assert "REVOKE UPDATE (" in downgrade

    # P3A narrows existing-row mutation. It does not silently redesign the
    # separately certified pre-tenant organization creation/read contract.
    assert '_FORWARD_AUTH_RELATION_ACL = {"INSERT", "SELECT"}' in source


def test_profile_capabilities_are_current_tenant_and_org_admin_bound() -> None:
    source = _source(MIGRATION)
    normalized = " ".join(source.split())

    assert "CREATE FUNCTION app_secure.current_organization_profile()" in source
    assert "CREATE FUNCTION app_secure.update_current_organization_profile(p_patch jsonb)" in source
    assert source.count("SECURITY DEFINER") == 2
    assert source.count("SET search_path = pg_catalog") == 2
    assert source.count("SET row_security = on") == 2

    for token in (
        "app.current_org_id",
        "app.current_role",
        "app.current_gym_id",
        "organization profile tenant context is required",
        "organization profile tenant context is invalid",
        "organization profile admin context is required",
        "('owner', 'admin')",
    ):
        assert token in source

    assert "target_org_id" not in source
    assert "p_org_id" not in source
    assert "WHERE organization.id = v_org_id" in source
    assert "unknown organization profile fields" in source

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


def test_api_runtime_receives_no_direct_organizations_acl_or_escalation() -> None:
    source = _source(MIGRATION)
    upper = re.sub(r"\s+", " ", source).upper()

    assert "TO APP_SECURITY_OWNER" in upper
    assert not re.search(
        r"GRANT\s+(?:SELECT|INSERT|UPDATE|DELETE|ALL)(?:\s*\([^)]*\))?\s+"
        r"ON\s+(?:TABLE\s+)?PUBLIC\.ORGANIZATIONS\s+TO\s+APP_RUNTIME",
        upper,
    )
    assert "GRANT ALL" not in upper
    assert "ROW_SECURITY = OFF" not in upper
    assert "DISABLE ROW LEVEL SECURITY" not in upper
    assert "OWNER TO APP_RUNTIME" not in upper
    assert not re.search(
        r"\b(?:CREATE|ALTER)\s+ROLE\s+(?:APP_RUNTIME|AUTH_RUNTIME|APP_SECURITY_OWNER)"
        r"\b[^;]*\bBYPASSRLS\b",
        upper,
        flags=re.DOTALL,
    )


def test_profile_route_never_loads_or_refreshes_the_organization_orm_row() -> None:
    source = _source(ROUTER)
    tree = ast.parse(source, filename=str(ROUTER))

    assert "from app.models.organization import OrganizationRegistration\n" in source
    assert "OrganizationRegistration, Organization" not in source
    assert "select(Organization)" not in source
    assert "db.refresh(org)" not in source
    assert "get_current_organization_profile" in source
    assert "update_current_organization_profile" in source
    assert "Depends(require_org_admin)" in source

    imported = {
        alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "Organization" not in imported


def test_profile_repository_can_only_call_bounded_app_secure_capabilities() -> None:
    source = _source(REPOSITORY)
    assert "app_secure.current_organization_profile()" in source
    assert "app_secure.update_current_organization_profile(" in source
    assert "public.organizations" not in source
    assert "Organization" not in source
    assert "org_id" not in source
    assert "json.dumps" in source


def test_request_validation_matches_non_nullable_database_profile_fields() -> None:
    with pytest.raises(ValidationError):
        OrganizationUpdate(name=None)
    with pytest.raises(ValidationError):
        OrganizationUpdate(social_links=None)

    # Explicit null remains legal PATCH semantics for nullable profile fields.
    payload = OrganizationUpdate(
        business_type=None,
        tagline=None,
        description=None,
        year_established=None,
        website_url=None,
    )
    assert payload.model_dump(exclude_unset=True) == {
        "business_type": None,
        "tagline": None,
        "description": None,
        "year_established": None,
        "website_url": None,
    }


def test_worker_asset_writer_remains_on_dedicated_worker_identity_and_outside_p3a_acl() -> None:
    migration = _source(MIGRATION)
    worker = _source(WORKER)

    assert "worker_runtime" not in migration
    assert "WorkerSyncSessionLocal" in worker
    assert "org.logo_status" in worker
    assert "org.cover_status" in worker
    assert "settings.DATABASE_URL" not in worker


def test_downgrade_is_restrictive_and_restores_predecessor_without_security_shortcuts() -> None:
    source = _source(MIGRATION)
    downgrade = _function_source(MIGRATION, "downgrade")

    assert "_require_forward(bind)" in downgrade
    assert "DROP FUNCTION app_secure.update_current_organization_profile(jsonb) RESTRICT" in downgrade
    assert "DROP FUNCTION app_secure.current_organization_profile() RESTRICT" in downgrade
    assert "_require_predecessor(bind)" in downgrade
    assert "CASCADE" not in downgrade
    assert "BYPASSRLS" not in downgrade
    assert "DISABLE TRIGGER" not in source.upper()
