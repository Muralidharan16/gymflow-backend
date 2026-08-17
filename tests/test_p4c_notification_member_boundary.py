from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "zb07d8e9f0a3c_p4c_notification_member_read_boundary.py"
DELIVERY = ROOT / "alembic" / "versions" / "w07d8e9f0a37_p4c_notification_delivery.py"
CRASH_RECOVERY = ROOT / "alembic" / "versions" / "y07d8e9f0a39_p4c_notification_crash_recovery.py"


def test_member_boundary_is_append_only_after_history_boundary() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'revision = "zb07d8e9f0a3c"' in source
    assert 'down_revision = "za07d8e9f0a3b"' in source


def test_member_boundary_preserves_force_rls_and_adds_no_runtime_grant() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert "ENABLE+FORCE RLS" in source
    assert "CREATE POLICY p4c_notification_member_security_owner_select" in source
    assert "FOR SELECT TO app_security_owner" in source
    assert "CREATE POLICY p4c_notification_delivery_security_owner_insert" in source
    assert "CREATE POLICY p4c_notification_reconcile_security_owner_insert" in source
    assert source.count("FOR INSERT TO app_security_owner") == 2
    for role in (
        "app_runtime",
        "auth_runtime",
        "worker_runtime",
        "lifecycle_maintenance_runtime",
    ):
        assert f"GRANT SELECT ON TABLE public.members TO {role}" not in source
        assert f"GRANT INSERT ON TABLE public.branch_outbox_events TO {role}" not in source
    assert "BYPASSRLS" not in source
    assert "DISABLE ROW LEVEL SECURITY" not in source
    assert "NO FORCE ROW LEVEL SECURITY" not in source
    assert "WITH CHECK (true)" not in source.lower()


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


def test_notification_delivery_child_policy_is_canonical_and_parent_lease_bound() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    policy = source.split(
        "CREATE POLICY p4c_notification_delivery_security_owner_insert", 1
    )[1].split(
        "CREATE POLICY p4c_notification_reconcile_security_owner_insert", 1
    )[0]

    for token in (
        "event_type='notification.delivery'",
        "status='pending'",
        "attempt_count=0",
        "leased_by IS NULL",
        "leased_until IS NULL",
        "jsonb_build_object('command_id',outbox_id::text)",
        "FROM public.notification_commands AS command_data",
        "command_data.command_id=branch_outbox_events.outbox_id",
        "command_data.tenant_id=branch_outbox_events.tenant_id",
        "command_data.branch_id=branch_outbox_events.branch_id",
        "command_data.correlation_id=branch_outbox_events.correlation_id",
        "command_data.max_attempts=branch_outbox_events.max_attempts",
        "command_data.next_attempt_at=branch_outbox_events.process_after",
        "parent_data.event_type='branch.member_notification'",
        "parent_data.status='processing'",
        "parent_data.leased_by=CAST(",
        "parent_data.leased_until>pg_catalog.clock_timestamp()",
    ):
        assert token in policy

    assert "app.current_org_id" in policy
    assert "app.worker_id" in policy
    assert "WITH CHECK (true)" not in policy.lower()


def test_notification_reconcile_child_policy_is_maintenance_and_authority_bound() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    policy = source.split(
        "CREATE POLICY p4c_notification_reconcile_security_owner_insert", 1
    )[1].split("_post_install_proof(bind)", 1)[0]

    for token in (
        "app.current_role",
        "'lifecycle_maintenance'",
        "app.internal_maintenance",
        "'lifecycle'",
        "event_type='notification.reconcile'",
        "status='pending'",
        "attempt_count=0",
        "max_attempts=8",
        "leased_by IS NULL",
        "leased_until IS NULL",
        "pg_input_is_valid(NULLIF(payload->>'command_id',''),'uuid')",
        "payload=pg_catalog.jsonb_build_object('command_id',payload->>'command_id')",
        "FROM public.notification_commands AS command_data",
        "command_data.command_id=CAST(NULLIF(branch_outbox_events.payload->>'command_id','') AS uuid)",
        "command_data.tenant_id=branch_outbox_events.tenant_id",
        "command_data.branch_id=branch_outbox_events.branch_id",
        "command_data.correlation_id=branch_outbox_events.correlation_id",
        "command_data.status='provider_accepted'",
        "command_data.provider_code='resend'",
        "command_data.provider_reference_id IS NOT NULL",
        "command_data.acknowledged_at IS NOT NULL",
        "command_data.acknowledged_at<=pg_catalog.clock_timestamp()-INTERVAL '2 minutes'",
    ):
        assert token in policy

    assert "WITH CHECK (true)" not in policy.lower()
    assert "app.current_org_id" not in policy
    assert "app.worker_id" not in policy


def test_fanout_and_v2_claim_remain_live_projection_bound() -> None:
    delivery_source = DELIVERY.read_text(encoding="utf-8")
    fanout = delivery_source.split(
        "CREATE FUNCTION app_secure.materialize_branch_member_notifications", 1
    )[1].split("CREATE FUNCTION app_secure.claim_notification_delivery", 1)[0]

    claim_source = CRASH_RECOVERY.read_text(encoding="utf-8")
    claim = claim_source.split(
        "CREATE FUNCTION app_secure.claim_notification_delivery_v2", 1
    )[1].split("$function$;", 1)[0]

    assert fanout.index("notification fanout requires live owned lease") < fanout.index(
        "FROM public.members m"
    )
    assert "m.org_id=v_tenant AND m.home_branch_id=v_branch" in fanout
    assert "m.is_active IS TRUE AND m.status::text='active'" in fanout
    assert "'notification.delivery'" in fanout
    assert "jsonb_build_object('command_id',c.command_id::text)" in fanout

    assert claim.index("notification claim requires live owned outbox lease") < claim.index(
        "FROM public.members AS member_data"
    )
    assert "member_data.id=v_command.member_id" in claim
    assert "member_data.org_id=v_command.tenant_id" in claim
    assert "member_data.home_branch_id=v_command.branch_id" in claim
    assert "member_data.is_active IS TRUE" in claim
    assert "member_data.status::text='active'" in claim
    assert "preference_data.email_suppressed_at IS NULL" in claim


def test_v2_claim_qualifies_return_table_identifier_collisions() -> None:
    source = CRASH_RECOVERY.read_text(encoding="utf-8")
    claim = source.split("CREATE FUNCTION app_secure.claim_notification_delivery_v2", 1)[1].split(
        "$function$;", 1
    )[0]

    assert "WHERE command_id=v_command.command_id" not in claim
    assert "ON CONFLICT(command_id,attempt_number)" not in claim
    assert claim.count("WHERE command_data.command_id=v_command.command_id") == 3
    assert "ON CONFLICT ON CONSTRAINT notification_delivery_attempts_command_id_attempt_number_key" in claim
    assert "FROM public.notification_commands AS command_data" in claim
    assert "UPDATE public.notification_commands AS command_data" in claim


def test_member_boundary_downgrade_removes_only_its_policies() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    downgrade = source.split("def downgrade()", 1)[1]
    assert "DROP POLICY IF EXISTS p4c_notification_reconcile_security_owner_insert" in downgrade
    assert "DROP POLICY IF EXISTS p4c_notification_delivery_security_owner_insert" in downgrade
    assert "DROP POLICY IF EXISTS p4c_notification_member_security_owner_select" in downgrade
    assert "REVOKE" not in downgrade
    assert "DROP TABLE" not in downgrade
