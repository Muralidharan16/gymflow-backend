from __future__ import annotations

from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
MIGRATION=ROOT/"alembic/versions/zq07d8e9f0a51_pay6_offline_payments.py"
SERVICE=ROOT/"app/finance_core/services/offline_payments.py"
ROUTER=ROOT/"app/finance_core/api/offline_payments.py"
SCHEMAS=ROOT/"app/finance_core/api/schemas.py"
MAIN=ROOT/"app/main.py"


def test_pay6_is_additive_over_certified_pay5():
    source=MIGRATION.read_text(encoding="utf-8")
    assert 'revision = "zq07d8e9f0a51"' in source
    assert 'down_revision = "zp07d8e9f0a50"' in source
    assert "CREATE TABLE finance.offline_payment_requests" in source
    assert "CREATE TABLE finance.offline_payment_events" in source
    for forbidden in (
        "DROP TABLE finance.payments",
        "DROP TABLE finance.invoices",
        "UPDATE finance.payments SET",
        "DELETE FROM finance.payments",
        "TRUNCATE TABLE",
    ):
        assert forbidden not in source


def test_pay6_offline_methods_are_bounded_and_proof_reference_are_required():
    source=MIGRATION.read_text(encoding="utf-8")
    schemas=SCHEMAS.read_text(encoding="utf-8")
    assert "payment_method IN ('cash','bank_transfer','cheque')" in source
    assert 'Literal["cash", "bank_transfer", "cheque"]' in schemas
    assert "proof_sha256 CHAR(64) NOT NULL" in source
    assert "reference_code VARCHAR(120) NOT NULL" in source
    assert "p_proof_sha256 !~ '^[0-9a-f]{64}$'" in source
    assert "v_reference !~ '^[A-Za-z0-9][A-Za-z0-9:._/-]{0,119}$'" in source


def test_pay6_has_no_free_form_mark_paid_api():
    router=ROUTER.read_text(encoding="utf-8")
    schemas=SCHEMAS.read_text(encoding="utf-8")
    service=SERVICE.read_text(encoding="utf-8")
    assert "paid: bool" not in schemas
    assert "status: str" not in schemas.split("class FinanceOfflinePaymentPrepareRequest",1)[1].split(
        "class FinanceOfflinePaymentPrepareResponse",1
    )[0]
    assert "from app.services.payment_service import" not in router
    assert "app.services.payment_service" not in router
    assert "from app.services.payment_service import" not in service


def test_pay6_maker_checker_is_database_enforced_and_replay_actor_fenced():
    source=MIGRATION.read_text(encoding="utf-8")
    assert source.count("v_request.prepared_actor_id=v_actor") >= 2
    assert "maker/checker approval boundary violated" in source
    assert "maker/checker rejection boundary violated" in source
    assert source.count("v_cmd.actor_ref_sha256::text IS DISTINCT FROM") >= 2
    assert "v_role NOT IN ('owner','admin')" in source


def test_pay6_revalidates_invoice_balance_and_uses_canonical_payment_application():
    source=MIGRATION.read_text(encoding="utf-8")
    approve=source.split(
        "CREATE FUNCTION app_secure.approve_offline_payment",1
    )[1].split("$function$",2)[1]
    assert "FOR UPDATE" in approve
    assert "v_invoice.status NOT IN ('issued','partially_paid')" in approve
    assert "v_request.amount>v_outstanding" in approve
    assert "'manual'" in approve
    assert "'captured','offline_approved'" in approve
    assert "app_secure.apply_finance_confirmed_payment" in approve
    assert "'offline-payment/'||v_request.id::text||'/apply'" in approve
    assert "finance.payment_events" in approve
    assert "manual.payment.approved" in approve


def test_pay6_reuses_pay3_monetary_command_store_for_all_decisions():
    source=MIGRATION.read_text(encoding="utf-8")
    for scope in (
        "finance.offline_payment.prepare",
        "finance.offline_payment.approve",
        "finance.offline_payment.reject",
    ):
        assert scope in source
    assert "INSERT INTO finance.monetary_commands" in source
    assert "actor_ref_sha256" in source
    assert "pg_catalog.sha256(" in source
    assert "PAY-6 offline payment idempotency conflict" in source


def test_pay6_audit_events_are_append_only():
    source=MIGRATION.read_text(encoding="utf-8")
    assert "trg_pay6_offline_events_immutable" in source
    assert "PAY-6 offline payment audit events are immutable" in source
    assert "BEFORE UPDATE OR DELETE ON finance.offline_payment_events" in source
    for event in (
        "offline_payment.prepared",
        "offline_payment.approved",
        "offline_payment.rejected",
    ):
        assert event in source


def test_pay6_function_install_uses_temporary_schema_create_only():
    source=MIGRATION.read_text(encoding="utf-8")
    install=source.split("def _install_functions(bind)",1)[1].split("def upgrade()",1)[0]
    assert "has_schema_privilege" in install
    assert "GRANT CREATE ON SCHEMA app_secure TO app_security_owner" in install
    assert "SET LOCAL ROLE app_security_owner" in install
    assert "RESET ROLE" in install
    assert "REVOKE CREATE ON SCHEMA app_secure FROM app_security_owner" in install
    assert install.index("RESET ROLE") < install.index(
        "REVOKE CREATE ON SCHEMA app_secure FROM app_security_owner"
    )


def test_pay6_trigger_install_uses_temporary_migration_schema_visibility():
    source=MIGRATION.read_text(encoding="utf-8")
    install=source.split("def _install_functions(bind)",1)[1].split("def upgrade()",1)[0]
    assert "GRANT USAGE ON SCHEMA app_secure TO migration_owner" in install
    assert (
        "GRANT EXECUTE ON FUNCTION "
        "app_secure.pay6_reject_offline_event_mutation() "
        "TO migration_owner"
    ) in install
    assert "CREATE TRIGGER trg_pay6_offline_events_immutable" in install
    assert (
        "REVOKE EXECUTE ON FUNCTION "
        "app_secure.pay6_reject_offline_event_mutation() "
        "FROM migration_owner"
    ) in install
    assert "REVOKE USAGE ON SCHEMA app_secure FROM migration_owner" in install
    assert install.index(
        "GRANT USAGE ON SCHEMA app_secure TO migration_owner"
    ) < install.index("CREATE TRIGGER trg_pay6_offline_events_immutable")
    assert install.index("CREATE TRIGGER trg_pay6_offline_events_immutable") < install.rindex(
        "REVOKE USAGE ON SCHEMA app_secure FROM migration_owner"
    )


def test_pay6_application_runtime_is_capability_only():
    source=MIGRATION.read_text(encoding="utf-8")
    for signature in (
        "app_secure.prepare_offline_payment",
        "app_secure.approve_offline_payment",
        "app_secure.reject_offline_payment",
    ):
        assert signature in source
    assert "PAY-6 leaked direct Finance DML" in source
    assert "GRANT SELECT,INSERT ON TABLE finance.offline_payment_requests TO app_runtime" not in source
    assert "GRANT INSERT ON TABLE finance.payments TO app_runtime" not in source
    assert "GRANT UPDATE ON TABLE finance.invoices TO app_runtime" not in source


def test_pay6_downgrade_is_fail_closed_and_acl_delta_reversible():
    source=MIGRATION.read_text(encoding="utf-8")
    assert "PAY-6 downgrade blocked: offline payment evidence exists" in source
    assert "app_private.pay6_offline_payment_acl_delta" in source
    assert "REVOKE {privilege} ({column_name})" in source
    assert "DROP TABLE app_private.pay6_offline_payment_acl_delta" in source


def test_pay6_router_is_registered_and_idempotency_is_mandatory():
    router=ROUTER.read_text(encoding="utf-8")
    main=MAIN.read_text(encoding="utf-8")
    assert 'alias="X-Idempotency-Key"' in router
    assert "OFFLINE_PAYMENT_IDEMPOTENCY_REQUIRED" in router
    assert "require_org_admin" in router
    assert "finance_offline_payments.router" in main
