from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "app/routers/organizations.py"
SERVICE = ROOT / "app/services/organization_registration_service.py"
MUTATION_REPOSITORY = ROOT / "app/repositories/organization_registration_mutations.py"
BACKFILL = ROOT / "scripts/p3b_backfill_legacy_registrations.py"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_normal_http_registration_path_has_no_legacy_orm_or_fernet_crypto() -> None:
    router = _text(ROUTER)
    service = _text(SERVICE)
    repository = _text(MUTATION_REPOSITORY)
    normal_runtime = "\n".join((router, service, repository))

    for forbidden in (
        "OrganizationRegistration",
        "encrypt_data",
        "decrypt_data",
        "Fernet",
        "SECRET_KEY",
        "id_number_encrypted",
        "public.organization_registration_payloads_secure",
        "public.encryption_key_registry",
    ):
        assert forbidden not in normal_runtime


def test_http_post_and_profile_registration_updates_use_secure_services() -> None:
    router = _text(ROUTER)
    assert "create_secure_organization_registration(" in router
    assert "replace_secure_organization_registration(" in router
    assert "list_current_organization_registrations(" in router
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


def test_profile_and_registration_mutations_remain_separate_p3c_boundary() -> None:
    router = _text(ROUTER)
    patch_source = router.split("async def update_org_profile", 1)[1]
    assert "P3C alone owns cross-domain atomic composition" in patch_source
    profile_commit = patch_source.index("await db.commit()")
    registration_service = min(
        index for index in (
            patch_source.find("create_secure_organization_registration("),
            patch_source.find("replace_secure_organization_registration("),
        )
        if index >= 0
    )
    assert profile_commit < registration_service


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
