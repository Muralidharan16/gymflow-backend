from __future__ import annotations

from pathlib import Path

import pytest

from app.finance_core.domain.operational_guards import FinanceOperationalPosture
from app.finance_core.services.operational_guards import FinanceOperationalGuardService
from tests.finance_core.test_phase5c_invoice_engine import fetch_one
from tests.finance_core.test_phase5d_payment_ledger import seed_finance_foundation


def test_current_internal_only_posture_passes_preflight():
    report = FinanceOperationalGuardService().preflight()

    assert report.safe is True
    assert report.safety_mode == "internal_only"
    assert report.failures == ()
    assert report.live_provider_enabled is False
    assert report.live_money_movement_enabled is False


@pytest.mark.parametrize(
    ("posture", "expected_code"),
    [
        (
            FinanceOperationalPosture(live_provider_enabled=True),
            "finance.live_provider.disabled",
        ),
        (
            FinanceOperationalPosture(live_money_movement_enabled=True),
            "finance.live_money_movement.disabled",
        ),
        (
            FinanceOperationalPosture(subscription_automation_enabled=True),
            "finance.subscription_automation.disabled",
        ),
        (
            FinanceOperationalPosture(customer_facing_checkout_enabled=True),
            "finance.customer_facing_checkout.disabled",
        ),
        (
            FinanceOperationalPosture(public_payment_routes_enabled=True),
            "finance.public_payment_routes.disabled",
        ),
        (
            FinanceOperationalPosture(kill_switch_live_actions_disabled=False),
            "finance.kill_switch.required",
        ),
    ],
)
def test_forbidden_operational_postures_fail_preflight(posture: FinanceOperationalPosture, expected_code: str):
    report = FinanceOperationalGuardService(posture).preflight()

    assert report.safe is False
    assert expected_code in {failure.code for failure in report.failures}


def test_unapproved_safety_mode_fails_without_enabling_live_behavior():
    report = FinanceOperationalGuardService(FinanceOperationalPosture(safety_mode="production")).preflight()  # type: ignore[arg-type]

    assert report.safe is False
    assert report.failures[0].code == "finance.safety_mode.not_allowed"
    assert report.live_provider_enabled is False
    assert report.live_money_movement_enabled is False


def test_failure_reasons_are_sanitized_and_contain_no_secrets():
    report = FinanceOperationalGuardService(
        FinanceOperationalPosture(
            safety_mode="production",  # type: ignore[arg-type]
            live_provider_enabled=True,
            live_money_movement_enabled=True,
            subscription_automation_enabled=True,
            customer_facing_checkout_enabled=True,
            public_payment_routes_enabled=True,
            kill_switch_live_actions_disabled=False,
        )
    ).preflight()
    combined = " ".join([failure.code + " " + failure.message for failure in report.failures]).lower()

    assert report.safe is False
    assert "secret" not in combined
    assert "key" not in combined
    assert "token" not in combined
    assert "password" not in combined
    assert "rzp_live_" not in combined


def test_guard_status_report_exposes_safe_fields_only():
    report = FinanceOperationalGuardService().preflight()
    report_fields = set(report.__dataclass_fields__)

    assert "provider_secret" not in report_fields
    assert "api_key" not in report_fields
    assert "webhook_secret" not in report_fields
    assert report_fields == {
        "safe",
        "safety_mode",
        "live_provider_enabled",
        "live_money_movement_enabled",
        "subscription_automation_enabled",
        "customer_facing_checkout_enabled",
        "public_payment_routes_enabled",
        "kill_switch_live_actions_disabled",
        "failures",
    }


def test_kill_switch_status_defaults_to_blocking_live_actions():
    status = FinanceOperationalGuardService().kill_switch_status()

    assert status.live_provider_actions_allowed is False
    assert status.live_money_movement_allowed is False
    assert status.subscription_automation_allowed is False
    assert status.customer_facing_payment_allowed is False


@pytest.mark.asyncio
async def test_guard_preflight_does_not_mutate_finance_tables():
    await seed_finance_foundation()
    before = await _finance_counts()

    service = FinanceOperationalGuardService()
    assert service.preflight().safe is True
    assert service.kill_switch_status().live_money_movement_allowed is False

    after = await _finance_counts()
    assert after == before


async def _finance_counts():
    return await fetch_one(
        """
        SELECT
            (SELECT count(*) FROM finance.invoices) AS invoices,
            (SELECT count(*) FROM finance.payments) AS payments,
            (SELECT count(*) FROM finance.payment_allocations) AS allocations,
            (SELECT count(*) FROM finance.ledger_entries) AS ledger_entries,
            (SELECT count(*) FROM finance.credit_notes) AS credit_notes,
            (SELECT count(*) FROM finance.refunds) AS refunds,
            (SELECT count(*) FROM finance.outbox_events) AS outbox_events
        """
    )


def test_phase5l_has_no_live_provider_frontend_or_production_enablement():
    repo_root = Path(__file__).resolve().parents[2]
    finance_root = repo_root / "app" / "finance_core"
    finance_files = [
        *(finance_root / "domain").rglob("*.py"),
        *(finance_root / "repositories").rglob("*.py"),
        *(finance_root / "services").rglob("*.py"),
    ]
    combined = "\n".join(path.read_text(encoding="utf-8").lower() for path in finance_files)
    assert "rzp_live_" not in combined
    assert "requests" not in combined
    assert "httpx" not in combined
    assert "aiohttp" not in combined
    assert "provider_secret" not in combined
    assert "activate_subscription" not in combined
    assert "deactivate_subscription" not in combined
    assert "platform_subscriptions" not in combined
    assert not (repo_root / "frontend").exists()
