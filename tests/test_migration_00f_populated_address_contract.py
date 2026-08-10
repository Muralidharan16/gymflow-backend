from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "00f277c748ea_add_hyperscale_branch_name_and_address_.py"
)


def _source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_00f_never_deletes_legacy_addresses_to_satisfy_branch_id() -> None:
    source = _source()

    assert "TRUNCATE TABLE organization_addresses" not in source
    assert "DELETE FROM public.organization_addresses" not in source
    assert "DELETE FROM organization_addresses" not in source


def test_00f_keeps_force_rls_and_uses_tenant_scoped_backfill() -> None:
    source = _source()

    assert "set_config(\n                    'app.current_org_id'" in source
    assert "DISABLE ROW LEVEL SECURITY" not in source
    assert "NO FORCE ROW LEVEL SECURITY" not in source
    assert "SET row_security = off" not in source
    assert "SET row_security TO off" not in source


def test_00f_installs_composite_fk_before_populated_backfill() -> None:
    source = _source()

    branch_column = source.index(
        "op.add_column('organization_addresses', sa.Column('branch_id', sa.UUID(), nullable=True))"
    )
    composite_fk = source.index(
        "op.create_foreign_key(\n"
        "        _BRANCH_ORG_FK,\n"
        "        'organization_addresses',\n"
        "        'org_branches',\n"
        "        ['branch_id', 'org_id'],\n"
        "        ['id', 'org_id'],"
    )
    backfill = source.index("    _backfill_legacy_addresses()")
    not_null = source.index(
        "op.alter_column(\n"
        "        'organization_addresses',\n"
        "        'branch_id',\n"
        "        existing_type=sa.UUID(),\n"
        "        nullable=False,"
    )

    assert branch_column < composite_fk < backfill < not_null
    assert "op.create_foreign_key(None, 'organization_addresses', 'org_branches', ['branch_id'], ['id']" not in source


def test_00f_composite_fk_is_explicit_validated_and_reversible() -> None:
    source = _source()

    assert "_BRANCH_ORG_FK = 'fk_org_addresses_branch_org'" in source
    assert "constraint_data.conname = 'fk_org_addresses_branch_org'" in source
    assert "constraint_data.convalidated" in source
    assert (
        "FOREIGN KEY (branch_id, org_id) REFERENCES org_branches(id, org_id) ON DELETE RESTRICT"
        in source
    )
    assert (
        "op.drop_constraint(_BRANCH_ORG_FK, 'organization_addresses', "
        "type_='foreignkey', schema='public')"
        in source
    )
