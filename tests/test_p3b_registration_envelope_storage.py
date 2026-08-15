from __future__ import annotations

import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/d07d8e9f0a24_p3b_registration_envelope_storage.py"


def _source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    source = _source()
    tree = ast.parse(source, filename=str(MIGRATION))
    node = next(
        item
        for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == name
    )
    assert node.end_lineno is not None
    return "".join(source.splitlines(keepends=True)[node.lineno - 1 : node.end_lineno])


def test_envelope_storage_is_single_expand_step_after_p3b_read_boundary() -> None:
    source = _source()
    assert 'revision = "d07d8e9f0a24"' in source
    assert 'down_revision = "c97d8e9f0a23"' in source
    assert "new ciphertext is deliberately stored in a separate FORCE-RLS" in source


def test_catalog_attestation_does_not_require_app_secure_schema_usage() -> None:
    source = _source()
    function_exists = _function_source("_function_exists")
    marker_lookup = _function_source("_marker_function_row")

    assert "pg_catalog.to_regprocedure(" not in source
    for function_source in (function_exists, marker_lookup):
        assert "pg_catalog.pg_proc" in function_source
        assert "pg_catalog.pg_namespace" in function_source
        assert "namespace_data.nspname = 'app_secure'" in function_source
        assert "procedure_data.proname" in function_source
        assert "procedure_data.pronargs = 0" in function_source

    assert "GRANT USAGE ON SCHEMA app_secure TO migration_owner" not in source


def test_new_envelope_ciphertext_never_lands_in_legacy_registration_relation() -> None:
    upgrade = _function_source("upgrade")

    assert "ADD COLUMN crypto_version smallint NOT NULL DEFAULT 0" in upgrade
    assert "ALTER COLUMN id_number_encrypted DROP NOT NULL" in upgrade
    assert "ALTER COLUMN id_number_masked TYPE varchar(50)" in upgrade
    assert "payload_encrypted bytea NOT NULL" in upgrade
    assert "key_version integer NOT NULL" in upgrade
    assert "organization_registration_payloads_secure" in upgrade

    metadata_ddl = upgrade.split(
        "CREATE TABLE public.organization_registration_payloads_secure", 1
    )[0]
    assert "payload_encrypted" not in metadata_ddl
    assert "id_number_ciphertext" not in metadata_ddl


def test_metadata_constraints_pin_crypto_format_and_business_uniqueness() -> None:
    source = _source()

    assert "ck_org_reg_crypto_material" in source
    assert "crypto_version = 0 AND id_number_encrypted IS NOT NULL" in source
    assert "crypto_version = 1 AND id_number_encrypted IS NULL" in source
    assert "ck_org_reg_canonical_identity" in source
    assert "id_type = pg_catalog.upper(pg_catalog.btrim(id_type))" in source
    assert "country_code = pg_catalog.upper(pg_catalog.btrim(country_code))" in source
    assert "UNIQUE (org_id, country_code, id_type)" in source
    assert "UNIQUE (id, org_id)" in source


def test_secure_payload_has_tenant_binding_key_fk_and_forced_rls() -> None:
    upgrade = _function_source("upgrade")

    assert "FOREIGN KEY (registration_id, tenant_id)" in upgrade
    assert "REFERENCES public.organization_registrations (id, org_id)" in upgrade
    assert "FOREIGN KEY (key_version)" in upgrade
    assert "REFERENCES public.encryption_key_registry (key_version)" in upgrade
    assert "ON DELETE RESTRICT" in upgrade
    assert "ENABLE ROW LEVEL SECURITY" in upgrade
    assert "FORCE ROW LEVEL SECURITY" in upgrade
    assert "p3b_tenant_isolation_registration_payloads_secure" in upgrade
    assert "app.current_org_id" in upgrade


def test_secure_payload_header_must_match_key_version_fk() -> None:
    source = _source()
    forward = _function_source("_require_forward")

    assert "ck_org_reg_payload_envelope_key_version" in source
    assert "pg_catalog.octet_length(payload_encrypted) >= 32" in source
    assert "pg_catalog.get_byte(payload_encrypted, 0)::bigint * 16777216" in source
    assert "pg_catalog.get_byte(payload_encrypted, 1)::bigint * 65536" in source
    assert "pg_catalog.get_byte(payload_encrypted, 2)::bigint * 256" in source
    assert "pg_catalog.get_byte(payload_encrypted, 3)::bigint" in source
    assert ") = key_version::bigint" in source
    assert "P3B secure registration payload envelope check is missing" in forward


def test_expand_step_adds_no_runtime_or_security_owner_payload_acl() -> None:
    source = _source()
    upper = re.sub(r"\s+", " ", source).upper()

    for role in ("APP_RUNTIME", "AUTH_RUNTIME", "APP_SECURITY_OWNER"):
        assert not re.search(
            rf"GRANT\s+(?:SELECT|INSERT|UPDATE|DELETE|TRUNCATE|ALL).*?"
            rf"ORGANIZATION_REGISTRATION_PAYLOADS_SECURE.*?TO\s+{role}",
            upper,
            flags=re.DOTALL,
        )
        assert not re.search(
            rf"GRANT\s+(?:SELECT|INSERT|UPDATE|DELETE|TRUNCATE|ALL).*?"
            rf"P3B_REGISTRATION_ENVELOPE_ROWS.*?TO\s+{role}",
            upper,
            flags=re.DOTALL,
        )

    for forbidden in (
        "DISABLE ROW LEVEL SECURITY",
        "NO FORCE ROW LEVEL SECURITY",
        "ROW_SECURITY = OFF",
        "GRANT ALL",
        "OWNER TO APP_RUNTIME",
        "OWNER TO APP_SECURITY_OWNER",
    ):
        assert forbidden not in upper

    assert not re.search(
        r"\b(?:CREATE|ALTER)\s+ROLE\s+[^;]*\bBYPASSRLS\b",
        upper,
        flags=re.DOTALL,
    )


def test_downgrade_loss_detection_does_not_bypass_forced_tenant_relations() -> None:
    predecessor = _function_source("_require_predecessor")
    downgrade = _function_source("downgrade")

    assert "SELECT 1 FROM public.organization_registrations" not in predecessor
    assert "SELECT 1 FROM public.organization_registration_payloads_secure" not in downgrade
    assert "SELECT EXISTS (SELECT 1 FROM public.p3b_registration_envelope_rows)" in downgrade
    assert "ALTER COLUMN id_number_masked TYPE varchar(20)" in downgrade
    assert "ALTER COLUMN id_number_encrypted SET NOT NULL" in downgrade
    assert downgrade.index("ALTER COLUMN id_number_encrypted SET NOT NULL") < downgrade.index(
        "DROP TABLE public.organization_registration_payloads_secure"
    )
    assert "CASCADE" not in downgrade


def test_migration_owner_only_marker_tracks_secure_payload_transactionally() -> None:
    upgrade = _function_source("upgrade")

    assert "CREATE TABLE public.p3b_registration_envelope_rows" in upgrade
    assert "FOREIGN KEY (registration_id)" in upgrade
    assert "REFERENCES public.organization_registration_payloads_secure" in upgrade
    assert "ON UPDATE CASCADE" in upgrade
    assert "ON DELETE CASCADE" in upgrade
    assert "CREATE FUNCTION app_secure.track_registration_envelope_row()" in upgrade
    assert "SECURITY DEFINER" in upgrade
    assert "SET search_path = pg_catalog" in upgrade
    assert "SET row_security = on" in upgrade
    assert (
        "REVOKE ALL ON FUNCTION app_secure.track_registration_envelope_row() FROM PUBLIC"
        in " ".join(upgrade.split())
    )
    assert "AFTER INSERT ON public.organization_registration_payloads_secure" in upgrade
    assert "INSERT INTO public.p3b_registration_envelope_rows" in upgrade
    assert "ON CONFLICT (registration_id) DO NOTHING" in upgrade
