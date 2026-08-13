from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
M00F = ROOT / "alembic/versions/00f277c748ea_add_hyperscale_branch_name_and_address_.py"
INSERT_TOKEN = "INSERT INTO public.branch_geolocation_state ("


def _source() -> str:
    return M00F.read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    source = _source()
    tree = ast.parse(source, filename=str(M00F))
    node = next(
        item
        for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == name
    )
    lines = source.splitlines()
    return "\n".join(lines[node.lineno - 1 : node.end_lineno])


def test_00f_geolocation_projection_has_one_revision_owner() -> None:
    source = _source()
    backfill = _function_source("_backfill_legacy_addresses")
    upgrade = _function_source("upgrade")

    assert source.count(INSERT_TOKEN) == 1
    assert backfill.count(INSERT_TOKEN) == 1
    assert upgrade.count(INSERT_TOKEN) == 0
    assert upgrade.count("_backfill_legacy_addresses()") == 1


def test_00f_single_projection_retains_exact_cardinality_postcondition() -> None:
    backfill = _function_source("_backfill_legacy_addresses")
    assert "00f geolocation backfill did not create one state row per address" in backfill
    assert "SELECT count(*) FROM public.branch_geolocation_state" in backfill
    assert "SELECT count(*) FROM public.organization_addresses" in backfill
