from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "app" / "routers" / "organizations.py"
SERVICE = ROOT / "app" / "services" / "organization_profile_mutation_service.py"
SESSION_DEP = ROOT / "app" / "core" / "service_managed_database.py"


def _function(tree: ast.AST, name: str) -> ast.AsyncFunctionDef | ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"missing function: {name}")


def _call_names(node: ast.AST) -> list[str]:
    names: list[str] = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Attribute):
            names.append(func.attr)
        elif isinstance(func, ast.Name):
            names.append(func.id)
    return names


def _assert_no_registration_orm_import(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "app.models.organization":
            imported = {alias.name for alias in node.names}
            assert "OrganizationRegistration" not in imported
        if isinstance(node, ast.Import):
            imported = {alias.name for alias in node.names}
            assert "app.models.organization.OrganizationRegistration" not in imported


def test_patch_route_has_no_manual_commit_or_registration_orm_write() -> None:
    source = ROUTER.read_text()
    tree = ast.parse(source)
    patch = _function(tree, "update_org_profile")
    calls = _call_names(patch)

    assert "commit" not in calls
    assert "rollback" not in calls
    assert "mutate_organization_profile_atomically" in calls
    assert "create_secure_organization_registration" not in calls
    assert "replace_secure_organization_registration" not in calls
    _assert_no_registration_orm_import(tree)


def test_patch_route_uses_service_managed_session_dependency() -> None:
    source = ROUTER.read_text()
    tree = ast.parse(source)
    patch = _function(tree, "update_org_profile")
    rendered = ast.unparse(patch)

    assert "Depends(get_service_managed_db)" in rendered
    assert "Depends(get_db)" not in rendered


def test_certified_p3a_helper_surface_is_preserved_but_not_used_by_p3c_patch() -> None:
    source = ROUTER.read_text()
    tree = ast.parse(source)
    helper = _function(tree, "_update_profile_or_forbidden")
    patch = _function(tree, "update_org_profile")

    assert "update_current_organization_profile" in _call_names(helper)
    assert "_update_profile_or_forbidden" not in _call_names(patch)


def test_router_has_no_shape_guess_for_masked_identifier() -> None:
    source = ROUTER.read_text()
    tree = ast.parse(source)
    material = _function(tree, "_registration_material")

    assert "_looks_like_server_mask" not in source
    assert "MaskedRegistrationIdentifierError" in source
    assert "RegistrationCreate" in _call_names(material)


def test_atomic_service_is_the_only_transaction_owner() -> None:
    source = SERVICE.read_text()
    tree = ast.parse(source)
    mutation = _function(tree, "mutate_organization_profile_atomically")
    rendered = ast.unparse(mutation)
    calls = _call_names(mutation)

    assert rendered.count("session.begin()") == 1
    assert "session.in_transaction()" in rendered
    assert "commit" not in calls
    assert "rollback" not in calls
    assert "update_current_organization_profile" in calls
    assert "get_current_organization_profile" in calls
    assert "list_current_organization_registrations" in calls
    assert "create_secure_organization_registration" in calls
    assert "replace_secure_organization_registration" in calls


def test_atomic_service_establishes_both_read_authorizations_before_kms_and_profile_lock() -> None:
    source = SERVICE.read_text()
    tree = ast.parse(source)
    mutation = _function(tree, "mutate_organization_profile_atomically")
    rendered = ast.unparse(mutation)

    assert "requested.normalized_identifier == str(existing['id_number_masked'])" in rendered
    assert "MaskedRegistrationIdentifierError" in rendered

    p3a_read = rendered.index("get_current_organization_profile")
    p3b_read = rendered.index("list_current_organization_registrations")
    create = rendered.index("create_secure_organization_registration")
    replace = rendered.index("replace_secure_organization_registration")
    profile_update = rendered.index("update_current_organization_profile")

    assert p3a_read < p3b_read < create < profile_update
    assert p3a_read < p3b_read < replace < profile_update


def test_atomic_service_has_no_raw_registration_or_organization_dml() -> None:
    source = SERVICE.read_text()
    tree = ast.parse(source)

    _assert_no_registration_orm_import(tree)
    assert "Organization(" not in source
    assert "insert(" not in source
    assert "update(" not in source
    assert "delete(" not in source
    assert "SELECT " not in source
    assert "app_secure." not in source


def test_service_managed_dependency_never_success_commits_and_rolls_back_cancellation() -> None:
    source = SESSION_DEP.read_text()
    tree = ast.parse(source)
    dependency = _function(tree, "get_service_managed_db")
    rendered = ast.unparse(dependency)
    calls = _call_names(dependency)

    assert "commit" not in calls
    assert "rollback" in calls
    assert "except BaseException" in rendered
    assert "initialize_request_session" in calls
    assert "adaptive_controller.record_latency" in source


def test_normal_get_db_contract_is_not_modified_by_p3c_dependency() -> None:
    source = SESSION_DEP.read_text()

    assert "from app.core.database import AsyncSessionLocal, initialize_request_session" in source
    assert "pool_manager.current_sessionmaker or AsyncSessionLocal" in source
    assert "def get_db(" not in source
