from __future__ import annotations

import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_pay16_authorization_matrix_is_complete_and_fail_closed():
    matrix = yaml.safe_load(
        _source("docs/architecture/pay16_security_authorization_matrix_v1.yaml")
    )
    assert matrix["version"] == 1
    actions = matrix["actions"]

    assert actions["checkout_create"]["allowed_roles"] == ["owner", "admin"]
    assert actions["offline_payment_prepare"]["allowed_roles"] == ["owner", "admin"]
    assert actions["offline_payment_approve"]["allowed_roles"] == ["owner", "admin"]
    assert actions["offline_payment_approve"]["maker_checker"] is True
    assert actions["offline_payment_approve"]["recent_auth_seconds"] == 600
    assert actions["offline_payment_approve"]["velocity"]["max_successes"] == 10
    assert actions["refund_provider_submit"]["public_route"] is False
    assert actions["finance_export"]["public_route"] is False
    assert matrix["default"] == "deny"


def test_privileged_offline_payment_boundary_binds_step_up_csrf_maker_checker_and_reason():
    route = _source("app/finance_core/api/offline_payments.py")
    security = _source("app/finance_core/security_abuse.py")

    assert "finance_high_risk_actor_dependency" in route
    assert "enforce_maker_checker" in route
    assert 'alias="X-Finance-Reason-Code"' in route
    assert "PAY16_APPROVAL_VELOCITY_LIMIT = 10" in route
    assert "FinanceOfflinePaymentRequest.organization_id == org_id" in route
    assert "OFFLINE_PAYMENT_TENANT_NOT_FOUND" in route

    assert "RECENT_AUTH_WINDOW_SECONDS = 10 * 60" in security
    assert 'request.cookies.get("finance_csrf")' in security
    assert 'request.headers.get("X-CSRF-Token")' in security
    assert "verify_finance_csrf_token" in security
    assert "await redis_client.ping()" in security
    assert "FINANCE_SECURITY_DEPENDENCY_UNAVAILABLE" in security


def test_session_rotation_carries_family_and_original_authentication_time():
    core = _source("app/core/security.py")
    service = _source("app/services/auth_service.py")
    router = _source("app/routers/auth.py")

    assert '"auth_time": authenticated_at' in core
    assert 'payload["f_id"] = str(family_id)' in core
    assert 'refresh_payload.get("auth_time")' in service
    assert "family_id=str(family.id)" in service
    assert "finance_csrf_token(tokens.access_token)" in router
    assert "httponly=False" in router
    assert 'key="refresh_token"' in router
    assert 'path="/auth"' in router


def test_webhook_boundary_has_signature_rotation_event_id_and_future_skew_defenses():
    domain = _source("app/finance_core/domain/razorpay_sandbox.py")
    service = _source("app/finance_core/services/razorpay_webhooks.py")
    inbox = _source("app/finance_core/services/provider_webhook_inbox.py")

    assert "previous_webhook_secret" in domain
    assert '"previous_webhook_secret": (' in domain
    assert "hmac.compare_digest" in domain

    assert "_authoritative_event_id" in service
    assert "previous_secret_valid" in service
    assert "MAX_RAZORPAY_FUTURE_SKEW_SECONDS = 300" in service
    assert "provider_event_timestamp > int(time.time())" in service
    assert "record_verified" in service
    assert "provider_event_id" in inbox
    assert "idempotency" in inbox.lower()


def test_amount_currency_reference_and_refund_abuse_guards_remain_server_authoritative():
    capture = _source("app/finance_core/services/provider_capture_confirmation.py")
    refund = _source("app/finance_core/services/payment_ledger.py")
    provider = _source("app/finance_core/domain/provider_boundary.py")

    assert "PROVIDER_AMOUNT_MISMATCH" in capture
    assert "PROVIDER_CURRENCY_MISMATCH" in capture
    assert "PROVIDER_EVIDENCE_REFERENCE_MISMATCH" in capture
    assert "PROVIDER_EVIDENCE_PAYMENT_MISMATCH" in capture

    assert "allocated_payment_total(payment.id)" in refund
    assert "refunded_payment_total(payment.id)" in refund
    assert "if amount > eligible_amount" in refund
    assert "Refund amount cannot exceed eligible applied payment amount" in refund

    assert "The adapter never accepts browser-selected payment, amount" in provider
    assert "ProviderRefundRequest" in provider


def test_live_provider_and_money_movement_stay_fail_closed():
    guards = _source("app/finance_core/api/guards.py")
    assert "FINANCE_PAYMENT_API_ENABLED = False" in guards
    assert "live_provider_enabled: bool = False" in guards
    assert "live_money_movement_enabled: bool = False" in guards
    assert "production_payment_application_enabled: bool = False" in guards


def test_finance_models_and_schemas_do_not_store_card_pan_cvv_or_upi_pin():
    # "PAN" in the PAY-16 payment-data threat model means a card Primary
    # Account Number. Finance legitimately stores 10-character Indian tax PANs
    # for legal entities/billing parties; those are not payment credentials.
    forbidden_payment_secret = re.compile(
        r"(?i)\b(?:card_number|card_pan|primary_account_number|cvv|cvc|upi_pin|card_security_code)\b"
    )
    targets = [
        *sorted((ROOT / "app" / "finance_core" / "models").glob("*.py")),
        ROOT / "app" / "finance_core" / "api" / "schemas.py",
    ]
    violations = []
    for path in targets:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''"):
                continue
            if forbidden_payment_secret.search(stripped):
                violations.append(f"{path.relative_to(ROOT)}:{lineno}:{stripped}")
    assert violations == []

    foundation = _source("app/finance_core/models/foundation.py")
    assert "class FinanceLegalEntity" in foundation
    assert "pan: Mapped[str | None] = mapped_column(String(10)" in foundation
    assert "class FinanceBillingParty" in foundation


def test_provider_hosted_collection_exposes_only_public_checkout_fields():
    domain = _source("app/finance_core/domain/razorpay_sandbox.py")
    assert 'return {"key": self.key_id, "order_id": self.order_id}' in domain
    match = re.search(
        r"class RazorpayCheckoutFields:.*?def verify_razorpay_checkout_signature",
        domain,
        flags=re.S,
    )
    assert match is not None
    assert '"key_secret"' not in match.group(0)


def test_security_logging_contract_forbids_secret_payload_material():
    security = _source("app/finance_core/security_abuse.py")
    logging_block = security.split("def security_event(", 1)[1]
    extra_block = logging_block.split("extra={", 1)[1].split("}", 1)[0]
    for forbidden in (
        "access_token",
        "raw_body",
        "signature",
        "webhook_secret",
        "key_secret",
        "refresh_token",
        "cvv",
        "upi_pin",
    ):
        assert forbidden not in extra_block


def test_no_public_finance_export_or_refund_submission_route_is_introduced():
    api_root = ROOT / "app" / "finance_core" / "api"
    route_source = "\n".join(
        path.read_text(encoding="utf-8").lower()
        for path in api_root.glob("*.py")
    )
    assert '@router.post("/refund' not in route_source
    assert '@router.get("/export' not in route_source
    assert '@router.post("/export' not in route_source
