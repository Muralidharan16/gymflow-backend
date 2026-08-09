from __future__ import annotations

from pathlib import Path


MIGRATION = Path(
    "alembic/versions/4d5e6f708192_establish_audit_principal_registry.py"
)


def _source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_revision_extends_current_head_without_owning_infrastructure_extensions() -> None:
    source = _source()

    assert 'revision = "4d5e6f708192"' in source
    assert 'down_revision = "3c4d5e6f7081"' in source
    assert "CREATE EXTENSION" not in source
    assert "DROP EXTENSION" not in source


def test_registry_is_typed_non_pii_and_append_only() -> None:
    source = _source()

    assert '"audit_principals"' in source
    assert '"principal_id"' in source
    assert '"org_id"' in source
    assert '"principal_type"' in source
    assert 'sa.Column("email"' not in source
    assert 'sa.Column("phone"' not in source
    assert "SELECT id, org_id, :principal_type" in source
    assert "audit_principals is append-only" in source
    assert "trg_audit_principals_immutable" in source


def test_all_supported_identity_domains_register_at_the_database_boundary() -> None:
    source = _source()

    for table_name, principal_type in (
        ("owners", "owner"),
        ("organization_users", "organization_user"),
        ("gym_owners", "legacy_gym_owner"),
    ):
        assert f'("{table_name}", "{principal_type}")' in source

    assert "trg_register_audit_principal_{table_name}" in source
    assert "app_private.register_audit_principal" in source
    assert "app_private.prevent_principal_identity_reassignment" in source


def test_registry_is_not_a_runtime_privilege_escalation_surface() -> None:
    source = _source().lower()

    assert "revoke all on table public.audit_principals from public" in source
    assert (
        "grant select, insert on table public.audit_principals to app_security_owner"
        in source
    )
    assert "grant all" not in source
    assert "audit_principals to app_runtime" not in source
    assert "audit_principals to app_user" not in source
    without_revoke = source.replace(
        "revoke all on table public.audit_principals from public", ""
    )
    assert "audit_principals to public" not in without_revoke


def test_address_audit_references_typed_principal_and_tenant_together() -> None:
    source = _source()

    for token in (
        "changed_by_type",
        "deleted_by_type",
        "fk_branch_address_history_audit_principal",
        "fk_branch_address_audit_audit_principal",
        "fk_organization_addresses_deleted_audit_principal",
        "FOREIGN KEY ({columns}) REFERENCES public.audit_principals",
        "(principal_id, org_id, principal_type)",
        "app.current_principal_type",
    ):
        assert token in source

    assert "NOT VALID" in source
    assert "VALIDATE CONSTRAINT" in source


def test_legacy_untyped_audit_rows_are_never_guessed() -> None:
    source = _source()

    assert "missing or ambiguous" in source
    assert "Reconcile the data explicitly before upgrading" in source
    assert "Existing branch_address_audit_log actor UUIDs" in source


def test_downgrade_refuses_to_discard_new_actor_provenance() -> None:
    source = _source()

    assert "Downgrade would discard typed audit-principal provenance" in source
    assert "changed_by_type <> 'legacy_gym_owner'" in source
    assert "deleted_by_type <> 'legacy_gym_owner'" in source


def test_private_trigger_functions_are_security_definer_with_fixed_search_path() -> None:
    source = _source()

    assert source.count("SECURITY DEFINER") == 3
    assert source.count("SET search_path = pg_catalog") == 3
    assert (
        "REVOKE ALL ON FUNCTION app_private.register_audit_principal() FROM PUBLIC"
        in source
    )
