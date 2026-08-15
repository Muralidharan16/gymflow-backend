from __future__ import annotations

import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMMAND = ROOT / "scripts/p3b_backfill_legacy_registrations.py"


def _source() -> str:
    return COMMAND.read_text(encoding="utf-8")


def test_backfill_requires_explicit_principal_manifest_and_reduced_runtime_login() -> None:
    source = _source()
    assert '"org_id", "user_id", "principal_type", "role"' in source
    assert "manifest contains duplicate organization" in source
    assert "pg_has_role(session_user, 'app_runtime', 'MEMBER')" in source
    assert "role_data.rolbypassrls" in source
    assert 'row[0] in {"migration_owner", "app_security_owner"}' in source
    assert "app.current_gym_id" in source
    assert '("app.current_gym_id", "")' in source


def test_backfill_reads_and_writes_only_through_p3b_capabilities() -> None:
    source = _source()
    assert "app_secure.current_legacy_registration_backfill_rows()" in source
    assert "app_secure.current_registration_dek()" in source
    assert "app_secure.install_registration_dek(%s, %s)" in source
    assert "app_secure.convert_legacy_organization_registration_envelope" in source
    for forbidden in (
        "FROM public.organization_registrations",
        "INSERT INTO public.organization_registrations",
        "UPDATE public.organization_registrations",
        "organization_registration_payloads_secure",
        "encryption_key_registry",
    ):
        assert forbidden not in source


def test_backfill_uses_legacy_decrypt_only_inside_one_time_command() -> None:
    source = _source()
    assert "from app.utils.encryption import decrypt_data, mask_id_number" in source
    assert "identifier = decrypt_data(legacy.encrypted_identifier)" in source
    assert "mask_id_number(identifier) != legacy.masked_identifier" in source
    assert "RegistrationCreate(" in source
    assert "fails current validation" in source


def test_backfill_reencrypts_with_registration_domain_aad_and_zeroizes_dek() -> None:
    source = _source()
    assert "encrypt_registration_identifier(" in source
    assert "tenant_id=principal.org_id" in source
    assert "registration_id=legacy.id" in source
    assert "key_version=key_version" in source
    assert "finally:" in source
    assert "zeroize_key(raw_dek)" in source


def test_backfill_never_logs_plaintext_ciphertext_or_key_material() -> None:
    source = _source()
    tree = ast.parse(source, filename=str(COMMAND))
    print_calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "print"
    ]
    assert len(print_calls) == 2
    rendered = [ast.unparse(call) for call in print_calls]
    assert all("principal.org_id" in item or "converted={total}" in item for item in rendered)
    for secret_name in (
        "identifier",
        "encrypted_identifier",
        "wrapped_dek",
        "raw_dek",
        "payload",
        "wrapping_key_id",
    ):
        assert all(secret_name not in item for item in rendered)


def test_backfill_has_no_tenant_discovery_or_owner_escalation_sql() -> None:
    upper = _source().upper()
    for forbidden in (
        "SELECT * FROM PUBLIC.ORGANIZATIONS",
        "SELECT ORG_ID FROM PUBLIC.ORGANIZATIONS",
        "SET ROLE APP_SECURITY_OWNER",
        "SET ROLE MIGRATION_OWNER",
        "ROW_SECURITY = OFF",
    ):
        assert forbidden not in upper
    assert not re.search(
        r"\b(?:CREATE|ALTER)\s+ROLE\s+[^;]*\bBYPASSRLS\b",
        upper,
        flags=re.DOTALL,
    )
