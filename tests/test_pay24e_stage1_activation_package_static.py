from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEDULER = ROOT / "app/pay24e_stage1_scheduler.py"
OVERLAY = ROOT / "deploy/docker-compose.pay24e-stage1-canary.yml"
PREFLIGHT = ROOT / "scripts/pay24e_stage1_canary_preflight.py"
RUNBOOK = ROOT / "docs/runbooks/pay24e/stage1-internal-canary.md"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_scheduler_publishes_only_entitlement_apply() -> None:
    source=_text(SCHEDULER)
    assert "app.tasks.entitlement_dispatcher.run" in source
    assert "app.tasks.refund_entitlement_dispatcher.run" not in source
    assert "pay24e-stage1-entitlement-poll" in source
    ast.parse(source)


def test_scheduler_has_no_database_or_provider_credentials() -> None:
    source=_text(SCHEDULER)
    for token in (
        "DATABASE_URL",
        "FINANCE_PAYMENT_DATABASE_URL",
        "FINANCE_CONFIG_DATABASE_URL",
        "ENTITLEMENT_DATABASE_URL",
        "RAZORPAY_KEY_SECRET",
        "AWS_SECRET_ACCESS_KEY",
    ):
        assert token in source
    assert "forbidden credentials" in source


def test_canary_overlay_is_profile_gated_and_zero_replica() -> None:
    source=_text(OVERLAY)
    assert source.count("replicas: 0") == 2
    assert source.count("pay24-stage1-canary") == 2
    assert "celery-entitlement-worker:" in source
    assert "pay24e-entitlement-scheduler:" in source
    assert "PAY24E_STAGE1_SCHEDULER: prepared" in source
    assert "refund_entitlement_dispatcher" not in source


def test_preflight_is_read_only_and_exact_sha_stage_aware() -> None:
    source=_text(PREFLIGHT)
    ast.parse(source)
    for forbidden in (
        "pay24a_transition_activation",
        "pay24a_bind_release_identity",
        "pay24a_bind_human_authorization",
        "INSERT ",
        "UPDATE ",
        "DELETE ",
    ):
        assert forbidden not in source
    assert "PAY24E_EXPECTED_SHA" in source
    assert "PAY24E_EXPECTED_STAGE" in source
    assert "EXPECTED_CAPABILITIES" in source


def test_runbook_requires_stage0_preprovision_and_durable_authority() -> None:
    source=_text(RUNBOOK).lower()
    assert "while pay-24-a is still stage 0" in source
    assert "explicit human authorization" in source
    assert "exactly one internal organization" in source
    assert "pay-24-a postgresql state is authoritative" in source
    assert "begin pay-24-a emergency rollback" in source
