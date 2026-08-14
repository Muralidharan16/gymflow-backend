from __future__ import annotations

from pathlib import Path


SOURCE = Path("app/services/onboarding_service.py")


def test_onboarding_attaches_typed_context_before_first_tenant_flush() -> None:
    source = SOURCE.read_text(encoding="utf-8")

    context_call = "await update_session_context("
    first_flush = "await self.session.flush()"

    assert "from app.core.database import update_session_context" in source
    assert source.count(context_call) == 1
    assert 'principal_type="owner"' in source
    assert "principal_id=str(owner.id)" in source
    assert "org_id=str(org.id)" in source
    assert first_flush in source
    assert source.index(context_call) < source.index(first_flush)


def test_onboarding_does_not_own_raw_guc_or_rls_bypass_logic() -> None:
    source = SOURCE.read_text(encoding="utf-8").lower()

    # Request/session database context is a core database-layer responsibility.
    assert "pg_catalog.set_config" not in source
    assert "set local" not in source

    forbidden = (
        "disable row level security",
        "no force row level security",
        "bypassrls",
        "row_security = off",
    )
    for token in forbidden:
        assert token not in source
