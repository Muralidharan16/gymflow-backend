from __future__ import annotations

import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/c97d8e9f0a23_p3b_registration_read_boundary.py"
REPOSITORY = ROOT / "app/repositories/organization_registrations.py"
ROUTER = ROOT / "app/routers/organizations.py"
GYM_SERVICE = ROOT / "app/services/gym_service.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _function_source(path: Path, name: str) -> str:
    source = _source(path)
    tree = ast.parse(source, filename=str(path))
    node = next(
        item
        for item in ast.walk(tree)
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


def test_p3b_read_boundary_is_single_expand_step_after_certified_p3a() -> None:
    source = _source(MIGRATION)

    assert 'revision = "c97d8e9f0a23"' in source
    assert 'down_revision = "c87d8e9f0a22"' in source
    assert "Existing mutation\nACLs are intentionally not contracted in this revision" in source

    assert set(_assignment("_READ_COLUMNS")) == {
        "id",
        "org_id",
        "id_type",
        "id_number_masked",
        "country_code",
        "is_verified",
        "verified_at",
    }
    assert "id_number_encrypted" not in set(_assignment("_READ_COLUMNS"))


def test_registration_relation_is_forced_rls_and_capabilities_are_hardened() -> None:
    source = _source(MIGRATION)
    normalized = " ".join(source.split())

    assert "ALTER TABLE public.organization_registrations ENABLE ROW LEVEL SECURITY" in source
    assert "ALTER TABLE public.organization_registrations FORCE ROW LEVEL SECURITY" in source
    assert "CREATE POLICY" in source
    assert "org_id = NULLIF(" in source
    assert "app.current_org_id" in source

    assert "CREATE FUNCTION app_secure.current_organization_registrations()" in source
    assert "CREATE FUNCTION app_secure.current_organization_has_registration()" in source
    assert source.count("\nSECURITY DEFINER\n") == 2
    assert source.count("\nSET search_path = pg_catalog\n") == 2
    assert source.count("\nSET row_security = on\n") == 2

    for token in (
        "app.current_org_id",
        "app.current_user_id",
        "app.current_principal_type",
        "app.current_role",
        "app.current_gym_id",
        "public.owners",
        "public.organization_users",
        "('owner', 'admin')",
        "ERRCODE = '42501'",
    ):
        assert token in source

    assert (
        "REVOKE ALL ON FUNCTION app_secure.current_organization_registrations() FROM PUBLIC"
        in normalized
    )
    assert (
        "REVOKE ALL ON FUNCTION app_secure.current_organization_has_registration() FROM PUBLIC"
        in normalized
    )
    assert (
        "GRANT EXECUTE ON FUNCTION app_secure.current_organization_registrations() TO app_runtime"
        in normalized
    )
    assert (
        "GRANT EXECUTE ON FUNCTION app_secure.current_organization_has_registration() TO app_runtime"
        in normalized
    )


def test_expand_step_does_not_create_new_direct_api_table_privileges() -> None:
    source = _source(MIGRATION)
    upper = re.sub(r"\s+", " ", source).upper()

    # Existing mutation ACLs are preserved temporarily for expand/contract
    # compatibility. This revision must not create any new direct API table ACL.
    assert not re.search(
        r"GRANT\s+(?:SELECT|INSERT|UPDATE|DELETE|ALL)(?:\s*\([^)]*\))?\s+"
        r"ON\s+(?:TABLE\s+)?PUBLIC\.ORGANIZATION_REGISTRATIONS\s+TO\s+APP_RUNTIME",
        upper,
    )
    assert "GRANT ALL" not in upper
    assert "ROW_SECURITY = OFF" not in upper
    assert "OWNER TO APP_RUNTIME" not in upper
    assert not re.search(
        r"\b(?:CREATE|ALTER)\s+ROLE\s+(?:APP_RUNTIME|AUTH_RUNTIME|APP_SECURITY_OWNER)"
        r"\b[^;]*\bBYPASSRLS\b",
        upper,
        flags=re.DOTALL,
    )


def test_security_owner_read_acl_excludes_encrypted_registration_payload() -> None:
    source = _source(MIGRATION)

    assert 'if ("id_number_encrypted", "SELECT") in acl:' in source
    assert "app_security_owner must not read encrypted registration payloads" in source
    assert "GRANT SELECT (" in source
    assert "TO app_security_owner" in source

    list_function = source.split('_LIST_FUNCTION = f"""', 1)[1].split('"""\n\n_EXISTS_FUNCTION', 1)[0]
    select_projection = list_function.split("RETURN QUERY", 1)[1]
    assert "id_number_masked" in select_projection
    assert "id_number_encrypted" not in select_projection


def test_registration_repository_can_only_use_bounded_app_secure_read_capabilities() -> None:
    source = _source(REPOSITORY)
    tree = ast.parse(source, filename=str(REPOSITORY))

    assert "app_secure.current_organization_registrations()" in source
    assert "app_secure.current_organization_has_registration()" in source
    assert "public.organization_registrations" not in source
    assert "OrganizationRegistration" not in source
    assert "id_number_encrypted" not in source
    assert 'if _sqlstate(exc) == "42501"' in source
    assert "RegistrationAuthorizationError" in source

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            parameter_names = {arg.arg for arg in node.args.args}
            assert "org_id" not in parameter_names
            assert "user_id" not in parameter_names


def test_profile_read_returns_only_masked_registration_contract() -> None:
    source = _source(ROUTER)
    profile_response = _function_source(ROUTER, "_profile_response")
    get_profile = _function_source(ROUTER, "get_org_profile")

    assert "list_current_organization_registrations" in source
    assert "await _get_registrations_or_forbidden(db)" in get_profile
    assert "RegistrationResponse.model_validate(registration)" in profile_response
    assert "business_id=None" in profile_response
    assert "gst_number=None" in profile_response
    assert "pan_number=None" in profile_response
    assert "id_number_encrypted" not in profile_response
    assert "encrypt_data" not in profile_response
    assert "decrypt_data" not in profile_response

    authorization_adapter = _function_source(ROUTER, "_get_registrations_or_forbidden")
    assert "except RegistrationAuthorizationError as exc:" in authorization_adapter
    assert "status_code=status.HTTP_403_FORBIDDEN" in authorization_adapter
    assert 'detail="Organization registration access denied"' in authorization_adapter
    assert "str(exc)" not in authorization_adapter


def test_branch_creation_uses_boolean_registration_capability_without_loading_secret_rows() -> None:
    source = _source(GYM_SERVICE)
    create_branch = _function_source(GYM_SERVICE, "create_branch")

    assert (
        "from app.repositories.organization_registrations import "
        "current_organization_has_registration"
    ) in source
    assert "await current_organization_has_registration(self.session)" in create_branch
    assert "OrganizationRegistration" not in source
    assert "id_number_encrypted" not in create_branch
    assert "id_number_masked" not in create_branch
