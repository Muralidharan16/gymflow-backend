from __future__ import annotations

from pathlib import Path


SOURCE = Path("app/services/onboarding_service.py")


def test_onboarding_sets_tenant_and_user_context_before_first_flush() -> None:
    source = SOURCE.read_text(encoding="utf-8")

    org_context = "set_config('app.current_org_id', :org_id, true)"
    user_context = "set_config('app.current_user_id', :user_id, true)"
    first_flush = "await self.session.flush()"

    assert source.count(org_context) == 1
    assert source.count(user_context) == 1
    assert first_flush in source

    first_flush_index = source.index(first_flush)
    assert source.index(org_context) < first_flush_index
    assert source.index(user_context) < first_flush_index


def test_onboarding_does_not_bypass_forced_rls() -> None:
    source = SOURCE.read_text(encoding="utf-8").lower()

    forbidden = (
        "disable row level security",
        "no force row level security",
        "bypassrls",
        "set row_security = off",
        "set local row_security = off",
    )

    for token in forbidden:
        assert token not in source
