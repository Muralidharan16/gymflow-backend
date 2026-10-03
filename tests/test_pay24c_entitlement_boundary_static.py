from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/zza7d8e9f0a70_pay24c_entitlement_authority.py"


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_pay24c_migration_is_successor_only_and_stage0_safe() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'revision = "zza7d8e9f0a70"' in source
    assert 'down_revision = "zz97d8e9f0a69"' in source
    assert "CREATE ROLE" not in source
    assert "ALTER ROLE entitlement_runtime" not in source
    assert "PAY24-C downgrade blocked: entitlement command history exists" in source


def test_entitlement_runtime_is_peer_isolated_and_unbound_from_existing_profiles() -> None:
    roles = json.loads(_source("security/cluster_role_bootstrap/roles.v1.json"))
    identity = json.loads(_source("security/cluster_role_bootstrap/identity_transitions.v1.json"))
    bindings = json.loads(_source("security/runtime_identity/runtime_bindings.v1.json"))
    profiles = json.loads(_source("security/runtime_identity/process_profiles.v1.json"))
    role = roles["managed_roles"]["entitlement_runtime"]
    assert role["attributes"] == {
        "superuser": False,
        "inherit": False,
        "create_role": False,
        "create_db": False,
        "can_login": False,
        "replication": False,
        "bypass_rls": False,
    }
    assert "entitlement_runtime" in identity["peer_isolation_principals"]
    assert bindings["bindings"]["entitlement"]["direct_capabilities"] == [
        "entitlement_runtime"
    ]
    assert profiles["profiles"]["entitlement_worker"]["runtime_components"] == [
        "entitlement"
    ]
    for name, profile in profiles["profiles"].items():
        if name != "entitlement_worker":
            assert "ENTITLEMENT_DATABASE_URL" in profile["forbidden_database_variables"]


def test_pay5_successor_enqueues_and_does_not_directly_apply_entitlement() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    successor = source.split(
        "CREATE OR REPLACE FUNCTION app_secure.consume_member_subscription_finance_event",
        1,
    )[1].split("$function$", 2)[1]
    assert "pay24c_enqueue_paid_activation" in successor
    assert "apply_member_subscription_finance_event" not in successor
    assert "'entitlement_pending'" in successor
    assert "effect_applied" in successor


def test_only_entitlement_runtime_receives_apply_command_execute() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert "app_secure.pay24c_apply_entitlement_command(uuid,uuid,bigint)" in source
    assert "TO entitlement_runtime" in source
    assert "GRANT USAGE ON SCHEMA app_secure TO entitlement_runtime" in source
    assert "PAY24-C widened migration_owner into app_secure" in source
    assert "PAY24-C entitlement runtime lacks app_secure USAGE" in source
    assert "PAY24-C protected term mutation requires entitlement_runtime" in source
    assert "PAY24-C protected V2 mutation requires entitlement_runtime" in source
    assert "PAY24-C freeze mutation requires entitlement_runtime" in source
    assert "GRANT TRIGGER ON TABLE" in source
    assert "TO app_security_owner" in source
    assert "REVOKE TRIGGER ON TABLE" in source
    assert "FROM app_security_owner" in source
    assert "PAY24-C temporary app_security_owner TRIGGER grant leaked" in source


def test_refund_is_durable_command_not_direct_subscription_write() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    body = source.split(
        "CREATE FUNCTION app_secure.pay24c_consume_refund_event", 1
    )[1].split("$function$", 2)[1]
    assert "member_entitlement_commands" in body
    assert "'recompute_refund'" in body
    assert "UPDATE public.subscription_terms" not in body


def test_stage0_production_overlay_has_no_entitlement_credential_or_service() -> None:
    overlay = _source("deploy/docker-compose.production-identities.yml")
    assert "celery-entitlement-worker:" not in overlay
    assert overlay.count('ENTITLEMENT_DATABASE_URL: ""') >= 5


def test_access_and_legacy_retirement_are_part_of_pay24c_surface() -> None:
    migration = MIGRATION.read_text(encoding="utf-8")
    assert "CREATE FUNCTION app_secure.member_entitlement_access_active" in migration
    assert '("app_user", "public.member_subscriptions", "UPDATE")' in migration
    assert '("app_runtime", "public.member_subscriptions", "UPDATE")' in migration
    assert '("app_user", "public.member_subscriptions_v2", "UPDATE")' in migration


def test_no_pay24c_runtime_function_contains_provider_io() -> None:
    combined = "\n".join(
        (
            MIGRATION.read_text(encoding="utf-8"),
            _source("app/tasks/entitlement_dispatcher.py"),
            _source("app/tasks/refund_entitlement_dispatcher.py"),
        )
    ).lower()
    for forbidden in (
        "razorpay",
        "api.razorpay.com",
        "requests.post(",
        "httpx.post(",
        "aiohttp",
        "rzp_live_",
    ):
        assert forbidden not in combined


def test_ambiguous_cross_invoice_refund_fails_to_review_instead_of_guessing() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert "ambiguous_refund_allocation" in source
    assert "status='review_required'" in source
    assert "restore refund allocation is ambiguous" in source
    assert "other_allocation.invoice_id<>v_invoice.id" in source


def test_non_subscription_refund_can_ack_without_fabricating_entitlement_command() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    ack = source.split(
        "CREATE FUNCTION app_secure.pay24c_ack_refund_event", 1
    )[1].split("$function$", 2)[1]
    assert "OR NOT EXISTS(" in ack
    assert "member_subscription_finance_bindings" in ack
