from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "zb07d8e9f0a3c_p4c_notification_member_read_boundary.py"
DELIVERY = ROOT / "alembic" / "versions" / "w07d8e9f0a37_p4c_notification_delivery.py"


def test_member_boundary_is_append_only_after_history_boundary() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'revision = "zb07d8e9f0a3c"' in source
    assert 'down_revision = "za07d8e9f0a3b"' in source


def test_member_boundary_preserves_force_rls_and_adds_no_runtime_grant() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert "requires members ENABLE+FORCE RLS" in source
    assert "CREATE POLICY p4c_notification_member_security_owner_select" in source
    assert "FOR SELECT TO app_security_owner" in source
    for role in (
        "app_runtime",
        "auth_runtime",
        "worker_runtime",
        "lifecycle_maintenance_runtime",
    ):
        assert f"GRANT SELECT ON TABLE public.members TO {role}" not in source
    assert "BYPASSRLS" not in source
    assert "USING (true)" not in source.lower()


def test_member_policy_is_tenant_and_worker_context_bound() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    for token in (
        "app.current_role",
        "branch_lifecycle_worker",
        "app.internal_maintenance",
        "branch_lifecycle_saga",
        "app.current_org_id",
        "app.worker_id",
        "org_id",
        "pg_input_is_valid",
    ):
        assert token in source


def test_fanout_and_claim_remain_live_projection_bound() -> None:
    source = DELIVERY.read_text(encoding="utf-8")
    fanout = source.split(
        "CREATE FUNCTION app_secure.materialize_branch_member_notifications", 1
    )[1].split("CREATE FUNCTION app_secure.claim_notification_delivery", 1)[0]
    claim = source.split(
        "CREATE FUNCTION app_secure.claim_notification_delivery", 1
    )[1].split("CREATE FUNCTION app_secure.acknowledge_notification_provider_acceptance", 1)[0]

    assert fanout.index("notification fanout requires live owned lease") < fanout.index(
        "FROM public.members m"
    )
    assert "m.org_id=v_tenant AND m.home_branch_id=v_branch" in fanout
    assert "m.is_active IS TRUE AND m.status::text='active'" in fanout

    assert claim.index("notification claim requires live owned outbox lease") < claim.index(
        "FROM public.members m"
    )
    assert "m.id=v_command.member_id AND m.org_id=v_command.tenant_id" in claim
    assert "m.home_branch_id=v_command.branch_id" in claim
    assert "m.is_active IS TRUE AND m.status::text='active'" in claim


def test_member_boundary_downgrade_removes_only_its_policy() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    downgrade = source.split("def downgrade()", 1)[1]
    assert "DROP POLICY IF EXISTS p4c_notification_member_security_owner_select" in downgrade
    assert "REVOKE" not in downgrade
    assert "DROP TABLE" not in downgrade
