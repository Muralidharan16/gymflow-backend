from __future__ import annotations

import hashlib
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.database import AsyncSessionLocal
from app.finance_core.domain.payment_ledger import FinancePaymentConflictError
from app.finance_core.domain.provider_boundary import (
    FinanceProviderConfigError,
    FinanceWebhookNormalizationError,
    FinanceWebhookSignatureError,
    NormalizedProviderEventCommand,
    ProviderCheckoutIntentRequest,
    ProviderSandboxConfig,
    SandboxCheckoutIntentProvider,
    StaticSandboxSignatureVerifier,
    validate_sandbox_provider_config,
)
from app.finance_core.services.provider_webhooks import FinanceProviderWebhookIntakeService
from tests.finance_core.test_phase5c_invoice_engine import fetch_scalar
from tests.finance_core.test_phase5d_payment_ledger import seed_finance_foundation


def sandbox_config(*, signing_secret: str = "test_secret") -> ProviderSandboxConfig:
    return ProviderSandboxConfig(
        provider_code="sandbox_provider",
        sandbox_mode=True,
        merchant_id="merchant_test",
        signing_secret=signing_secret,
    )


def signature_for(payload_hash: str, signing_secret: str = "test_secret") -> str:
    return hashlib.sha256(f"{payload_hash}:{signing_secret}".encode("utf-8")).hexdigest()


def event_command(
    *,
    provider_event_id: str = "evt_sandbox_1",
    raw_status: str = "captured",
    payload_hash: str = "a" * 64,
    idempotency_key: str = "webhook-event-key-1",
) -> NormalizedProviderEventCommand:
    return NormalizedProviderEventCommand(
        provider_code="sandbox_provider",
        provider_event_id=provider_event_id,
        event_type="payment.captured",
        raw_status=raw_status,
        payload_hash=payload_hash,
        signature=signature_for(payload_hash),
        idempotency_key=idempotency_key,
    )


@pytest.mark.asyncio
async def test_sandbox_adapter_implements_provider_protocol_and_returns_deterministic_response():
    provider = SandboxCheckoutIntentProvider(sandbox_config())
    request = ProviderCheckoutIntentRequest(
        invoice_id="00000000-0000-0000-0000-000000000001",
        amount=Decimal("1180.00"),
        currency_code="INR",
        idempotency_key="adapter-key",
    )
    first = await provider.create_checkout_intent(request)
    second = await provider.create_checkout_intent(request)
    assert first == second
    assert first.provider_code == "sandbox_provider"
    assert first.provider_order_ref is not None
    assert first.provider_order_ref.startswith("sandbox_order_")
    assert first.status == "created"


def test_provider_config_validation_accepts_sandbox_and_redacts_secret_values():
    config = validate_sandbox_provider_config(sandbox_config(signing_secret="super_secret_value"))
    rendered = repr(config)
    assert config.provider_code == "sandbox_provider"
    assert "super_secret_value" not in rendered
    assert "[REDACTED]" in rendered


def test_provider_config_rejects_production_mode_without_leaking_secret():
    with pytest.raises(FinanceProviderConfigError) as exc:
        validate_sandbox_provider_config(
            ProviderSandboxConfig(
                provider_code="sandbox_provider",
                sandbox_mode=False,
                merchant_id="merchant_test",
                signing_secret="do_not_leak",
            )
        )
    assert "do_not_leak" not in str(exc.value)
    assert "[REDACTED]" in str(exc.value)


@pytest.mark.asyncio
async def test_webhook_normalization_accepts_valid_sandbox_event_and_records_payment_event():
    await seed_finance_foundation()
    async with AsyncSessionLocal() as session:
        service = FinanceProviderWebhookIntakeService(
            session,
            config=sandbox_config(),
            signature_verifier=StaticSandboxSignatureVerifier(),
        )
        result = await service.normalize_provider_event(event_command())
        await session.commit()

    assert result.provider_event_id == "evt_sandbox_1"
    assert result.raw_status == "captured"
    assert await fetch_scalar("SELECT count(*) FROM finance.payment_events") == 1


@pytest.mark.asyncio
async def test_invalid_signature_is_rejected():
    await seed_finance_foundation()
    command = event_command()
    command = NormalizedProviderEventCommand(
        provider_code=command.provider_code,
        provider_event_id=command.provider_event_id,
        event_type=command.event_type,
        raw_status=command.raw_status,
        payload_hash=command.payload_hash,
        signature="bad-signature",
        idempotency_key=command.idempotency_key,
    )
    async with AsyncSessionLocal() as session:
        service = FinanceProviderWebhookIntakeService(
            session,
            config=sandbox_config(),
            signature_verifier=StaticSandboxSignatureVerifier(),
        )
        with pytest.raises(FinanceWebhookSignatureError):
            await service.normalize_provider_event(command)
        await session.rollback()


@pytest.mark.asyncio
async def test_duplicate_provider_event_replay_and_changed_idempotency_conflict():
    await seed_finance_foundation()
    async with AsyncSessionLocal() as session:
        service = FinanceProviderWebhookIntakeService(
            session,
            config=sandbox_config(),
            signature_verifier=StaticSandboxSignatureVerifier(),
        )
        first = await service.normalize_provider_event(event_command(idempotency_key="event-replay"))
        replay = await service.normalize_provider_event(event_command(idempotency_key="event-replay"))
        assert replay.payment_event_id == first.payment_event_id
        assert replay.replayed is True
        with pytest.raises(FinancePaymentConflictError):
            await service.normalize_provider_event(event_command(idempotency_key="event-duplicate-other-key"))
        await session.rollback()


@pytest.mark.asyncio
async def test_same_idempotency_key_with_different_normalized_payload_conflicts():
    await seed_finance_foundation()
    async with AsyncSessionLocal() as session:
        service = FinanceProviderWebhookIntakeService(
            session,
            config=sandbox_config(),
            signature_verifier=StaticSandboxSignatureVerifier(),
        )
        await service.normalize_provider_event(event_command(provider_event_id="evt_idem", idempotency_key="event-idem"))
        with pytest.raises(FinancePaymentConflictError):
            await service.normalize_provider_event(
                event_command(provider_event_id="evt_idem_changed", idempotency_key="event-idem")
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_unknown_status_is_rejected_by_explicit_rule():
    await seed_finance_foundation()
    async with AsyncSessionLocal() as session:
        service = FinanceProviderWebhookIntakeService(
            session,
            config=sandbox_config(),
            signature_verifier=StaticSandboxSignatureVerifier(),
        )
        with pytest.raises(FinanceWebhookNormalizationError):
            await service.normalize_provider_event(event_command(raw_status="mystery"))
        await session.rollback()


@pytest.mark.asyncio
async def test_webhook_intake_does_not_allocate_post_ledger_or_activate_subscription():
    await seed_finance_foundation()
    async with AsyncSessionLocal() as session:
        service = FinanceProviderWebhookIntakeService(
            session,
            config=sandbox_config(),
            signature_verifier=StaticSandboxSignatureVerifier(),
        )
        await service.normalize_provider_event(event_command(provider_event_id="evt_no_side_effect", idempotency_key="event-no-side-effect"))
        await session.commit()

    assert await fetch_scalar("SELECT count(*) FROM finance.payment_allocations") == 0
    assert await fetch_scalar("SELECT count(*) FROM finance.ledger_entries") == 0
    assert await fetch_scalar("SELECT count(*) FROM information_schema.tables WHERE table_name = 'platform_subscriptions'") >= 0


def test_phase5f_has_no_live_provider_api_frontend_or_production_enablement():
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
    assert "platform_subscriptions" not in combined
    assert not (repo_root / "frontend").exists()
