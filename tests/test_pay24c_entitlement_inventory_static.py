from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_pay24c1_modern_admission_is_pending_and_finance_gated() -> None:
    service = _source("app/services/member_subscription_v2_service.py")
    assert "status=ModernSubscriptionStatus.pending" in service
    assert "app_secure.create_member_subscription_pending_term" in service


def test_pay24c1_canonical_lifecycle_tables_are_api_read_only() -> None:
    migration = _source(
        "alembic/versions/8d3e4f5a6b7c_harden_subscription_lifecycle_read_boundary.py"
    )
    assert '"subscription_terms"' in migration
    assert "GRANT SELECT ON TABLE public.{table_name} TO app_runtime" in migration
    assert 'for forbidden in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")' in migration


def test_pay24c1_modern_projection_has_no_api_update_authority() -> None:
    migration = _source(
        "alembic/versions/c6d7e8f9a0b1_harden_member_subscription_runtime_boundary.py"
    )
    assert '"member_subscriptions_v2": ("SELECT", "INSERT")' in migration
    assert "GRANT SELECT, INSERT ON TABLE public.member_subscriptions_v2 TO app_runtime" in migration
    assert '"member_subscriptions_v2": set()' in migration


def test_pay24c1_pay4_activation_is_worker_gated_and_revalidates_finance() -> None:
    migration = _source(
        "alembic/versions/zo07d8e9f0a49_pay4_member_finance_binding.py"
    )
    assert "CREATE FUNCTION app_secure.apply_member_subscription_finance_event" in migration
    assert "PAY-4 activation requires worker_runtime" in migration
    assert "finance.invoice.paid" in migration
    assert "p.status NOT IN ('captured','settled')" in migration
    assert "v_term.status::text <> 'pending_payment'" in migration
    assert "SET status=v_target::subscription_term_status" in migration
    assert "INSERT INTO public.subscription_events" in migration


def test_pay24c1_pay5_consumption_is_durable_and_replay_fenced() -> None:
    migration = _source(
        "alembic/versions/zp07d8e9f0a50_pay5_finance_event_delivery.py"
    )
    dispatcher = _source("app/tasks/finance_event_dispatcher.py")
    assert "public.member_subscription_finance_event_consumptions" in migration
    assert "CREATE FUNCTION app_secure.consume_member_subscription_finance_event" in migration
    assert "app_secure.apply_member_subscription_finance_event" in migration
    assert "v_event.lease_fence IS DISTINCT FROM p_lease_fence" in migration
    assert "WHERE c.finance_event_id=p_finance_event_id" in migration
    assert "app_secure.consume_member_subscription_finance_event" in dispatcher


def test_pay24c1_mounted_legacy_findings_are_now_retired_by_successor() -> None:
    inventory = _source("docs/architecture/PAY24C1_ENTITLEMENT_AUTHORITY_INVENTORY.md")
    router = _source("app/routers/subscriptions.py")
    service = _source("app/services/subscription_service.py")

    assert "LEGACY_DIRECT_MUTATION_CODE_PRESENT" in inventory
    assert "LEGACY_ENTITLEMENT_MUTATION_RETIRED" in router
    assert "PAY-24-C legacy entitlement mutation is retired" in service
    for forbidden in (
        "sub.status = SubscriptionStatus.frozen",
        "sub.end_date = sub.end_date + timedelta",
        "sub.status = SubscriptionStatus.active",
        "sub.status = SubscriptionStatus.cancelled",
        "member.status = MemberStatus.inactive",
    ):
        assert forbidden not in service

def test_pay24c1_legacy_admission_is_retired() -> None:
    service = _source("app/services/subscription_service.py")
    router = _source("app/routers/subscriptions.py")
    assert "PAY-15 legacy subscription payment writes are retired" in service
    assert "LEGACY_SUBSCRIPTION_PAYMENT_WRITE_RETIRED" in router


def test_pay24c1_legacy_expiry_has_bounded_maintenance_replacement() -> None:
    old_task = _source("app/tasks/expire_subs.py")
    maintenance = _source("app/tasks/platform_maintenance.py")
    celery = _source("app/core/celery_app.py")

    assert "PAY-24-C legacy entitlement mutation is retired" in old_task
    assert "app_secure.expire_legacy_member_subscriptions(500)" in maintenance
    assert '"app.tasks.platform_maintenance.expire_legacy_member_subscriptions"' in celery
    assert '"app.tasks.expire_subs"' not in celery


def test_pay24c1_refund_finalization_is_not_direct_entitlement_mutation() -> None:
    refund = _source("app/finance_core/services/refund_financial_finalization.py")
    assert "app_secure.finalize_pay10_refund" in refund
    assert "subscription_terms" not in refund
    assert "member_subscriptions_v2" not in refund


def test_pay24c1_pay14_reconciliation_is_evidence_only() -> None:
    service = _source("app/platform_billing/services/accounting_reconciliation.py")
    architecture = _source("docs/architecture/PAY14_RECONCILIATION_TREASURY_ACCOUNTING.md")
    assert "records evidence and a reconciliation decision only" in service
    assert "does not become payment/refund/dispute mutation authority" in architecture
    assert "subscription_terms" not in service
    assert "member_subscriptions_v2" not in service


def test_pay24c1_inventory_declares_stage1_still_unauthorized() -> None:
    inventory = _source("docs/architecture/pay24c_entitlement_mutation_inventory_v1.json")
    assert '"stage1_live_activation": "NOT_AUTHORIZED"' in inventory
    assert '"real_provider_calls": 0' in inventory
    assert '"real_money_movement": 0' in inventory
    assert '"PAY24C_LEGACY_BYPASS_WRITES": "NOT_YET_ZERO"' in inventory
