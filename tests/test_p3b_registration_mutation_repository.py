from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = ROOT / "app/repositories/organization_registration_mutations.py"


def _source() -> str:
    return REPOSITORY.read_text(encoding="utf-8")


def test_mutation_repository_calls_only_bounded_database_capabilities() -> None:
    source = _source()

    assert "app_secure.create_organization_registration_envelope(" in source
    assert "app_secure.replace_organization_registration_envelope(" in source
    assert "public.organization_registrations" not in source
    assert "public.organization_registration_payloads_secure" not in source
    assert "encryption_key_registry" not in source
    assert "INSERT INTO" not in source.upper()
    assert "UPDATE " not in source.upper()
    assert "DELETE FROM" not in source.upper()


def test_mutation_repository_never_accepts_tenant_or_principal_identifiers() -> None:
    source = _source()
    tree = ast.parse(source, filename=str(REPOSITORY))

    targets = [
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name in {
            "create_organization_registration_envelope",
            "replace_organization_registration_envelope",
        }
    ]
    assert {target.name for target in targets} == {
        "create_organization_registration_envelope",
        "replace_organization_registration_envelope",
    }
    for target in targets:
        names = {
            argument.arg
            for argument in (*target.args.args, *target.args.kwonlyargs)
        }
        for forbidden in (
            "org_id",
            "tenant_id",
            "user_id",
            "principal_type",
            "role",
            "gym_id",
        ):
            assert forbidden not in names


def test_mutation_repository_maps_only_explicit_database_contract_states() -> None:
    source = _source()

    assert 'state == "42501"' in source
    assert "RegistrationMutationAuthorizationError" in source
    assert 'state == "22023"' in source
    assert "RegistrationMutationValidationError" in source
    assert 'state == "23503"' in source
    assert "RegistrationKeyStateError" in source
    assert 'state == "P0002"' in source
    assert "RegistrationTargetNotFoundError" in source
    assert 'state == "23505"' in source
    assert "RegistrationConflictError" in source
    assert "target or key" not in source
    assert "str(exc)" not in source


def test_mutation_repository_returns_masked_metadata_only() -> None:
    source = _source()

    assert "class CreatedOrganizationRegistration" in source
    assert "id_number_masked: str" in source
    dataclass_source = source.split("class CreatedOrganizationRegistration", 1)[1].split(
        "class RegistrationMutationAuthorizationError", 1
    )[0]
    for forbidden in (
        "id_number_encrypted:",
        "payload_encrypted:",
        "encrypted_dek:",
        "wrapping_key_id:",
    ):
        assert forbidden not in dataclass_source
