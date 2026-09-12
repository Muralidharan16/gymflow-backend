from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "za07d8e9f0a3b_p4c_notification_history_read_boundary.py"
DELIVERY = ROOT / "alembic" / "versions" / "w07d8e9f0a37_p4c_notification_delivery.py"


def test_history_boundary_is_append_only_after_p4c_operations_head() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'revision = "za07d8e9f0a3b"' in source
    assert 'down_revision = "z07d8e9f0a3a"' in source


def test_history_boundary_preserves_force_rls_and_adds_no_runtime_table_grants() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert "requires branch_status_history ENABLE+FORCE RLS" in source
    assert "CREATE POLICY p4c_notification_history_security_owner_select" in source
    assert "FOR SELECT TO app_security_owner" in source
    for role in (
        "app_runtime",
        "auth_runtime",
        "worker_runtime",
        "lifecycle_maintenance_runtime",
    ):
        assert f"TO {role}" not in source
        assert f"GRANT SELECT ON TABLE public.branch_status_history TO {role}" not in source
    assert "BYPASSRLS" not in source
    assert "USING (true)" not in source.lower()


def test_history_policy_is_tenant_and_worker_context_bound() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    for token in (
        "app.current_role",
        "branch_lifecycle_worker",
        "app.internal_maintenance",
        "branch_lifecycle_saga",
        "app.current_org_id",
        "app.worker_id",
        "public.org_branches",
        "branch_status_history.branch_id",
    ):
        assert token in source
    assert "pg_input_is_valid" in source
    assert "p4b_search_internal_branch_read" in source


def test_fanout_still_establishes_live_lease_before_history_read() -> None:
    source = DELIVERY.read_text(encoding="utf-8")
    fanout = source.split(
        "CREATE FUNCTION app_secure.materialize_branch_member_notifications", 1
    )[1].split("CREATE FUNCTION app_secure.claim_notification_delivery", 1)[0]
    lease = fanout.index("notification fanout requires live owned lease")
    history = fanout.index("FROM public.branch_status_history h")
    assert lease < history
    assert "o.status='processing'" in fanout
    assert "o.leased_by=p_worker_id" in fanout
    assert "o.leased_until>pg_catalog.clock_timestamp()" in fanout
    assert "h.branch_id=v_branch AND h.correlation_id=v_correlation" in fanout


def test_history_boundary_downgrade_removes_only_its_policy() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    downgrade = source.split("def downgrade()", 1)[1]
    assert "DROP POLICY IF EXISTS p4c_notification_history_security_owner_select" in downgrade
    assert "REVOKE" not in downgrade
    assert "DROP TABLE" not in downgrade
