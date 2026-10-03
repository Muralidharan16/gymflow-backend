from __future__ import annotations

import ast
import json
from datetime import datetime, timezone
import uuid

from app.payment_activation.domain import (
    ActivationAuthorization,
    ActivationCapability,
    ActivationRuntime,
    ActivationStage,
    KillSwitches,
    ProductionActivationPolicy,
)
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/zzb7d8e9f0a71_pay24d_stage1_readiness_observability.py"
CONTRACT = ROOT / "docs/architecture/pay24d_stage1_readiness_v1.json"
DESIGN = ROOT / "docs/architecture/PAY24D_STAGE1_READINESS.md"
OVERLAY = ROOT / "deploy/docker-compose.pay24-stage1-readiness.yml"
CURRENT_OVERLAY = ROOT / "deploy/docker-compose.production-identities.yml"
CELERY = ROOT / "app/core/celery_app.py"
CONFIG = ROOT / "app/core/config.py"
METRICS = ROOT / "app/observability/runtime_metrics.py"
BOOTSTRAP = ROOT / "app/observability/metrics_bootstrap.py"
ENTITLEMENT_TASK = ROOT / "app/tasks/entitlement_dispatcher.py"
RULES = ROOT / "ops/observability/pay24d_stage1_rules.yml"
RUNBOOK = ROOT / "docs/runbooks/pay24d/entitlement-stage1.md"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _assignment(name: str):
    module=ast.parse(_text(MIGRATION))
    for node in module.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(name)


def test_pay24d_revision_and_stage0_authority_are_frozen() -> None:
    assert _assignment("revision") == "zzb7d8e9f0a71"
    assert _assignment("down_revision") == "zza7d8e9f0a70"
    migration=_text(MIGRATION)
    assert "pay24a_transition_activation" not in migration
    assert "provider_egress_state" not in migration
    assert "UPDATE finance.payment_activation_authority" not in migration


def test_stage1_entitlement_deployment_template_is_disabled_and_secret_isolated() -> None:
    overlay=_text(OVERLAY)
    current=_text(CURRENT_OVERLAY)
    assert "celery-entitlement-worker:" in overlay
    assert "pay24-stage1-readiness" in overlay
    assert "replicas: 0" in overlay
    assert "DOERS_PROCESS_PROFILE: entitlement_worker" in overlay
    assert "CELERY_WORKER_PROFILE: entitlement" in overlay
    assert "ENTITLEMENT_DATABASE_URL: ${PAY24_ENTITLEMENT_DATABASE_URL:?required}" in overlay
    for name in (
        "DATABASE_URL","AUTH_DATABASE_URL","WORKER_DATABASE_URL",
        "MAINTENANCE_DATABASE_URL","FINANCE_PAYMENT_DATABASE_URL",
        "FINANCE_CONFIG_DATABASE_URL",
    ):
        assert f'{name}: ""' in overlay
    for name in (
        "AWS_ACCESS_KEY_ID","AWS_SECRET_ACCESS_KEY","RAZORPAY_KEY_ID",
        "RAZORPAY_KEY_SECRET","RAZORPAY_WEBHOOK_SECRET","P4C_RESEND_API_KEY",
        "RESEND_WEBHOOK_SECRET",
    ):
        assert f'{name}: ""' in overlay
    assert "celery-entitlement-worker:" not in current
    assert current.count('ENTITLEMENT_DATABASE_URL: ""') >= 5


def test_entitlement_runtime_requires_tls_and_observability() -> None:
    config=_text(CONFIG)
    bootstrap=_text(BOOTSTRAP)
    overlay=_text(OVERLAY)
    assert "ssl=verify-full" in config
    assert "sslmode=verify-full" in config
    assert "forbidden provider/cloud secrets" in config
    assert '"entitlement_worker"' in bootstrap
    assert "P8_METRICS_OTLP_ENDPOINT" in overlay


def test_stage1_tasks_are_explicitly_routed_but_not_scheduled() -> None:
    source=_text(CELERY)
    route_block=source.split("task_routes=",1)[1].split("class RuntimeDatabaseIdentityBootstep",1)[0]
    assert '"app.tasks.entitlement_dispatcher.run": {"queue": ENTITLEMENT_QUEUE}' in route_block
    assert '"app.tasks.refund_entitlement_dispatcher.run": {"queue": WORKER_QUEUE}' in route_block
    schedule=source.split("celery_app.conf.beat_schedule = {",1)[1]
    assert '"app.tasks.entitlement_dispatcher.run"' not in schedule
    assert '"app.tasks.refund_entitlement_dispatcher.run"' not in schedule


def test_pay24d_observability_is_aggregate_and_non_authoritative() -> None:
    migration=_text(MIGRATION)
    metrics=_text(METRICS)
    task=_text(ENTITLEMENT_TASK)
    rules=_text(RULES)
    assert "pay24d_entitlement_readiness_snapshot" in migration
    function=migration.split("CREATE FUNCTION app_secure.pay24d_entitlement_readiness_snapshot",1)[1].split("$function$",2)[1]
    for token in ("INSERT ","UPDATE ","DELETE ","TRUNCATE "):
        assert token not in function.upper()
    for token in ("pending_count","processing_count","failed_count","review_required_count","oldest_pending_age_seconds","expired_processing_leases"):
        assert token in migration
    assert "doers.entitlement.commands" in metrics
    assert "doers.entitlement.oldest_pending_age" in metrics
    assert "doers.entitlement.expired_processing_leases" in metrics
    assert "Observability never becomes entitlement business authority" in task
    assert "DoersEntitlementWorkerMissing" in rules
    assert "DoersEntitlementCommandBacklogOld" in rules


def test_stage1_rollback_runbook_is_fail_closed() -> None:
    runbook=_text(RUNBOOK).lower()
    for phrase in (
        "begin-emergency-rollback",
        "provider egress is `closing`",
        "scale the entitlement worker to zero",
        "do not purge",
        "admitted=0",
        "active=0",
        "finalize pay-24-a rollback",
    ):
        assert phrase in runbook
    assert "unknown provider outcome as if it failed" in runbook



def test_stage1_internal_canary_is_exact_sha_authorized_and_kill_switched() -> None:
    certified_sha = "d" * 40
    internal = uuid.UUID("24d00000-0000-4000-8000-000000000001")
    outsider = uuid.UUID("24d00000-0000-4000-8000-000000000002")
    authorization = ActivationAuthorization(
        authorization_id="PAY24D-STATIC-STAGE1",
        authorized_sha=certified_sha,
        authorized_stage=ActivationStage.INTERNAL_ORGANIZATION,
        authorized_by="human-release-approver",
        authorized_at=datetime(2026, 10, 3, tzinfo=timezone.utc),
    )

    blocked = ActivationRuntime(
        certified_sha=certified_sha,
        deployed_sha=certified_sha,
        stage=ActivationStage.INTERNAL_ORGANIZATION,
        live_provider_configured=True,
        provider_egress_enabled=False,
        kill_switches=KillSwitches(),
        authorization=authorization,
        internal_organization_id=internal,
    )
    blocked_decision = ProductionActivationPolicy(blocked).decide(
        ActivationCapability.SUBSCRIPTION_ACTIVATION,
        organization_id=internal,
    )
    assert blocked_decision.allowed is False
    assert blocked_decision.code == (
        "activation.kill_switch.subscription_activation.disabled"
    )

    enabled = ActivationRuntime(
        certified_sha=certified_sha,
        deployed_sha=certified_sha,
        stage=ActivationStage.INTERNAL_ORGANIZATION,
        live_provider_configured=True,
        provider_egress_enabled=False,
        kill_switches=KillSwitches(subscription_activation=True),
        authorization=authorization,
        internal_organization_id=internal,
    )
    policy = ProductionActivationPolicy(enabled)
    assert policy.decide(
        ActivationCapability.SUBSCRIPTION_ACTIVATION,
        organization_id=internal,
    ).allowed is True
    outsider_decision = policy.decide(
        ActivationCapability.SUBSCRIPTION_ACTIVATION,
        organization_id=outsider,
    )
    assert outsider_decision.allowed is False
    assert outsider_decision.code == "activation.organization.not_in_rollout"


def test_zero_money_canary_checklist_preserves_stage0() -> None:
    contract=json.loads(_text(CONTRACT))
    assert contract["phase"] == "PAY-24-D"
    assert contract["deployment"]["replicas"] == 0
    assert contract["deployment"]["beat_entitlement_schedule_present"] is False
    assert contract["canary"] == {
        "disposable_pg_only": True,
        "provider_io": False,
        "real_provider_credentials": False,
        "real_money_movement": 0,
    }
    assert contract["gates"]["stage1_live_activation"] == "NOT_AUTHORIZED"
    assert contract["gates"]["real_provider_calls"] == 0
    assert contract["gates"]["real_money_movement"] == 0
    design=_text(DESIGN)
    assert "PAY24_STAGE1_LIVE_ACTIVATION=NOT_AUTHORIZED" in design
    assert "PAY24_REAL_PROVIDER_CALLS=0" in design
    assert "PAY24_REAL_MONEY_MOVEMENT=0" in design
