from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/zr07d8e9f0a52_pay8_durable_checkout_webhooks.py"
PAYMENT_API = ROOT / "app/finance_core/api/payment_boundary.py"
CHECKOUT = ROOT / "app/finance_core/services/checkout_orchestration.py"
MEMBER_CHECKOUT = ROOT / "app/finance_core/services/member_subscription_checkout.py"
OPERATIONS = ROOT / "app/finance_core/services/provider_operations.py"
WEBHOOKS = ROOT / "app/finance_core/services/razorpay_webhooks.py"
INBOX = ROOT / "app/finance_core/services/provider_webhook_inbox.py"
MODELS = ROOT / "app/finance_core/models/foundation.py"


def test_pay8_is_one_additive_revision_over_certified_pay7():
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'revision = "zr07d8e9f0a52"' in source
    assert 'down_revision = "zq07d8e9f0a51"' in source
    assert "CREATE TABLE finance.provider_operations" in source
    assert "CREATE TABLE finance.provider_webhook_inbox" in source
    for forbidden in (
        "DROP TABLE finance.payments",
        "DROP TABLE finance.payment_events",
        "ALTER TABLE finance.payments ALTER COLUMN",
        "UPDATE finance.payments SET organization_id",
        "TRUNCATE TABLE",
    ):
        assert forbidden not in source


def test_provider_operation_schema_fences_tenant_idempotency_and_unknown_outcome():
    source = MIGRATION.read_text(encoding="utf-8")
    models = MODELS.read_text(encoding="utf-8")

    assert "fk_pay8_provider_operation_payment_org" in source
    assert "FOREIGN KEY(payment_id,organization_id)" in source
    assert "REFERENCES finance.payments(id,organization_id)" in source
    assert "fk_pay8_provider_operation_payment_org" in models
    assert "uq_pay8_provider_operation_key" in source
    assert "uq_pay8_provider_operation_payment" in source
    assert "uq_pay8_provider_object" in source
    assert "'reserved','in_flight','succeeded'," in source
    assert "'failed_retryable','failed_final','unknown'" in source
    assert "lease_fence BIGINT NOT NULL DEFAULT 0" in source
    assert "lease_expired_unknown" in source
    assert "v_operation.status NOT IN (" in source
    assert "'reserved','failed_retryable'" in source


def test_provider_completion_is_fenced_and_success_binds_payment_atomically():
    source = MIGRATION.read_text(encoding="utf-8")
    body = source.split(
        "CREATE FUNCTION app_secure.finish_finance_provider_operation", 1
    )[1].split("$function$", 2)[1]

    assert "v_operation.status<>'in_flight'" in body
    assert "v_operation.lease_owner" in body
    assert "v_operation.lease_fence" in body
    assert "PAY-8 provider operation fence conflict" in body
    assert "UPDATE finance.payments" in body
    assert "provider_order_ref=p_provider_object_id" in body
    assert "UPDATE finance.provider_operations" in body
    assert "status=p_outcome" in body
    assert "provider_evidence_sha256" in body


def test_unknown_operation_requires_reconciliation_runtime_and_cannot_be_claimed():
    source = MIGRATION.read_text(encoding="utf-8")
    reconcile = source.split(
        "CREATE FUNCTION app_secure.reconcile_finance_provider_operation", 1
    )[1].split("$function$", 2)[1]
    grants = source.split("for signature in (", 1)[1]

    assert "finance_reconciliation_runtime" in reconcile
    assert "v_operation.status<>'unknown'" in reconcile
    assert "p_outcome NOT IN ('succeeded','failed_final')" in reconcile
    assert "GRANT EXECUTE ON FUNCTION {_RECONCILE_OPERATION}" in source
    assert "TO finance_reconciliation_runtime" in source
    assert (
        'f"GRANT EXECUTE ON FUNCTION {_RECONCILE_OPERATION} "\n'
        '            "TO app_runtime"'
    ) not in source


def test_checkout_http_route_commits_local_authority_before_provider_io():
    source = PAYMENT_API.read_text(encoding="utf-8")
    route = source.split(
        "async def create_checkout_session(", 1
    )[1].split("@router.get", 1)[0]

    prepare = route.index("prepare_checkout_session")
    first_commit = route.index("await db.commit()", prepare)
    claim = route.index("claim_provider_operation", first_commit)
    second_commit = route.index("await db.commit()", claim)
    provider_call = route.index("call_provider", second_commit)
    finish = route.index("finish_provider_success", provider_call)
    final_commit = route.index("await db.commit()", finish)

    assert prepare < first_commit < claim < second_commit < provider_call < finish < final_commit
    assert "provider_operation.status not in" in route
    assert '"reserved"' in route
    assert '"failed_retryable"' in route
    assert "FINANCE_PROVIDER_OUTCOME_UNKNOWN" in source


def test_business_checkout_uses_durable_provider_operation_service():
    checkout = CHECKOUT.read_text(encoding="utf-8")
    member = MEMBER_CHECKOUT.read_text(encoding="utf-8")
    operations = OPERATIONS.read_text(encoding="utf-8")

    for source in (checkout, member):
        assert "FinanceProviderOperationService" in source
        assert "reserve_checkout(" in source
        assert "claim_provider_operation" in source
        assert "finish_provider_success" in source
        assert "finish_provider_error" in source

    assert "provider_checkout_request_hash" in operations
    assert "provider_checkout_success_hash" in operations
    assert "reconcile_unknown" in operations
    assert "raw_body" not in operations
    assert "authorization" not in operations.lower()


def test_webhook_signature_normalization_precedes_durable_inbox_recording():
    source = WEBHOOKS.read_text(encoding="utf-8")
    method = source.split("async def record_verified_webhook(", 1)[1].split(
        "async def claim_recorded_webhook", 1
    )[0]
    normalize = method.index("self.normalize(webhook)")
    record = method.index("self._inbox.record_verified(")
    assert normalize < record

    normalize_body = source.split("def normalize(", 1)[1]
    assert "verify_razorpay_webhook_signature" in normalize_body
    assert "Invalid Razorpay webhook signature" in normalize_body


def test_webhook_inbox_deduplicates_exact_evidence_and_conflicts_changed_replay():
    source = MIGRATION.read_text(encoding="utf-8")
    body = source.split(
        "CREATE FUNCTION app_secure.record_finance_provider_webhook", 1
    )[1].split("$function$", 2)[1]

    assert "uq_pay8_webhook_provider_event" in source
    assert "ON CONFLICT DO NOTHING" in body
    for field in (
        "payload_sha256",
        "signature_sha256",
        "event_type",
        "provider_order_ref",
        "provider_payment_ref",
        "provider_amount_subunits",
        "provider_currency",
        "provider_payment_status",
        "provider_captured",
        "provider_payment_order_ref",
        "provider_order_entity_ref",
        "provider_order_status",
        "provider_event_timestamp",
    ):
        assert field in body
    assert "PAY-8 webhook event replay conflict" in body


def test_webhook_processing_is_fenced_reclaimable_and_payment_runtime_recoverable():
    source = MIGRATION.read_text(encoding="utf-8")
    claim = source.split(
        "CREATE FUNCTION app_secure.claim_finance_provider_webhook", 1
    )[1].split("$function$", 2)[1]
    next_claim = source.split(
        "CREATE FUNCTION app_secure.claim_next_finance_provider_webhook", 1
    )[1].split("$function$", 2)[1]
    complete = source.split(
        "CREATE FUNCTION app_secure.complete_finance_provider_webhook", 1
    )[1].split("$function$", 2)[1]

    assert "lease_expired_retry" in claim
    assert "processing_attempts=processing_attempts+1" in claim
    assert "lease_fence=lease_fence+1" in claim
    assert "FOR UPDATE SKIP LOCKED" in next_claim
    assert "finance_payment_runtime" in next_claim
    assert "v_row.lease_owner" in complete
    assert "v_row.lease_fence" in complete
    assert "PAY-8 webhook completion fence conflict" in complete


def test_webhook_route_durably_records_then_claims_before_finance_mutation():
    source = PAYMENT_API.read_text(encoding="utf-8")
    route = source.split(
        "async def receive_razorpay_webhook(", 1
    )[1].split("@router.post(\"/internal/payment-applications\"", 1)[0]

    record = route.index("record_verified_webhook")
    record_commit = route.index("await db.commit()", record)
    claim = route.index("claim_recorded_webhook", record_commit)
    claim_commit = route.index("await db.commit()", claim)
    process = route.index("process_claimed_webhook", claim_commit)
    complete = route.index("complete_claimed_webhook", process)
    process_commit = route.index("await db.commit()", complete)

    assert record < record_commit < claim < claim_commit < process < complete < process_commit
    assert "await db.rollback()" in route
    assert "fail_claimed_webhook" in route
    assert '"provider_processing_retry"' in route


def test_webhook_completion_reads_only_declared_predecessor_event_columns():
    source = MIGRATION.read_text(encoding="utf-8")
    body = source.split(
        "CREATE FUNCTION app_secure.complete_finance_provider_webhook", 1
    )[1].split("$function$", 2)[1]

    assert "SELECT\n                    e.id," in body
    assert "e.payment_id" in body
    assert "e.provider_code" in body
    assert "e.provider_event_id" in body
    assert "SELECT e.*" not in body


def test_runtime_roles_have_capabilities_but_no_direct_provider_table_dml():
    source = MIGRATION.read_text(encoding="utf-8")

    assert "PAY-8 leaked direct durable provider DML" in source
    for role in (
        "_APP",
        "_WORKER",
        "_PAYMENT_RUNTIME",
        "_RECON_RUNTIME",
    ):
        assert role in source
    assert "REVOKE ALL ON TABLE finance.provider_operations FROM PUBLIC" in source
    assert "REVOKE ALL ON TABLE finance.provider_webhook_inbox FROM PUBLIC" in source


def test_populated_downgrade_is_fail_closed_and_no_live_activation_exists():
    source = MIGRATION.read_text(encoding="utf-8")
    combined = "\n".join(
        path.read_text(encoding="utf-8").lower()
        for path in (
            MIGRATION,
            PAYMENT_API,
            CHECKOUT,
            MEMBER_CHECKOUT,
            OPERATIONS,
            WEBHOOKS,
            INBOX,
        )
    )

    assert "PAY-8 downgrade blocked: durable provider evidence exists" in source
    assert "live_money_movement = true" not in combined
    assert "live_provider_enabled=true" not in combined
    assert "rzp_live_" not in combined
    assert "refund_provider" not in combined
