from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/zze7d8e9f0a74_pay24g_live_payment_authority.py"
LIVE_SERVICE = ROOT / "app/finance_core/services/razorpay_live.py"
EVIDENCE = ROOT / "app/finance_core/services/pay24g_live_payment_evidence.py"
ROUTES = ROOT / "app/finance_core/api/payment_boundary.py"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_pay24g_live_evidence_is_reconciliation_runtime_only() -> None:
    source=_text(MIGRATION)
    assert 'revision = "zze7d8e9f0a74"' in source
    assert 'down_revision = "zzd7d8e9f0a73"' in source
    assert "finance_reconciliation_runtime" in source
    assert "p_provider_code = 'razorpay_sandbox'" in source
    assert "p_provider_code = 'razorpay'" in source
    assert "GRANT EXECUTE ON FUNCTION" in source
    assert "TO finance_reconciliation_runtime" in source


def test_pay24g_live_evidence_and_application_recheck_stage1() -> None:
    source=_text(MIGRATION)
    assert "PAY-24-G live provider evidence denied by Stage-1 authority" in source
    assert "PAY-24-G live payment application denied by Stage-1 authority" in source
    assert source.count("a.stage=1") == 2
    assert source.count("a.provider_egress_state='open'") == 2
    assert source.count("a.internal_organization_id=v_payment.organization_id") == 2
    assert source.count("a.payment_application IS TRUE") == 2
    assert source.count("a.authorized_sha=a.certified_sha") == 2


def test_pay24g_live_provider_fetch_is_read_only() -> None:
    source=_text(LIVE_SERVICE)
    assert "/payments/{normalized}" in source
    assert 'connection.request("GET"' in source
    assert "/capture" not in source
    assert "/refund" not in source


def test_pay24g_terminal_evidence_requires_signature_and_api_capture() -> None:
    source=_text(EVIDENCE)
    assert "verify_razorpay_checkout_signature" in source
    assert "fetch_payment(" in source
    assert 'payment.status != "captured"' in source
    assert "payment.captured is not True" in source
    assert 'provider_code="razorpay"' in source
    assert 'event_type="payment.captured"' in source


def test_pay24g_adds_no_public_live_http_route() -> None:
    routes=_text(ROUTES)
    assert "FinanceTerminalLivePaymentEvidenceService" not in routes
    assert "RazorpayLivePaymentsClient" not in routes

def test_pay24g_reconciliation_uses_only_nested_scoped_idempotency() -> None:
    source=_text(MIGRATION)
    assert "p_scope = 'finance.provider.capture.confirm'" in source
    assert "PAY-24-G finance idempotency completion scope unavailable" in source
    assert "pg_catalog.aclexplode(" in source
    assert "acl.privilege_type='EXECUTE'" in source
    assert "reconciliation must not directly execute" in source
    assert (
        "GRANT EXECUTE ON FUNCTION\n"
        "                app_secure.reserve_finance_idempotency"
    ) not in source
    assert (
        "GRANT EXECUTE ON FUNCTION\n"
        "                app_secure.complete_finance_idempotency"
    ) not in source

