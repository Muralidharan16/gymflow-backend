from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
M16C = ROOT / "alembic/versions/16c65fdfd9a8_init_geo_infrastructure.py"


def _source() -> str:
    return M16C.read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    source = _source()
    tree = ast.parse(source, filename=str(M16C))
    node = next(
        item
        for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == name
    )
    lines = source.splitlines()
    return "\n".join(lines[node.lineno - 1 : node.end_lineno])


def test_16c_consumes_infrastructure_and_predecessor_objects_without_owning_them() -> None:
    source = _source()
    upgrade = _function_source("upgrade")
    downgrade = _function_source("downgrade")

    assert "_require_infrastructure_citext(bind)" in source
    assert "_require_predecessor_set_updated_at(bind)" in source
    assert "CREATE EXTENSION" not in upgrade
    assert "CREATE OR REPLACE FUNCTION set_updated_at" not in upgrade
    assert "DROP FUNCTION" not in downgrade
    assert "DROP EXTENSION" not in downgrade
    assert "DROP TYPE public.citext" not in downgrade


def test_16c_has_fail_closed_full_geo_inverse() -> None:
    source = _source()
    downgrade = _function_source("downgrade")

    assert "_preflight(bind, direction=\"downgrade\")" in downgrade
    assert "_postflight(bind, direction=\"downgrade\")" in downgrade
    assert "CASCADE" not in downgrade.replace(
        "No\n    # CASCADE/IF EXISTS is used", ""
    )
    assert "IF EXISTS" not in downgrade

    expected_drop_order = [
        "geo_quarantined_records",
        "geo_import_jobs",
        "geo_raw_import_files",
        "geo_postal_overrides",
        "geo_audit_log",
        "city_name_aliases",
        "subdivision_name_aliases",
        "country_name_aliases",
        "postal_codes",
        "cities",
        "subdivisions",
        "countries",
    ]
    positions = [
        downgrade.index(f"DROP TABLE public.{table_name};")
        for table_name in expected_drop_order
    ]
    assert positions == sorted(positions)

    assert "DROP TYPE public.geo_import_status;" in downgrade
    assert "DROP TYPE public.geo_record_status;" in downgrade


def test_16c_requires_exact_owned_surface_and_reduced_migration_identity() -> None:
    source = _source()
    assert "16c geo migration requires session_user=migration_owner" in source
    assert "migration_owner is over-privileged for 16c geo migration" in source
    assert "16c geo relation collision before upgrade" in source
    assert "16c geo relations missing from owned surface" in source
    assert "16c geo relation ownership drift" in source
    assert "16c geo enum contract drift" in source
    assert "16c set_updated_at trigger inventory drift" in source
