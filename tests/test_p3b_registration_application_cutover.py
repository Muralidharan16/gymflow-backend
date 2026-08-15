from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "app/routers/organizations.py"
SERVICE = ROOT / "app/services/organization_registration_service.py"
ATOMIC_SERVICE = ROOT / "app/services/organization_profile_mutation_service.py"
MUTATION_REPOSITORY = ROOT / "app/repositories/organization_registration_mutations.py"
BACKFILL = ROOT / "scripts/p3b_backfill_legacy_registrations.py"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _imports_from(source: str, module: str) -> set[str]:
    tree = ast.parse(source)
    return {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == module
        for alias in node.names
    }


def test_normal_http_registration_path_has_no_legacy_orm_or_fernet_crypto() -> None:
    router = _text(ROUTER)
    service = _text(SERVICE)
    repository = _text(MUTATION_REPOSITORY)
    normal_runtime = "\n".join((router, service, repository))

    # The secure mutation repository intentionally exposes a result dataclass named
    # CreatedOrganizationRegistration.  Pin the actual legacy ORM import rather than
    # rejecting that safe type merely because its name contains the model suffix.
    for source in (router, service, repository):
        assert "OrganizationRegistration" not in _imports_from(
            source,
            "app.models.organization",
        )

    for forbidden in (
        "encrypt_data(",
        "decrypt_data(",
        "Fernet(",
        "SECRET_KEY",
        "id_number_encrypted",
        "public.organization_registration_payloads_secure",
        "public.encryption_key_registry",
    ):
        assert forbidden not in normal_runtime


def test_http_registration_paths_use_certified_secure_services_after_p3c_cutover() -> None:
    router = _text(ROUTER)
    atomic_service = _text(ATOMIC_SERVICE)

    # Standalone registration creation remains a direct P3B HTTP operation.
    assert "create_secure_organization_registration(" in router
    assert "list_current_organization_registrations(" in router

    # P3C deliberately removes direct replacement from the router. Both P3B secure
    # mutation services are composed by the single P3C transaction owner instead.
    assert "mutate_organization_profile_atomically(" in router
    assert "replace_secure_organization_registration(" not in router
    assert "create_secure_organization_registration(" in atomic_service
    assert "replace_secure_organization_registration(" in atomic_service

    assert "RegistrationCreate(" in router
    assert "mask_id_number(identifier)" in router


def test_http_maps_only_explicit_registration_boundary_failures() -> None:
    router = _text(ROUTER)
    for error_name in (
        "RegistrationMutationAuthorizationError",
        "RegistrationMutationValidationError",
        "RegistrationKeyStateError",
        "RegistrationConflictError",
        "RegistrationTargetNotFoundError",
        "RegistrationKeyAuthorizationError",
        "AWSKMSUnavailableError",
        "AWSKMSContractError",
        "RegistrationKMSConfigurationError",
    ):
        assert error_name in router
    assert "except RuntimeError" not in router
    assert "except Exception" not in router
    assert "Organization registration encryption service is unavailable" in router


def test_p3c_composes_p3b_mutations_without_router_owned_transaction() -> None:
    router = _text(ROUTER)
    tree = ast.parse(router, filename=str(ROUTER))
    patch = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "update_org_profile"
    )
    assert patch.end_lineno is not None
    patch_source = "".join(
        router.splitlines(keepends=True)[patch.lineno - 1 : patch.end_lineno]
    )
    atomic_service = _text(ATOMIC_SERVICE)

    assert "mutate_organization_profile_atomically(" in patch_source
    assert "await db.commit()" not in patch_source
    assert "await db.rollback()" not in patch_source
    assert "create_secure_organization_registration(" not in patch_source
    assert "replace_secure_organization_registration(" not in patch_source

    assert "async with session.begin():" in atomic_service
    assert "create_secure_organization_registration(" in atomic_service
    assert "replace_secure_organization_registration(" in atomic_service
    assert "update_current_organization_profile(" in atomic_service


def test_legacy_decrypt_is_confined_to_explicit_one_time_backfill_command() -> None:
    router = _text(ROUTER)
    service = _text(SERVICE)
    backfill = _text(BACKFILL)
    assert "decrypt_data" not in router
    assert "decrypt_data" not in service
    assert "decrypt_data" in backfill
    assert "One-time P3B legacy registration re-encryption command" in backfill


def test_router_registration_helpers_do_not_accept_tenant_or_principal_ids() -> None:
    tree = ast.parse(_text(ROUTER), filename=str(ROUTER))
    targets = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in {
            "add_registration",
            "update_org_profile",
            "_registration_material",
        }
    }
    assert set(targets) == {"add_registration", "update_org_profile", "_registration_material"}
    for node in targets.values():
        argument_names = {
            arg.arg for arg in (*node.args.args, *node.args.kwonlyargs)
        }
        for forbidden in ("org_id", "tenant_id", "user_id", "principal_type", "gym_id"):
            assert forbidden not in argument_names
