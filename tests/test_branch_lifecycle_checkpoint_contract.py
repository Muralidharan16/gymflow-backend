from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/93a4b5c6d7e8_align_durable_saga_checkpoint_contract.py"
SERVICE = ROOT / "app/services/branch_lifecycle_service.py"

PREDECESSOR = {
    "search_deindexed",
    "bookings_cancelled",
    "refunds_initiated",
    "refunds_completed",
    "notifications_sent",
    "compensation_initiated",
    "compensation_completed",
}
DURABLE = {
    "transaction_b_started",
    "bookings_processed",
    "refunds_queued",
    "notifications_queued",
}


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _tree(path: Path) -> ast.Module:
    return ast.parse(_source(path), filename=str(path))


def _assignment(path: Path, name: str):
    for node in _tree(path).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"missing assignment {name}")


def _function(path: Path, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    return next(
        node
        for node in _tree(path).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    )


def test_checkpoint_migration_is_single_head_successor() -> None:
    assert _assignment(MIGRATION, "revision") == "93a4b5c6d7e8"
    assert _assignment(MIGRATION, "down_revision") == "8293a4b5c6d7"


def test_checkpoint_vocabularies_are_exact_and_non_overlapping() -> None:
    assert set(_assignment(MIGRATION, "_PREDECESSOR_CHECKPOINTS")) == PREDECESSOR
    assert set(_assignment(MIGRATION, "_DURABLE_CHECKPOINTS")) == DURABLE
    assert PREDECESSOR.isdisjoint(DURABLE)


def test_service_persists_only_the_durable_checkpoint_vocabulary() -> None:
    method = _function(SERVICE, "execute_saga_cascade")
    observed: set[str] = set()
    for node in ast.walk(method):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "_record_checkpoint" or len(node.args) < 2:
            continue
        checkpoint = node.args[1]
        assert isinstance(checkpoint, ast.Constant) and isinstance(checkpoint.value, str)
        observed.add(checkpoint.value)

    assert observed == DURABLE


def test_legacy_false_success_checkpoint_names_do_not_return_to_service() -> None:
    source = _source(SERVICE)
    for checkpoint in (
        "refunds_initiated",
        "refunds_completed",
        "notifications_sent",
        "search_deindexed",
        "bookings_cancelled",
    ):
        assert f'"{checkpoint}"' not in source


def test_checkpoint_migration_refuses_unresolved_state_both_directions() -> None:
    source = _source(MIGRATION)
    upgrade = ast.get_source_segment(source, _function(MIGRATION, "upgrade")) or ""
    downgrade = ast.get_source_segment(source, _function(MIGRATION, "downgrade")) or ""

    assert '_require_no_inflight_checkpoint(bind, "legacy")' in upgrade
    assert '_require_no_inflight_checkpoint(bind, "durable")' in downgrade
    assert "Resolve/compensate the saga first" in source
    assert "sample_branch_ids" in source


def test_checkpoint_migration_changes_no_security_boundary() -> None:
    source = _source(MIGRATION).upper()
    forbidden = (
        "GRANT ",
        "REVOKE ",
        "CREATE POLICY",
        "DROP POLICY",
        "ENABLE ROW LEVEL SECURITY",
        "DISABLE ROW LEVEL SECURITY",
        "FORCE ROW LEVEL SECURITY",
        "NO FORCE ROW LEVEL SECURITY",
        "ALTER ROLE",
        "SET ROLE",
        "SET LOCAL ROLE",
        "BYPASSRLS",
        "SECURITY DEFINER",
        "SECURITY INVOKER",
        "CASCADE",
    )
    executable = "\n".join(
        value.value.upper()
        for value in ast.walk(_tree(MIGRATION))
        if isinstance(value, ast.Constant) and isinstance(value.value, str)
    )
    for token in forbidden:
        assert token not in executable


def test_constraint_replacement_is_exact_and_non_destructive() -> None:
    source = _source(MIGRATION)
    replace = ast.get_source_segment(source, _function(MIGRATION, "_replace_constraint")) or ""

    assert "DROP CONSTRAINT" in replace
    assert "ADD CONSTRAINT" in replace
    assert "saga_last_checkpoint IS NULL" in replace
    assert "saga_last_checkpoint IN" in replace
    assert "UPDATE " not in replace.upper()
    assert "DELETE " not in replace.upper()
    assert "TRUNCATE " not in replace.upper()
