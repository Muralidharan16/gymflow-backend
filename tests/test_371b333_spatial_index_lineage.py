from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
M329 = ROOT / "alembic/versions/371b1a44a329_add_address_type_column.py"
M333 = ROOT / "alembic/versions/371b1a44a333_add_spatial_and_filtering_indexes.py"
M00F = ROOT / "alembic/versions/00f277c748ea_add_hyperscale_branch_name_and_address_.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _function_source(path: Path, name: str) -> str:
    source = _source(path)
    tree = ast.parse(source, filename=str(path))
    node = next(
        item
        for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == name
    )
    lines = source.splitlines()
    return "\n".join(lines[node.lineno - 1 : node.end_lineno])


def test_329_geography_columns_are_the_authoritative_spatial_index_creator() -> None:
    source = _source(M329)
    assert "geoalchemy2.types.Geography" in source
    assert "sa.Column('coordinates', coord_col_type" in source
    assert source.count("sa.Column('coordinates', coord_col_type") == 2


def test_333_does_not_recreate_or_drop_geoalchemy_spatial_indexes() -> None:
    source = _source(M333)
    upgrade = _function_source(M333, "upgrade")
    downgrade = _function_source(M333, "downgrade")

    for name in (
        "idx_organization_addresses_coordinates",
        "idx_member_addresses_coordinates",
    ):
        assert name in source
        assert f'op.create_index("{name}"' not in source
        assert f'op.drop_index("{name}"' not in source
        assert f"op.create_index('{name}'" not in source
        assert f"op.drop_index('{name}'" not in source

    assert "_require_spatial_predecessor_contract(bind)" in upgrade
    assert "_require_spatial_predecessor_contract(bind)" in downgrade


def test_333_forbids_the_redundant_abbreviated_org_spatial_index() -> None:
    source = _source(M333)
    assert '"idx_org_addresses_coordinates"' not in source
    assert "public.idx_org_addresses_coordinates" in source
    assert "Redundant spatial index" in source


def test_333_validates_exact_gist_column_index_contract() -> None:
    helper = _function_source(M333, "_require_canonical_spatial_index")
    for token in (
        "pg_catalog.pg_index",
        "pg_catalog.pg_am",
        '"access_method": "gist"',
        '"is_valid": True',
        '"is_ready": True',
        '"is_unique": False',
        '"has_no_predicate": True',
        '"has_no_expressions": True',
        '"indexed_columns": ["coordinates"]',
    ):
        assert token in helper


def test_333_still_owns_only_its_filtering_indexes() -> None:
    source = _source(M333)
    upgrade = _function_source(M333, "upgrade")
    downgrade = _function_source(M333, "downgrade")

    for name in (
        "idx_org_addresses_country_state",
        "idx_org_addresses_city",
    ):
        assert name in upgrade
        assert name in downgrade

    assert upgrade.count("op.create_index(") == 2
    assert downgrade.count("op.drop_index(") == 2


def test_00f_preserves_371b_spatial_predecessor_contract_in_place() -> None:
    upgrade = _function_source(M00F, "upgrade")
    downgrade = _function_source(M00F, "downgrade")

    # 00f is now an expand/compatibility migration.  The 371b-owned Geography
    # column and canonical GiST index must survive both directions in place;
    # 00f must neither replace them with VARCHAR storage nor recreate/drop them.
    for function_source in (upgrade, downgrade):
        assert "DROP COLUMN coordinates" not in function_source
        assert "ADD COLUMN coordinates" not in function_source
        assert "idx_organization_addresses_coordinates" not in function_source
        assert "sa.Column('coordinates', sa.VARCHAR(length=255)" not in function_source
        assert 'sa.Column("coordinates", sa.String(length=255)' not in function_source
