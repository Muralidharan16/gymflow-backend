from __future__ import annotations

import ast
from pathlib import Path


MIGRATION = Path(
    "alembic/versions/4d5e6f708192_establish_audit_principal_registry.py"
)


def _source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _tree() -> ast.Module:
    return ast.parse(_source(), filename=str(MIGRATION))


def _function_source(name: str) -> str:
    source = _source()
    lines = source.splitlines(keepends=True)
    for node in _tree().body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            assert node.end_lineno is not None
            return "".join(lines[node.lineno - 1 : node.end_lineno])
    raise AssertionError(f"Missing function {name}")


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
    function_builder = _function_source("_create_private_functions")

    for function_name in (
        "register_audit_principal",
        "prevent_principal_identity_reassignment",
        "prevent_audit_principal_mutation",
    ):
        marker = f"CREATE FUNCTION app_private.{function_name}()"
        assert marker in function_builder
        block = function_builder[function_builder.index(marker) :]
        next_create = block.find("CREATE FUNCTION app_private.", len(marker))
        if next_create != -1:
            block = block[:next_create]
        assert "SECURITY DEFINER" in block
        assert "SET search_path = pg_catalog" in block

    assert "REVOKE ALL ON FUNCTION {signature} FROM PUBLIC" in function_builder


def test_trigger_creation_uses_owner_grant_window_and_proves_direct_final_acl() -> None:
    source = _source()
    grant = _function_source("_grant_trigger_creation_execute")
    revoke = _function_source("_revoke_trigger_creation_execute")
    create = _function_source("_create_principal_triggers")
    verify = _function_source("_require_private_function_security_contract")

    # Only the private-function owner grants/revokes; migration_owner remains the
    # table owner that issues CREATE TRIGGER.
    assert "_as_security_owner(" in grant
    assert 'f"GRANT EXECUTE ON FUNCTION {signature} TO {_MIGRATION_OWNER}"' in grant
    assert "_as_security_owner(" in revoke
    assert 'f"REVOKE EXECUTE ON FUNCTION {signature} FROM {_MIGRATION_OWNER}"' in revoke

    grant_index = create.index("_grant_trigger_creation_execute(bind)")
    first_trigger_index = create.index("CREATE TRIGGER")
    revoke_index = create.index("_revoke_trigger_creation_execute(bind)")
    verify_index = create.index("_require_private_function_security_contract(bind)")
    assert grant_index < first_trigger_index < revoke_index < verify_index

    # Final-state proof uses direct ACL inspection, not effective privileges that
    # could be inherited through a role edge.
    assert "pg_catalog.aclexplode" in verify
    assert "pg_catalog.acldefault" in verify
    assert "pg_catalog.has_function_privilege" not in verify
    assert "migration_owner_direct_execute" in verify
    assert "public_execute" in verify
    assert 'row["owner_name"] != _SECURITY_OWNER' in verify
    assert 'row["configuration"] != ["search_path=pg_catalog"]' in verify

    assert "SET LOCAL ROLE app_security_owner" in source
    assert "RESET ROLE" in source


def test_failed_trigger_creation_cannot_leave_temporary_privilege_committed() -> None:
    create = _function_source("_create_principal_triggers")
    source = _source()

    assert "same transaction" in create
    assert "Any failure rolls the" in create
    assert "privilege window back" in create
    assert "_grant_trigger_creation_execute(bind)" in create
    assert "_revoke_trigger_creation_execute(bind)" in create
    assert "Temporary direct migration_owner EXECUTE was not revoked" in source
