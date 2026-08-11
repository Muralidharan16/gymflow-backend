from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/8293a4b5c6d7_harden_lifecycle_history_correlation.py"
SERVICE = ROOT / "app/services/branch_lifecycle_service.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _literal_assignment(path: Path, name: str):
    module = ast.parse(_source(path), filename=str(path))
    for node in module.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"missing assignment {name}")


def test_correlation_hardening_is_a_single_head_successor() -> None:
    assert _literal_assignment(MIGRATION, "revision") == "8293a4b5c6d7"
    assert _literal_assignment(MIGRATION, "down_revision") == "718293a4b5c6"


def test_validator_uses_bounded_security_owner_capability() -> None:
    source = _source(MIGRATION)

    assert "app_security_owner" in source
    assert "NOLOGIN/NOINHERIT/NOBYPASSRLS" in source
    assert (
        "GRANT SELECT (branch_id, correlation_id, emitted_at) "
        "ON TABLE public.branch_lifecycle_events TO app_security_owner"
        in source
    )
    assert "CREATE POLICY lifecycle_correlation_validator_event_read" in source
    assert "FOR SELECT TO app_security_owner" in source
    assert "REVOKE CREATE ON SCHEMA public FROM app_security_owner" in source
    assert "8293 leaked public CREATE to app_security_owner" in source

    # The defect must not be repaired by expanding ordinary application or
    # worker event-read privileges.
    assert "GRANT SELECT ON TABLE public.branch_lifecycle_events TO app_runtime" not in source
    assert "GRANT SELECT ON TABLE public.branch_lifecycle_events TO worker_runtime" not in source
    assert "BYPASSRLS" not in source.replace("NOBYPASSRLS", "")


def test_validator_is_exact_branch_correlation_and_transaction_deferred() -> None:
    source = _source(MIGRATION)

    for predicate in (
        "event_data.correlation_id = NEW.correlation_id",
        "event_data.branch_id = NEW.branch_id",
        "event_data.emitted_at = NEW.correlation_emitted_at",
    ):
        assert predicate in source

    assert "CREATE CONSTRAINT TRIGGER trg_validate_history_correlation" in source
    assert "AFTER INSERT ON public.branch_status_history" in source
    assert "DEFERRABLE INITIALLY DEFERRED" in source
    assert "EXECUTE FUNCTION public.validate_history_correlation_hardened()" in source
    assert "CREATE TRIGGER trg_validate_history_correlation\n        BEFORE INSERT" in source


def test_internal_trigger_functions_are_closed_to_public_at_head() -> None:
    source = _source(MIGRATION)

    assert (
        "REVOKE ALL ON FUNCTION "
        "public.validate_history_correlation_hardened() FROM PUBLIC"
        in source
    )
    assert (
        "REVOKE ALL ON FUNCTION public.validate_history_correlation() FROM PUBLIC"
        in source
    )
    assert (
        "GRANT EXECUTE ON FUNCTION public.validate_history_correlation() TO PUBLIC"
        in source
    )
    assert "predecessor PUBLIC EXECUTE" in source


def test_downgrade_removes_only_the_revision_owned_delta() -> None:
    source = _source(MIGRATION)

    assert (
        "DROP POLICY lifecycle_correlation_validator_event_read "
        "ON public.branch_lifecycle_events"
        in source
    )
    assert (
        "REVOKE SELECT (branch_id, correlation_id, emitted_at) "
        "ON TABLE public.branch_lifecycle_events FROM app_security_owner"
        in source
    )
    assert "SET LOCAL ROLE app_security_owner" in source
    assert "DROP FUNCTION public.validate_history_correlation_hardened() RESTRICT" in source
    assert "RESET ROLE" in source
    assert "CASCADE" not in source


def test_transaction_a_flushes_canonical_event_before_history_is_added() -> None:
    source = _source(SERVICE)
    start = source.index("async def initiate_transition")
    end = source.index("async def _record_checkpoint", start)
    method = source[start:end]

    event_add = method.index("self.db.add(event)")
    event_flush = method.index("await self.db.flush()", event_add)
    history_add = method.index("self.db.add(history)")
    assert event_add < event_flush < history_add

    # Atomicity is preserved: no intermediate commit may split the canonical
    # event from history/outbox Transaction A.
    assert "await self.db.commit()" not in method[:history_add]
