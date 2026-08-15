from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = ROOT / "app/repositories/registration_keys.py"


def _source() -> str:
    return REPOSITORY.read_text(encoding="utf-8")


def test_repository_uses_only_registration_specific_app_secure_capabilities() -> None:
    source = _source()

    assert "app_secure.current_registration_dek()" in source
    assert "app_secure.install_registration_dek(:encrypted_dek, :wrapping_key_id)" in source
    assert "app_secure.lookup_registration_dek(:key_version)" in source
    assert "public.encryption_key_registry" not in source
    assert "encryption_key_registry_key_version_seq" not in source


def test_repository_never_accepts_tenant_or_principal_identifiers() -> None:
    source = _source()
    tree = ast.parse(source, filename=str(REPOSITORY))

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names = {
                argument.arg
                for argument in (*node.args.args, *node.args.kwonlyargs)
            }
            assert "org_id" not in names
            assert "tenant_id" not in names
            assert "user_id" not in names
            assert "principal_type" not in names
            assert "role" not in names


def test_repository_keeps_wrapped_key_and_exact_kms_identity_together() -> None:
    source = _source()

    assert "class RegistrationDEK" in source
    assert "key_version: int" in source
    assert "encrypted_dek: bytes" in source
    assert "wrapping_key_id: str" in source
    assert 'mapping["key_version"]' in source
    assert 'mapping["encrypted_dek"]' in source
    assert 'mapping["wrapping_key_id"]' in source


def test_repository_maps_only_database_authorization_failures() -> None:
    source = _source()

    assert 'if _sqlstate(exc) == "42501"' in source
    assert "RegistrationKeyAuthorizationError" in source
    assert "except DBAPIError as exc:" in source
    assert "raise" in source
    assert "str(exc)" not in source
