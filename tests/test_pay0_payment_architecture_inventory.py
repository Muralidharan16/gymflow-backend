import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "docs/architecture/pay0_payment_architecture_inventory.json"
SCOPE = ROOT / "docs/architecture/PAY0_PAYMENT_ARCHITECTURE_INVENTORY_AND_GOVERNANCE.md"
ACCEPTANCE = ROOT / "docs/architecture/PAY0_ACCEPTANCE_MATRIX.md"
WORKFLOW = ROOT / ".github/workflows/pay0-payment-architecture-inventory.yml"

BASE_SHA = "4357fb14b406514d80376f6d23aed4dc185c4c00"
BASE_TREE = "66e4528ee2380a5591ee8a94d9df3ff465e3581c"
ALEMBIC_HEAD = "zk07d8e9f0a45"
BRANCH = "hardening/pay0-payment-architecture-inventory"


def _inventory():
    return json.loads(INVENTORY.read_text(encoding="utf-8"))


def test_pay0_exact_inherited_baseline_is_frozen():
    data = _inventory()
    assert data["phase"] == "PAY-0"
    assert data["branch"] == BRANCH
    assert data["inherited_p10"]["commit"] == BASE_SHA
    assert data["inherited_p10"]["tree"] == BASE_TREE
    assert data["inherited_p10"]["alembic_head"] == ALEMBIC_HEAD


def test_financial_authority_model_is_explicit_and_fail_closed():
    authority = _inventory()["authority"]
    assert authority["durable_internal_financial_authority"] == "postgresql_finance_core"
    assert authority["external_money_fact_authority"] == "verified_provider_evidence"
    assert authority["member_commerce"] == "business_state_only_not_accounting_authority"
    assert authority["platform_billing"] == "separate_saas_billing_domain"
    assert authority["browser"] == "non_authoritative"
    assert authority["redis_celery_beat"] == "delivery_coordination_only"
    assert authority["operator_input"] == "non_authoritative"


def test_current_live_money_switches_remain_disabled():
    guards = (ROOT / "app/finance_core/api/guards.py").read_text(encoding="utf-8")
    settings = (ROOT / "app/core/settings_schema.py").read_text(encoding="utf-8")

    assert "FINANCE_PAYMENT_API_ENABLED = False" in guards
    assert "live_provider_enabled: bool = False" in guards
    assert "live_money_movement_enabled: bool = False" in guards

    for token in (
        "PLATFORM_BILLING_CHECKOUT: bool = False",
        "PLATFORM_BILLING_WEBHOOK_PROCESSING: bool = False",
        "PLATFORM_BILLING_DUNNING_TRANSITIONS: bool = False",
        "PLATFORM_BILLING_NOTIFICATIONS: bool = False",
    ):
        assert token in settings

    live = _inventory()["live_money_guards"]
    assert live["refund_provider_execution"] == "DEFERRED_FAIL_CLOSED"
    assert all(
        value is False
        for key, value in live.items()
        if key != "refund_provider_execution"
    )


def test_legacy_member_payment_contract_drift_is_explicitly_inventory_only():
    model = (ROOT / "app/models/payment.py").read_text(encoding="utf-8")
    service = (ROOT / "app/services/payment_service.py").read_text(encoding="utf-8")
    router = (ROOT / "app/routers/payments.py").read_text(encoding="utf-8")
    schema = (ROOT / "app/schemas/payment.py").read_text(encoding="utf-8")

    assert "payment_type:" in model
    assert "transaction_reference:" in model
    assert "collected_by:" in model
    assert "type=type" in service
    assert "reference_number=reference_number" in service
    assert "created_by=created_by" in service
    assert "data.method" in router and "data.type" in router
    assert "payment_method: PaymentMethod" in schema
    assert "payment_type: PaymentType" in schema

    item = next(x for x in _inventory()["components"] if x["id"] == "legacy_member_payments")
    assert item["classification"] == "deprecated_compatibility_mutation_surface"
    assert item["production_authority"] is False


def test_member_subscription_payment_gap_is_frozen_not_changed():
    service = (ROOT / "app/services/member_subscription_v2_service.py").read_text(encoding="utf-8")
    router = (ROOT / "app/routers/member_subscriptions_v2.py").read_text(encoding="utf-8")
    lifecycle = (ROOT / "app/domain/subscription_lifecycle.py").read_text(encoding="utf-8")

    assert "status=ModernSubscriptionStatus.active" in service
    assert "RazorpaySandboxAdapter" in router
    assert 'pending_payment = "pending_payment"' in lifecycle

    item = next(x for x in _inventory()["components"] if x["id"] == "modern_member_subscription_v2")
    assert item["risk"] == "currently_active_before_authoritative_payment"


def test_platform_billing_real_provider_is_not_admitted():
    provider_dir = ROOT / "app/platform_billing/providers"
    providers = sorted(path.name for path in provider_dir.glob("*.py"))
    assert providers == [
        "__init__.py",
        "base.py",
        "fake.py",
        "fake_checkout_evidence.py",
        "fake_checkout_simulation.py",
        "reconciliation.py",
    ]
    api_dir = ROOT / "app/platform_billing/api"
    assert not (api_dir / "webhook.py").exists()
    assert not (api_dir / "webhooks.py").exists()
    assert not (api_dir / "callback.py").exists()


def test_pay0_migration_inventory_is_complete_and_pay0_has_no_migration():
    migration = _inventory()["migration_baseline"]
    assert migration["mutation_allowed_in_pay0"] is False
    assert migration["expected_single_head"] == ALEMBIC_HEAD
    assert len(migration["payment_related_migrations"]) == 9
    for rel in migration["payment_related_migrations"]:
        assert (ROOT / rel).exists(), rel

    required = {
        "linear_or_explicitly_reviewed_alembic_graph",
        "real_postgresql_forward_migration",
        "downgrade_reupgrade_when_supported",
        "populated_data_preservation",
        "rls_acl_ownership_preservation",
        "lock_wait_and_table_rewrite_analysis",
        "rolling_schema_compatibility",
        "backup_restore_pitr_compatibility",
    }
    assert required == set(migration["future_required_gates"])


def test_pay0_hard_stops_and_markers_are_bound():
    hard_stops = set(_inventory()["hard_stops"])
    assert {
        "application_runtime_code_change_in_pay0",
        "alembic_revision_change_in_pay0",
        "database_acl_rls_or_role_change_in_pay0",
        "live_provider_activation",
        "live_money_movement",
        "refund_provider_execution",
        "merge",
        "release",
        "production_deployment",
    } <= hard_stops

    combined = SCOPE.read_text(encoding="utf-8") + ACCEPTANCE.read_text(encoding="utf-8")
    for marker in (
        "PAY0_ARCHITECTURE_INVENTORY=PASS",
        "PAY0_MIGRATION_BASELINE=PASS",
        "PAY0_P10_INHERITED=PASS",
        "PAY0_LIVE_MONEY_MOVEMENT=DISABLED",
        "PAY0_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED",
        "PAY0_FINAL=PASS",
    ):
        assert marker in combined


def test_pay0_workflow_binds_exact_parent_and_migration_regressions():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert BRANCH in workflow
    assert BASE_SHA in workflow
    assert BASE_TREE in workflow
    assert ALEMBIC_HEAD in workflow
    assert "scripts/verify_alembic_graph.py" in workflow
    assert "migration-lifecycle-ci.yml" in workflow
    assert "migration-data-preservation-ci.yml" in workflow
    assert "migration-adversarial-safety-ci.yml" in workflow
    assert "migration-semantics-inventory.yml" in workflow
    assert "finance-hardening-ci.yml" in workflow
    assert "PAY0_FINAL=PASS" in workflow
