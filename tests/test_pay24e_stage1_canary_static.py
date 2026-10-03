from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/zzc7d8e9f0a72_pay24e_stage1_internal_canary.py"
TASK = ROOT / "app/tasks/entitlement_dispatcher.py"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_pay24e_migration_does_not_activate_stage1() -> None:
    source=_text(MIGRATION)
    assert 'revision = "zzc7d8e9f0a72"' in source
    assert 'down_revision = "zzb7d8e9f0a71"' in source
    assert "pay24a_transition_activation(" not in source
    assert "pay24a_bind_human_authorization(" not in source
    assert "UPDATE finance.payment_activation_authority" not in source


def test_stage0_claim_is_empty_and_old_claim_is_revoked() -> None:
    source=_text(MIGRATION)
    assert "IF v_authority.stage=0 THEN" in source
    assert "REVOKE EXECUTE ON FUNCTION {_OLD_CLAIM}" in source
    task=_text(TASK)
    assert "pay24e_claim_entitlement_commands" in task
    assert "pay24c_claim_entitlement_commands" not in task


def test_stage1_scope_is_exact_internal_org_and_minimal_four_switches() -> None:
    source=_text(MIGRATION)
    for token in (
        "v_authority.stage<>1",
        "v_authority.provider_egress_state<>'open'",
        "c.organization_id=v_authority.internal_organization_id",
        "v_authority.checkout IS NOT TRUE",
        "v_authority.webhooks IS NOT TRUE",
        "v_authority.payment_application IS NOT TRUE",
        "v_authority.subscription_activation IS NOT TRUE",
        "v_authority.refund_execution IS NOT FALSE",
        "v_authority.recurring_billing IS NOT FALSE",
        "v_authority.dunning IS NOT FALSE",
        "v_authority.platform_billing IS NOT FALSE",
        "v_authority.deployed_sha",
        "v_authority.authorized_sha",
    ):
        assert token in source


def test_protected_mutations_recheck_stage1_authority() -> None:
    source=_text(MIGRATION)
    for guard in (
        "pay24c_guard_term_mutation",
        "pay24c_guard_v2_mutation",
        "pay24c_guard_freeze_mutation",
    ):
        assert f"CREATE OR REPLACE FUNCTION app_secure.{guard}()" in source
    assert source.count("pay24e_assert_stage1_entitlement_authority") >= 5
    assert "PAY-24-E Stage-1 entitlement authority denied" in source
