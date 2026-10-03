#!/usr/bin/env python3
"""PAY-24-F terminal-controlled Stage-1/live-order canary.

This tool never captures a payment and never processes a webhook. The only
provider mutation it can perform is Razorpay order creation after a committed
PAY-24-B checkout admission.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.finance_core.domain.provider_boundary import FinanceProviderOperationError
from app.finance_core.domain.razorpay_live import RazorpayLiveConfig
from app.finance_core.services.member_subscription_checkout import (
    MemberSubscriptionCheckoutPreparation,
    SourceBoundMemberSubscriptionCheckoutService,
)
from app.finance_core.services.razorpay_live import (
    RazorpayLiveCheckoutAdapter,
    RazorpayLiveHTTPTransport,
    RazorpayLiveOrdersClient,
)
from app.payment_activation import ActivationCapability, DurableActivationAuthority
from app.payment_activation.domain import ActivationStage, KillSwitches
from app.payment_activation.release_identity import (
    GitWorktreeReleaseIdentityProvider,
    RootOwnedReleaseAttestationProvider,
)


_SCHEMA = 1
_PROVIDER = "razorpay"
_ENVIRONMENT = "live"
_LEASE_SECONDS = 300


def _required(name: str) -> str:
    value = str(os.environ.get(name, "") or "").strip()
    if not value:
        raise SystemExit(f"missing required environment variable: {name}")
    return value


def _async_url(raw: str, name: str) -> str:
    value = raw.strip()
    if value.startswith("postgresql+asyncpg://"):
        return value
    if value.startswith("postgresql://"):
        return value.replace("postgresql://", "postgresql+asyncpg://", 1)
    raise SystemExit(f"{name} must be a PostgreSQL asyncpg URL")


def _engine(name: str):
    return create_async_engine(
        _async_url(_required(name), name),
        poolclass=NullPool,
        pool_pre_ping=True,
    )


def _validate_order_database_pair() -> tuple[str, str]:
    app_raw = _required("PAY24F_APP_DATABASE_URL")
    payment_raw = _required("PAY24F_PAYMENT_DATABASE_URL")
    app = make_url(app_raw)
    payment = make_url(payment_raw)
    if (
        app.host != payment.host
        or app.port != payment.port
        or app.database != payment.database
    ):
        raise SystemExit("PAY-24-F app/payment database targets differ")
    if not app.username or not payment.username or app.username == payment.username:
        raise SystemExit("PAY-24-F app/payment database identities are not isolated")
    return app_raw, payment_raw


def _release_provider(args):
    if args.release_attestation:
        return RootOwnedReleaseAttestationProvider(
            Path(args.release_attestation)
        )
    if args.release_git_root:
        return GitWorktreeReleaseIdentityProvider(Path(args.release_git_root))
    raise SystemExit(
        "one trusted release source is required: "
        "--release-attestation or --release-git-root"
    )


def _measure_exact_release(args, certified_sha: str) -> str:
    measured = _release_provider(args).measure()
    if measured.deployed_sha != certified_sha:
        raise SystemExit(
            "trusted deployed SHA does not equal the certified PAY-24-F SHA"
        )
    return measured.deployed_sha


def _interactive_exact(prompt: str, expected: str) -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SystemExit("PAY-24-F mutation commands require an interactive terminal")
    typed = input(prompt)
    if typed != expected:
        raise SystemExit("confirmation phrase mismatch; no mutation performed")


async def _set_org(session: AsyncSession, organization_id: uuid.UUID) -> None:
    await session.execute(
        text("SELECT pg_catalog.set_config('app.current_org_id',:org,true)"),
        {"org": str(organization_id)},
    )


def _safe_snapshot(snapshot) -> dict[str, object]:
    return {
        "stage": int(snapshot.stage),
        "generation": snapshot.generation,
        "provider_egress": snapshot.provider_egress,
        "enabled_capabilities": [
            item.value for item in snapshot.enabled_capabilities
        ],
        "internal_organization_id": (
            str(snapshot.internal_organization_id)
            if snapshot.internal_organization_id
            else None
        ),
        "authorization_id": snapshot.authorization_id,
        "authorized_stage": (
            int(snapshot.authorized_stage)
            if snapshot.authorized_stage is not None
            else None
        ),
        "certified_sha": snapshot.certified_sha,
        "deployed_sha": snapshot.deployed_sha,
        "posture_digest": snapshot.posture_digest,
    }


async def cmd_snapshot(args) -> int:
    engine = _engine("FINANCE_CONFIG_DATABASE_URL")
    try:
        maker = async_sessionmaker(
            engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        async with maker() as session:
            snapshot = await DurableActivationAuthority(session).snapshot()
            await session.rollback()
        print(json.dumps({"pay24f_snapshot": _safe_snapshot(snapshot)}, sort_keys=True))
        return 0
    finally:
        await engine.dispose()


async def cmd_activate_stage1(args) -> int:
    certified_sha = args.certified_sha
    if len(certified_sha) != 40 or any(
        ch not in "0123456789abcdef" for ch in certified_sha
    ):
        raise SystemExit("--certified-sha must be a lowercase 40-character Git SHA")
    organization_id = uuid.UUID(args.internal_org)
    measured_sha = _measure_exact_release(args, certified_sha)

    phrase = f"AUTHORIZE PAY24 STAGE1 {certified_sha} {organization_id}"
    _interactive_exact(
        "Type the exact Stage-1 authorization phrase:\n"
        f"{phrase}\n> ",
        phrase,
    )

    engine = _engine("FINANCE_CONFIG_DATABASE_URL")
    maker = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    try:
        async with maker() as session:
            authority = DurableActivationAuthority(
                session,
                release_identity_provider=_release_provider(args),
            )
            before = await authority.snapshot()
            if not (
                before.stage is ActivationStage.LIVE_CONFIG_EGRESS_BLOCKED
                and before.provider_egress == "blocked"
                and not before.enabled_capabilities
                and before.internal_organization_id is None
            ):
                raise SystemExit(
                    "Stage-1 activation requires exact PAY-24-A Stage-0 posture"
                )

            await authority.bind_release_identity(
                operation_id=uuid.uuid4(),
                expected_generation=before.generation,
                certified_sha=certified_sha,
                actor=args.actor,
            )
            await session.commit()

            after_release = await authority.snapshot()
            await authority.bind_human_authorization(
                operation_id=uuid.uuid4(),
                expected_generation=after_release.generation,
                authorization_id=args.authorization_id,
                authorized_sha=certified_sha,
                authorized_stage=ActivationStage.INTERNAL_ORGANIZATION,
                authorized_by=args.authorized_by,
                authorized_at=datetime.now(UTC),
                actor=args.actor,
            )
            await session.commit()

            after_auth = await authority.snapshot()
            await authority.transition_activation(
                operation_id=uuid.uuid4(),
                expected_generation=after_auth.generation,
                target_stage=ActivationStage.INTERNAL_ORGANIZATION,
                provider_egress="open",
                internal_organization_id=organization_id,
                kill_switches=KillSwitches(
                    checkout=True,
                    webhooks=True,
                    payment_application=True,
                    subscription_activation=True,
                ),
                actor=args.actor,
            )
            await session.commit()

            final = await authority.snapshot()
            expected = [
                ActivationCapability.CHECKOUT,
                ActivationCapability.WEBHOOKS,
                ActivationCapability.PAYMENT_APPLICATION,
                ActivationCapability.SUBSCRIPTION_ACTIVATION,
            ]
            if not (
                final.stage is ActivationStage.INTERNAL_ORGANIZATION
                and final.provider_egress == "open"
                and list(final.enabled_capabilities) == expected
                and final.internal_organization_id == organization_id
                and final.certified_sha == certified_sha
                and final.deployed_sha == measured_sha
                and final.authorized_stage
                is ActivationStage.INTERNAL_ORGANIZATION
            ):
                raise SystemExit(
                    "PAY-24-F Stage-1 postcondition verification failed"
                )
            print(
                json.dumps(
                    {"pay24f_stage1": "PASS", **_safe_snapshot(final)},
                    sort_keys=True,
                )
            )
            return 0
    finally:
        await engine.dispose()


def _evidence_payload(
    *,
    certified_sha: str,
    organization_id: uuid.UUID,
    subscription_id: uuid.UUID,
    prepared: MemberSubscriptionCheckoutPreparation,
) -> dict[str, object]:
    if prepared.provider_operation is None or prepared.provider_request_sha256 is None:
        raise SystemExit("live order preparation has no provider operation")
    return {
        "schema_version": _SCHEMA,
        "phase": "PAY-24-F",
        "kind": "live_order_preparation",
        "certified_sha": certified_sha,
        "organization_id": str(organization_id),
        "subscription_id": str(subscription_id),
        "finance_invoice_id": str(prepared.finance_invoice_id),
        "finance_checkout_intent_id": str(prepared.finance_checkout_intent_id),
        "amount": str(prepared.amount),
        "currency_code": prepared.currency_code,
        "provider_code": _PROVIDER,
        "provider_environment": _ENVIRONMENT,
        "provider_operation_id": str(prepared.provider_operation.operation_id),
        "provider_operation_status": prepared.provider_operation.status,
        "provider_request_sha256": prepared.provider_request_sha256,
        "prepared_at": datetime.now(UTC).isoformat(),
        "real_money_movement": False,
    }


def _write_evidence(path: Path, payload: dict[str, object]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
    except Exception:
        try:
            path.unlink(missing_ok=True)
        finally:
            raise


def _load_evidence(path: str) -> dict[str, object]:
    source = Path(path).expanduser().resolve()
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit("PAY-24-F preparation evidence is unreadable") from exc
    required = {
        "schema_version",
        "phase",
        "kind",
        "certified_sha",
        "organization_id",
        "subscription_id",
        "finance_invoice_id",
        "finance_checkout_intent_id",
        "amount",
        "currency_code",
        "provider_code",
        "provider_environment",
        "provider_operation_id",
        "provider_operation_status",
        "provider_request_sha256",
        "prepared_at",
        "real_money_movement",
    }
    if set(payload) != required:
        raise SystemExit("PAY-24-F preparation evidence fields drifted")
    if (
        payload["schema_version"] != _SCHEMA
        or payload["phase"] != "PAY-24-F"
        or payload["kind"] != "live_order_preparation"
        or payload["provider_code"] != _PROVIDER
        or payload["provider_environment"] != _ENVIRONMENT
        or payload["real_money_movement"] is not False
    ):
        raise SystemExit("PAY-24-F preparation evidence is invalid")
    return payload


async def _prepare(
    *,
    app_url: str,
    organization_id: uuid.UUID,
    subscription_id: uuid.UUID,
) -> MemberSubscriptionCheckoutPreparation:
    engine = create_async_engine(
        _async_url(app_url, "PAY24F_APP_DATABASE_URL"),
        poolclass=NullPool,
        pool_pre_ping=True,
    )
    maker = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    try:
        async with maker() as session:
            await _set_org(session, organization_id)
            service = SourceBoundMemberSubscriptionCheckoutService(session)
            prepared = await service.prepare_local_checkout(
                organization_id=organization_id,
                subscription_id=subscription_id,
                provider_code=_PROVIDER,
                provider_environment=_ENVIRONMENT,
            )
            await session.commit()
            return prepared
    finally:
        await engine.dispose()


async def cmd_prepare_live_order(args) -> int:
    app_raw, _ = _validate_order_database_pair()
    certified_sha = args.certified_sha
    _measure_exact_release(args, certified_sha)
    organization_id = uuid.UUID(args.internal_org)
    subscription_id = uuid.UUID(args.subscription_id)
    prepared = await _prepare(
        app_url=app_raw,
        organization_id=organization_id,
        subscription_id=subscription_id,
    )
    if prepared.provider_order_ref:
        raise SystemExit(
            "subscription already has a provider order; refusing new preparation"
        )
    if prepared.provider_operation is None:
        raise SystemExit("live order provider operation is unavailable")
    if prepared.provider_operation.status not in {
        "reserved",
        "failed_retryable",
    }:
        raise SystemExit(
            "live order operation is not executable: "
            f"{prepared.provider_operation.status}"
        )
    payload = _evidence_payload(
        certified_sha=certified_sha,
        organization_id=organization_id,
        subscription_id=subscription_id,
        prepared=prepared,
    )
    _write_evidence(Path(args.evidence), payload)
    print(json.dumps(payload, sort_keys=True))
    return 0


def _assert_preparation_matches(
    evidence: dict[str, object],
    prepared: MemberSubscriptionCheckoutPreparation,
) -> None:
    operation = prepared.provider_operation
    if operation is None or prepared.provider_request_sha256 is None:
        raise SystemExit("prepared provider operation disappeared")
    actual = {
        "finance_invoice_id": str(prepared.finance_invoice_id),
        "finance_checkout_intent_id": str(prepared.finance_checkout_intent_id),
        "amount": str(prepared.amount),
        "currency_code": prepared.currency_code,
        "provider_operation_id": str(operation.operation_id),
        "provider_request_sha256": prepared.provider_request_sha256,
    }
    for field, value in actual.items():
        if evidence[field] != value:
            raise SystemExit(
                f"PAY-24-F prepared evidence mismatch: {field}"
            )


async def _attach_provider_order(
    *,
    app_url: str,
    organization_id: uuid.UUID,
    subscription_id: uuid.UUID,
    checkout_intent_id: uuid.UUID,
    provider_order_ref: str,
) -> None:
    engine = create_async_engine(
        _async_url(app_url, "PAY24F_APP_DATABASE_URL"),
        poolclass=NullPool,
        pool_pre_ping=True,
    )
    maker = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    try:
        async with maker() as session:
            await _set_org(session, organization_id)
            await SourceBoundMemberSubscriptionCheckoutService(
                session
            ).attach_provider_order(
                subscription_id=subscription_id,
                checkout_intent_id=checkout_intent_id,
                provider_order_ref=provider_order_ref,
            )
            await session.commit()
    finally:
        await engine.dispose()


async def cmd_execute_live_order(args) -> int:
    evidence = _load_evidence(args.evidence)
    certified_sha = str(evidence["certified_sha"])
    _measure_exact_release(args, certified_sha)

    app_raw, payment_raw = _validate_order_database_pair()
    organization_id = uuid.UUID(str(evidence["organization_id"]))
    subscription_id = uuid.UUID(str(evidence["subscription_id"]))
    checkout_intent_id = uuid.UUID(
        str(evidence["finance_checkout_intent_id"])
    )

    prepared = await _prepare(
        app_url=app_raw,
        organization_id=organization_id,
        subscription_id=subscription_id,
    )
    _assert_preparation_matches(evidence, prepared)
    if prepared.provider_order_ref:
        print(
            json.dumps(
                {
                    "pay24f_live_order": "ALREADY_ATTACHED",
                    "provider_order_ref": prepared.provider_order_ref,
                    "finance_checkout_intent_id": str(
                        prepared.finance_checkout_intent_id
                    ),
                    "real_money_movement": False,
                },
                sort_keys=True,
            )
        )
        return 0

    phrase = f"CREATE LIVE RAZORPAY ORDER {checkout_intent_id}"
    _interactive_exact(
        "This creates a LIVE Razorpay order but does not capture money.\n"
        f"Type exactly:\n{phrase}\n> ",
        phrase,
    )

    config = RazorpayLiveConfig(
        mode="live",
        key_id=_required("RAZORPAY_LIVE_KEY_ID"),
        key_secret=_required("RAZORPAY_LIVE_KEY_SECRET"),
        merchant_reference=_required("RAZORPAY_LIVE_MERCHANT_REFERENCE"),
    )
    adapter = RazorpayLiveCheckoutAdapter(
        config=config,
        client=RazorpayLiveOrdersClient(
            config=config,
            transport=RazorpayLiveHTTPTransport(),
        ),
    )

    payment_engine = create_async_engine(
        _async_url(payment_raw, "PAY24F_PAYMENT_DATABASE_URL"),
        poolclass=NullPool,
        pool_pre_ping=True,
    )
    maker = async_sessionmaker(
        payment_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    lease_owner = uuid.uuid4()
    provider_order_ref: str | None = None

    try:
        async with maker() as payment:
            effects = SourceBoundMemberSubscriptionCheckoutService(
                payment
            ).bind_provider_effects(payment)
            authority = DurableActivationAuthority(payment)

            await _set_org(payment, organization_id)
            claim = await effects.claim_provider_operation(
                prepared=prepared,
                lease_owner=lease_owner,
            )
            if not claim.claimed:
                await payment.rollback()
                if claim.status == "succeeded" and claim.provider_object_id:
                    provider_order_ref = claim.provider_object_id
                else:
                    raise SystemExit(
                        "live order operation is unresolved: "
                        f"{claim.status}"
                    )
            else:
                binding = SourceBoundMemberSubscriptionCheckoutService(
                    payment
                ).provider_admission_binding(
                    prepared=prepared,
                    claim=claim,
                )
                admission = await authority.request_current_provider_admission(
                    capability=ActivationCapability.CHECKOUT,
                    logical_operation_id=binding.logical_operation_id,
                    operation_sha=binding.operation_sha,
                    lease_seconds=_LEASE_SECONDS,
                )
                await payment.commit()

                await _set_org(payment, organization_id)
                started = await authority.start_provider_admission(
                    admission_id=admission.admission_id,
                    execution_id=lease_owner,
                )
                await payment.commit()
                if started.state != "active":
                    raise SystemExit(
                        "PAY-24 provider admission did not become active"
                    )

                try:
                    response = await adapter.create_checkout_intent(
                        prepared.provider_request
                    )
                except FinanceProviderOperationError as exc:
                    await _set_org(payment, organization_id)
                    await effects.finish_provider_error(
                        claim=claim,
                        lease_owner=lease_owner,
                        error=exc,
                    )
                    await authority.finish_provider_admission(
                        admission_id=admission.admission_id,
                        execution_id=lease_owner,
                        outcome=(
                            "unknown"
                            if exc.requires_reconciliation
                            else "completed"
                        ),
                    )
                    await payment.commit()
                    if exc.requires_reconciliation:
                        raise SystemExit(
                            "LIVE PROVIDER OUTCOME UNKNOWN: do not retry; "
                            "reconcile the same operation"
                        ) from exc
                    raise SystemExit(
                        f"live provider order failed safely: {exc.code}"
                    ) from exc

                await _set_org(payment, organization_id)
                provider_order_ref = (
                    await effects.finish_provider_success(
                        provider_adapter=adapter,
                        claim=claim,
                        lease_owner=lease_owner,
                        response=response,
                    )
                )
                await authority.finish_provider_admission(
                    admission_id=admission.admission_id,
                    execution_id=lease_owner,
                    outcome="completed",
                )
                await payment.commit()
    finally:
        await payment_engine.dispose()

    if provider_order_ref is None:
        raise SystemExit("live provider order result is unavailable")

    await _attach_provider_order(
        app_url=app_raw,
        organization_id=organization_id,
        subscription_id=subscription_id,
        checkout_intent_id=checkout_intent_id,
        provider_order_ref=provider_order_ref,
    )

    print(
        json.dumps(
            {
                "pay24f_live_order": "PASS",
                "organization_id": str(organization_id),
                "subscription_id": str(subscription_id),
                "finance_checkout_intent_id": str(checkout_intent_id),
                "provider_order_ref": provider_order_ref,
                "amount": evidence["amount"],
                "currency_code": evidence["currency_code"],
                "real_money_movement": False,
                "payment_capture_enabled": False,
                "webhook_live_path_enabled": False,
            },
            sort_keys=True,
        )
    )
    return 0


async def cmd_rollback(args) -> int:
    phrase = "BEGIN PAY24 EMERGENCY ROLLBACK"
    _interactive_exact(
        "Type exactly to close provider admission immediately:\n"
        f"{phrase}\n> ",
        phrase,
    )
    engine = _engine("FINANCE_CONFIG_DATABASE_URL")
    maker = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    try:
        async with maker() as session:
            authority = DurableActivationAuthority(session)
            current = await authority.snapshot()
            if current.stage is ActivationStage.LIVE_CONFIG_EGRESS_BLOCKED:
                print(
                    json.dumps(
                        {
                            "pay24f_rollback": "ALREADY_STAGE0",
                            **_safe_snapshot(current),
                        },
                        sort_keys=True,
                    )
                )
                return 0

            started = await authority.begin_emergency_rollback(
                operation_id=uuid.uuid4(),
                expected_generation=current.generation,
                actor=args.actor,
            )
            await session.commit()

            await authority.expire_provider_admissions(
                operation_id=uuid.uuid4(),
                actor=args.actor,
            )
            await session.commit()

            drains = await authority.admission_drain_snapshot()
            blocking = [
                item
                for item in drains
                if item.state in {"admitted", "active"}
                and item.lease_count > 0
            ]
            if blocking:
                print(
                    json.dumps(
                        {
                            "pay24f_rollback": "CLOSING",
                            "generation": started.generation,
                            "blocking_admissions": [
                                {
                                    "generation": item.activation_generation,
                                    "state": item.state,
                                    "count": item.lease_count,
                                }
                                for item in blocking
                            ],
                        },
                        sort_keys=True,
                    )
                )
                return 2

            closing = await authority.snapshot()
            final = await authority.finalize_emergency_rollback(
                operation_id=uuid.uuid4(),
                expected_generation=closing.generation,
                actor=args.actor,
            )
            await session.commit()
            snapshot = await authority.snapshot()
            if not (
                final.stage is ActivationStage.LIVE_CONFIG_EGRESS_BLOCKED
                and final.provider_egress == "blocked"
                and snapshot.stage
                is ActivationStage.LIVE_CONFIG_EGRESS_BLOCKED
                and not snapshot.enabled_capabilities
                and snapshot.internal_organization_id is None
            ):
                raise SystemExit(
                    "PAY-24 emergency rollback final posture invalid"
                )
            print(
                json.dumps(
                    {
                        "pay24f_rollback": "PASS",
                        **_safe_snapshot(snapshot),
                    },
                    sort_keys=True,
                )
            )
            return 0
    finally:
        await engine.dispose()


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    snap = sub.add_parser("snapshot")
    snap.set_defaults(handler=cmd_snapshot)

    activate = sub.add_parser("activate-stage1")
    activate.add_argument("--certified-sha", required=True)
    activate.add_argument("--internal-org", required=True)
    activate.add_argument("--authorization-id", required=True)
    activate.add_argument("--authorized-by", required=True)
    activate.add_argument("--actor", required=True)
    activate.add_argument("--release-attestation")
    activate.add_argument("--release-git-root")
    activate.set_defaults(handler=cmd_activate_stage1)

    prepare = sub.add_parser("prepare-live-order")
    prepare.add_argument("--certified-sha", required=True)
    prepare.add_argument("--internal-org", required=True)
    prepare.add_argument("--subscription-id", required=True)
    prepare.add_argument("--evidence", required=True)
    prepare.add_argument("--release-attestation")
    prepare.add_argument("--release-git-root")
    prepare.set_defaults(handler=cmd_prepare_live_order)

    execute = sub.add_parser("execute-live-order")
    execute.add_argument("--evidence", required=True)
    execute.add_argument("--release-attestation")
    execute.add_argument("--release-git-root")
    execute.set_defaults(handler=cmd_execute_live_order)

    rollback = sub.add_parser("rollback-stage0")
    rollback.add_argument("--actor", required=True)
    rollback.set_defaults(handler=cmd_rollback)
    return p


async def async_main() -> int:
    args = parser().parse_args()
    return await args.handler(args)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main()))
