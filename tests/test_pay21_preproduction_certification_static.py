from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "docs/architecture/pay21_preproduction_contract_v1.json"
DOC = ROOT / "docs/architecture/PAY21_PREPRODUCTION_CERTIFICATION.md"
WORKFLOW = ROOT / ".github/workflows/pay21-preproduction-certification.yml"
RUNTIME = ROOT / "scripts/ci/run_pay21_preproduction_runtime.sh"
PROVIDER = ROOT / "scripts/ci/pay21_razorpay_test_mode_probe.py"


def contract() -> dict:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def test_pay21_is_bound_to_exact_pay20_candidate_without_new_migration() -> None:
    c = contract()
    assert c["phase"] == "PAY-21"
    assert c["predecessor"] == {
        "phase": "PAY-20",
        "sha": "5abfeb7a9a3cee44fc362e1a80f74599a6f694ee",
        "tree": "996da603e4cdd088e91e0fef1ecc0dc7d7cddfea",
        "alembic_head": "zz47d8e9f0a64",
    }
    assert c["alembic_head"] == "zz47d8e9f0a64"
    assert c["new_migration"] is False
    assert c["no_new_money_authority"] is True


def test_pay21_requires_every_requested_preproduction_component() -> None:
    env = contract()["environment"]
    assert env == {
        "postgresql_major": 16,
        "redis_major": 7,
        "celery": "real",
        "tls_required": True,
        "secrets_system": "github_actions_encrypted_secrets",
        "provider": "razorpay",
        "provider_mode": "test",
        "provider_api": "https://api.razorpay.com/v1",
        "production_images": True,
        "production_db_roles": True,
        "private_service_network": True,
        "only_tls_ingress_published": True,
    }


def test_pay21_covers_full_requested_e2e_matrix() -> None:
    assert set(contract()["scenarios"]) == {
        "member_admission",
        "online_payment",
        "cash_payment",
        "partial_payment",
        "renewal",
        "refund",
        "partial_refund",
        "settlement",
        "failed_payment",
        "platform_subscription",
        "recurring_attempt",
        "dunning",
        "mandate_revocation",
        "dispute_simulation",
        "backup_restore",
        "bad_deployment_rollback",
    }


def test_pay21_provider_probe_is_real_test_mode_and_secret_safe() -> None:
    source = PROVIDER.read_text(encoding="utf-8")
    for token in (
        "RazorpayTestModeHTTPTransport",
        "RazorpayTestModeOrdersClient",
        "https://api.razorpay.com/v1",
        "rzp_test_",
        "PAY21_RAZORPAY_TEST_ACCOUNT=PASS",
        "PAY21_RAZORPAY_REAL_HTTPS=PASS",
        "PAY21_REAL_MONEY_MOVEMENT=0",
    ):
        assert token in source
    assert "rzp_live_" in source
    assert "key_secret" not in source.split('record = {', 1)[1]


def test_pay21_runtime_uses_private_network_tls_real_celery_and_same_image() -> None:
    source = RUNTIME.read_text(encoding="utf-8")
    for token in (
        "docker network create",
        "redis:7-alpine",
        "tls-port 6379",
        "maxmemory-policy noeviction",
        "python -m celery",
        "DOERS_PROCESS_PROFILE=api",
        "DOERS_PROCESS_PROFILE=worker",
        "nginx:1.27-alpine",
        "https://localhost:8443/_system/ready",
        "test -z \"$(docker port pay21-api)\"",
        "test -z \"$(docker port pay21-worker)\"",
        "test -z \"$(docker port pay21-redis)\"",
        "API_IMAGE",
        "WORKER_IMAGE",
        "PAY21_PRODUCTION_DB_ROLES=PASS",
        "PAY21_NETWORK_CONTROLS=PASS",
    ):
        assert token in source


def test_pay21_workflow_requires_secret_injection_recovery_and_all_scenarios() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    for token in (
        "secrets.PAY21_RAZORPAY_TEST_KEY_ID",
        "secrets.PAY21_RAZORPAY_TEST_KEY_SECRET",
        "secrets.PAY21_RAZORPAY_TEST_WEBHOOK_SECRET",
        "pay21_razorpay_test_mode_probe.py",
        "run_pay21_preproduction_runtime.sh",
        "pay19_seed_financial_state.py",
        "pay19_financial_fingerprint.sql",
        "pg_dump",
        "pg_restore",
        "p9d-bad-deployment-rollback.yml",
        "test_fully_applied_payment_activates_exactly_once_under_concurrency",
        "test_checker_approval_creates_canonical_payment_allocation_ledger_and_paid_event",
        "test_partial_offline_payment_updates_invoice_but_does_not_emit_paid_event",
        "test_partial_and_full_explicit_settlement_update_invoice_status",
        "test_pay12_pg16_durable_lifecycle_invariants",
        "test_pay13_open_dispute_preserves_capture_and_freezes_money_actions",
        "test_pay14_authoritative_three_way_match_closes_without_money_mutation",
        "PAY21_PREPRODUCTION_CERTIFICATION=PASS",
    ):
        assert token in source


def test_pay21_safety_forbids_production_data_credentials_and_money() -> None:
    c = contract()
    assert c["provider_proof"]["live_key_forbidden"] is True
    assert c["provider_proof"]["real_money_forbidden"] is True
    assert c["provider_proof"]["production_customer_data_forbidden"] is True
    assert c["release_safety"] == {
        "production_credentials": False,
        "production_customer_data": False,
        "live_money_movement": False,
        "production_deployment_authorized": False,
    }
    doc = DOC.read_text(encoding="utf-8")
    assert "No live provider key" in doc
    assert "production customer" in doc.lower()
