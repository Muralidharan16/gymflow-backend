from __future__ import annotations

import ast
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "alembic" / "versions" / "zz17d8e9f0a61_pay15_legacy_payment_retirement.py"
DOMAIN = ROOT / "app" / "finance_core" / "domain" / "legacy_retirement.py"
MODELS = ROOT / "app" / "finance_core" / "models" / "legacy_retirement.py"
REPOSITORY = ROOT / "app" / "finance_core" / "repositories" / "legacy_retirement.py"
SERVICE = ROOT / "app" / "finance_core" / "services" / "legacy_retirement.py"
PAYMENTS_ROUTER = ROOT / "app" / "routers" / "payments.py"
SUBSCRIPTIONS_ROUTER = ROOT / "app" / "routers" / "subscriptions.py"
PAYMENT_SERVICE = ROOT / "app" / "services" / "payment_service.py"
INVOICE_SERVICE = ROOT / "app" / "services" / "invoice_service.py"
SUBSCRIPTION_SERVICE = ROOT / "app" / "services" / "subscription_service.py"
DOC = ROOT / "docs" / "architecture" / "PAY15_LEGACY_PAYMENT_RETIREMENT.md"
CONTRACT = ROOT / "docs" / "architecture" / "pay15_legacy_retirement_v1.yaml"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_pay15_revision_and_predecessor_are_exact():
    source = _source(MIGRATION)
    assert 'revision: str = "zz17d8e9f0a61"' in source
    assert (
        'down_revision: Union[str, Sequence[str], None] = "zz07d8e9f0a60"'
        in source
    )


def test_legacy_tables_are_database_immutable_including_truncate():
    source = _source(MIGRATION)
    assert "BEFORE INSERT OR UPDATE OR DELETE ON public.payments" in source
    assert "BEFORE INSERT OR UPDATE OR DELETE ON public.invoices" in source
    assert "BEFORE TRUNCATE ON public.payments" in source
    assert "BEFORE TRUNCATE ON public.invoices" in source
    assert "legacy monetary authority retired" in source


def test_legacy_mutation_http_surfaces_are_retired_but_reads_remain():
    payments = _source(PAYMENTS_ROUTER)
    subscriptions = _source(SUBSCRIPTIONS_ROUTER)
    assert "LEGACY_PAYMENT_WRITE_RETIRED" in payments
    assert "LEGACY_SUBSCRIPTION_PAYMENT_WRITE_RETIRED" in subscriptions
    assert "status.HTTP_410_GONE" in payments
    assert "status.HTTP_410_GONE" in subscriptions
    assert '@router.get("", response_model=PaginatedResponse[PaymentResponse])' in payments
    assert 'async def list_payments(' in payments
    assert '@router.get("/{payment_id}", response_model=Response[PaymentResponse])' in payments
    assert 'async def get_payment_detail(' in payments
    assert '@router.get("/{payment_id}/invoice", response_model=Response[InvoiceResponse])' in payments
    assert 'async def get_payment_invoice(' in payments
    assert "regenerate_pdf" not in payments
    assert "void_invoice(invoice.id" not in payments


def test_legacy_services_have_no_remaining_monetary_write_body():
    payment = _source(PAYMENT_SERVICE)
    invoice = _source(INVOICE_SERVICE)
    subscription = _source(SUBSCRIPTION_SERVICE)
    assert "PAY-15 legacy payment writes are retired" in payment
    assert "created = await self.payment_repo.create(payment)" not in payment
    assert "PAY-15 legacy invoice writes are retired" in invoice
    assert "invoice.status = InvoiceStatus.VOID" not in invoice
    assert "invoice.pdf_url =" not in invoice
    assert "Invoice(" not in invoice
    assert "PAY-15 legacy subscription payment writes are retired" in subscription
    assert "payment = Payment(" not in subscription
    assert "await self.payment_repo.create(payment)" not in subscription


def test_migration_capabilities_are_app_secure_config_only():
    source = _source(MIGRATION)
    for function_name in (
        "pay15_capture_inventory",
        "pay15_begin_reconciliation",
        "pay15_record_invoice_disposition",
        "pay15_record_payment_disposition",
        "pay15_record_subscription_link",
        "pay15_certify_ready",
        "pay15_activate_cutover",
        "pay15_enter_rollback_hold",
    ):
        assert f"CREATE FUNCTION app_secure.{function_name}" in source
        assert (
            f"GRANT EXECUTE ON FUNCTION app_secure.{function_name}"
            in source
        )
    assert "TO finance_config_runtime" in source
    assert "PAY-15 migration requires finance_config_runtime" in source
    assert "SET row_security = on" in source
    assert "FROM PUBLIC" in source
    assert "GRANT EXECUTE ON FUNCTION app_secure.pay15_capture_inventory" in source
    assert "TO app_runtime" not in source.split(
        "GRANT EXECUTE ON FUNCTION app_secure.pay15_capture_inventory", 1
    )[1].split("CREATE VIEW finance.legacy_invoices_compat_v", 1)[0]


def test_configuration_repository_has_no_direct_migration_table_dml():
    source = _source(REPOSITORY)
    for forbidden in (
        "INSERT INTO finance.legacy_",
        "UPDATE finance.legacy_",
        "DELETE FROM finance.legacy_",
        "TRUNCATE",
    ):
        assert forbidden not in source
    assert "app_secure.pay15_capture_inventory" in source
    assert "app_secure.pay15_record_invoice_disposition" in source
    assert "app_secure.pay15_record_payment_disposition" in source
    assert "finance_config_runtime" in source


def test_no_http_router_imports_pay15_migration_service():
    for path in (ROOT / "app" / "routers").glob("*.py"):
        assert "legacy_retirement" not in _source(path)


def test_migration_has_record_level_money_tax_reference_and_subscription_gates():
    source = _source(MIGRATION)
    for token in (
        "source_invoice_total",
        "source_invoice_tax_total",
        "source_payment_total",
        "legacy_invoice_number",
        "source_transaction_reference",
        "source_razorpay_id",
        "legacy_subscription_id",
        "source_checksum_sha256",
        "manifest_sha256",
        "unknown historical invoices",
        "unreconciled migrated money remains",
        "duplicate Finance records",
        "monetary/tax totals do not reconcile",
        "unpreserved subscription bindings",
        "source checksum changed after inventory snapshot",
    ):
        assert token in source


def test_invoice_number_and_provider_reference_are_exact_for_migrated_history():
    source = _source(MIGRATION)
    assert "official_invoice_number IS DISTINCT FROM v_source.invoice_number" in source
    assert "provider_payment_ref IS DISTINCT FROM v_source_ref" in source
    assert "total_tax_amount IS DISTINCT FROM v_source.tax_amount" in source
    assert "grand_total_amount IS DISTINCT FROM v_source.total_amount" in source


def test_historical_read_only_and_compatibility_projection_exist():
    source = _source(MIGRATION)
    assert "'historical_read_only','migrated_finance'" in source
    assert "finance.legacy_invoices_compat_v" in source
    assert "finance.legacy_payments_compat_v" in source
    assert "security_barrier=true" in source
    assert "GRANT SELECT ON\n                    finance.legacy_invoices_compat_v" not in source


def test_migration_history_is_append_only_and_populated_downgrade_fails_closed():
    source = _source(MIGRATION)
    assert "PAY-15 migration history is immutable" in source
    assert (
        "PAY-15 downgrade blocked: legacy retirement/migration history exists"
        in source
    )


def test_cutover_is_checksum_bound_and_rollback_never_restores_legacy_writes():
    source = _source(MIGRATION)
    assert "app_secure.pay15_source_snapshot_sha" in source
    assert "app_secure.pay15_certify_ready" in source
    assert "app_secure.pay15_activate_cutover" in source
    assert "cutover manifest checksum mismatch" in source
    assert "app_secure.pay15_enter_rollback_hold" in source
    assert "rollback hold is terminal; legacy writes remain disabled" in source
    assert "finance_pause_legacy_read_only" in source


def test_pay15_source_acl_is_column_scoped_and_reversed_on_empty_downgrade():
    source = _source(MIGRATION)
    assert "GRANT SELECT (\n            id, gym_id, subscription_id" in source
    assert "GRANT SELECT ON TABLE public.payments TO app_security_owner" not in source
    assert "GRANT SELECT ON TABLE public.invoices TO app_security_owner" not in source
    downgrade = source.split("def downgrade()", 1)[1]
    assert "REVOKE SELECT (\n            id, gym_id, subscription_id" in downgrade
    assert "REVOKE SELECT (id, org_id)" in downgrade


def test_pay15_is_finance_core_only_and_does_not_import_platform_billing():
    for path in (DOMAIN, MODELS, REPOSITORY, SERVICE):
        tree = ast.parse(_source(path))
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module)
            elif isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
        assert not any(name.startswith("app.platform_billing") for name in imports)


def test_contract_binds_hard_zero_gates_and_safety_posture():
    contract = yaml.safe_load(_source(CONTRACT))
    assert contract["predecessor_sha"] == "b23b81f4472554f64e08342dc1268d393d720c7f"
    assert contract["predecessor_tree"] == "d1ad75c713bf87b9380822081d3e36b77762255e"
    assert contract["revision"] == "zz17d8e9f0a61"
    assert contract["legacy_writes"] == 0
    assert contract["unreconciled_migrated_money"] == 0
    assert contract["unknown_historical_invoices"] == 0
    assert contract["duplicate_finance_records"] == 0
    assert contract["permanent_dual_write"] is False
    assert contract["live_provider"] is False
    assert contract["production_runtime_binding"] is False
    assert contract["gate"] == "PAY15_LEGACY_RETIREMENT=PASS"
    assert "PAY15_LEGACY_RETIREMENT=PASS" in _source(DOC)
