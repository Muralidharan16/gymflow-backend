#!/usr/bin/env python3
"""PAY-24-G terminal-controlled real-money internal canary.

The command opens no public route. It binds a PAY-24 provider admission, serves
one Razorpay Checkout page on 127.0.0.1, verifies the successful callback with
the live key secret, fetches authoritative payment state from Razorpay, records
captured evidence through finance_reconciliation_runtime, then applies the
captured payment through the existing Finance application gate.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.finance_core.domain.payment_application_gate import ApplyConfirmedPaymentCommand
from app.finance_core.domain.razorpay_live import RazorpayLiveConfig
from app.finance_core.services.member_subscription_checkout import (
    MemberSubscriptionCheckoutPreparation,
    SourceBoundMemberSubscriptionCheckoutService,
)
from app.finance_core.services.pay24g_live_payment_evidence import (
    ConfirmTerminalLivePaymentCommand,
    FinanceTerminalLivePaymentEvidenceService,
)
from app.finance_core.services.payment_application_gate import FinancePaymentApplicationGateService
from app.finance_core.services.razorpay_live import (
    RazorpayLiveHTTPTransport,
    RazorpayLivePaymentsClient,
)
from app.payment_activation import ActivationCapability, DurableActivationAuthority
from app.payment_activation.domain import ActivationStage
from app.payment_activation.release_identity import (
    GitWorktreeReleaseIdentityProvider,
    RootOwnedReleaseAttestationProvider,
)


_MAX_CANARY_AMOUNT = Decimal("10.00")
_CANARY_CURRENCY = "INR"
_ADMISSION_LEASE_SECONDS = 900
_CALLBACK_TIMEOUT_SECONDS = 600
_EXPECTED_CAPABILITIES = (
    "checkout",
    "webhooks",
    "payment_application",
    "subscription_activation",
)
_PEER_ROLES = (
    "app_runtime",
    "auth_runtime",
    "worker_runtime",
    "lifecycle_maintenance_runtime",
    "finance_payment_runtime",
    "finance_refund_runtime",
    "entitlement_runtime",
)


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
    raise SystemExit(f"{name} must be a PostgreSQL URL")


def _engine_from_env(name: str):
    return create_async_engine(
        _async_url(_required(name), name),
        poolclass=NullPool,
        pool_pre_ping=True,
    )


def _release_provider(args):
    if args.release_attestation:
        return RootOwnedReleaseAttestationProvider(Path(args.release_attestation))
    if args.release_git_root:
        return GitWorktreeReleaseIdentityProvider(Path(args.release_git_root))
    raise SystemExit(
        "one trusted release source is required: "
        "--release-attestation or --release-git-root"
    )


def _measure_exact_release(args, certified_sha: str) -> None:
    measured = _release_provider(args).measure()
    if measured.deployed_sha != certified_sha:
        raise SystemExit(
            "trusted deployed SHA does not equal the certified PAY-24-G SHA"
        )


def _interactive_exact(prompt: str, expected: str) -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SystemExit("PAY-24-G real-money command requires an interactive terminal")
    typed = input(prompt)
    if typed != expected:
        raise SystemExit("confirmation phrase mismatch; no live payment started")


async def _set_org(session: AsyncSession, organization_id: uuid.UUID) -> None:
    await session.execute(
        text("SELECT pg_catalog.set_config('app.current_org_id',:org,true)"),
        {"org": str(organization_id)},
    )


def _load_pay24f_evidence(path: str) -> dict[str, object]:
    source = Path(path).expanduser().resolve()
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit("PAY-24-F preparation evidence is unreadable") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("phase") != "PAY-24-F"
        or payload.get("kind") != "live_order_preparation"
        or payload.get("provider_code") != "razorpay"
        or payload.get("provider_environment") != "live"
        or payload.get("real_money_movement") is not False
    ):
        raise SystemExit("PAY-24-F preparation evidence is invalid")
    return payload


async def _prepare_existing_checkout(
    *,
    app_url_name: str,
    organization_id: uuid.UUID,
    subscription_id: uuid.UUID,
) -> MemberSubscriptionCheckoutPreparation:
    engine = _engine_from_env(app_url_name)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as session:
            await _set_org(session, organization_id)
            prepared = await SourceBoundMemberSubscriptionCheckoutService(
                session
            ).prepare_local_checkout(
                organization_id=organization_id,
                subscription_id=subscription_id,
                provider_code="razorpay",
                provider_environment="live",
            )
            await session.commit()
            return prepared
    finally:
        await engine.dispose()


async def _require_exact_stage1(
    *,
    organization_id: uuid.UUID,
    certified_sha: str,
) -> None:
    engine = _engine_from_env("FINANCE_CONFIG_DATABASE_URL")
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as session:
            snapshot = await DurableActivationAuthority(session).snapshot()
            await session.rollback()
        if not (
            snapshot.stage is ActivationStage.INTERNAL_ORGANIZATION
            and snapshot.provider_egress == "open"
            and tuple(item.value for item in snapshot.enabled_capabilities)
                == _EXPECTED_CAPABILITIES
            and snapshot.internal_organization_id == organization_id
            and snapshot.certified_sha == certified_sha
            and snapshot.deployed_sha == certified_sha
            and snapshot.authorized_stage is ActivationStage.INTERNAL_ORGANIZATION
        ):
            raise SystemExit("PAY-24-G exact Stage-1 posture is not active")
    finally:
        await engine.dispose()


async def _assert_reconciliation_identity(session: AsyncSession) -> None:
    row = (
        await session.execute(
            text(
                """
                SELECT r.rolname,session_user::text,current_user::text,
                       r.rolsuper,r.rolcreatedb,r.rolcreaterole,
                       r.rolreplication,r.rolbypassrls,
                       current_setting('row_security')::text
                FROM pg_catalog.pg_roles r
                WHERE r.rolname=session_user
                """
            )
        )
    ).one()
    if (
        row[0] != row[1]
        or row[1] != row[2]
        or any(bool(value) for value in row[3:8])
        or str(row[8]).lower() != "on"
    ):
        raise SystemExit("PAY-24-G reconciliation login posture is unsafe")

    allowed = (
        await session.execute(
            text(
                "SELECT pg_catalog.pg_has_role("
                "session_user,'finance_reconciliation_runtime','MEMBER')"
            )
        )
    ).scalar_one()
    if allowed is not True:
        raise SystemExit(
            "PAY-24-G reconciliation login lacks finance_reconciliation_runtime"
        )
    for role in _PEER_ROLES:
        reachable = (
            await session.execute(
                text(
                    "SELECT pg_catalog.pg_has_role(session_user,:role,'MEMBER')"
                ),
                {"role": role},
            )
        ).scalar_one()
        if reachable:
            raise SystemExit(
                f"PAY-24-G reconciliation login reaches forbidden peer role: {role}"
            )


def _operation_sha(prepared: MemberSubscriptionCheckoutPreparation) -> str:
    canonical = json.dumps(
        {
            "finance_checkout_intent_id": str(prepared.finance_checkout_intent_id),
            "finance_invoice_id": str(prepared.finance_invoice_id),
            "provider_order_ref": prepared.provider_order_ref,
            "amount": str(prepared.amount),
            "currency_code": prepared.currency_code,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class _CallbackResult:
    status: str
    payload: dict[str, str] | None = None


def _checkout_html(
    *,
    token: str,
    key_id: str,
    order_id: str,
    amount: Decimal,
    currency: str,
) -> bytes:
    safe_key = escape(key_id, quote=True)
    safe_order = escape(order_id, quote=True)
    safe_amount = escape(str(amount), quote=True)
    safe_currency = escape(currency, quote=True)
    safe_token = escape(token, quote=True)
    html = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>DOERS PAY-24-G Internal Canary</title>
</head>
<body>
  <h1>DOERS Internal Payment Canary</h1>
  <p>Amount: {safe_currency} {safe_amount}</p>
  <p>This page is bound to one certified internal canary order.</p>
  <button id="pay">Pay with Razorpay</button>
  <pre id="status">Ready</pre>
  <script src="https://checkout.razorpay.com/v1/checkout.js"></script>
  <script>
    const statusNode = document.getElementById("status");
    async function send(path, body) {{
      await fetch(path + "/{safe_token}", {{
        method: "POST",
        headers: {{"Content-Type": "application/json"}},
        body: JSON.stringify(body || {{}})
      }});
    }}
    const options = {{
      key: "{safe_key}",
      order_id: "{safe_order}",
      name: "DOERS Internal Canary",
      description: "Certified Stage-1 payment canary",
      retry: {{enabled: false}},
      handler: async function (response) {{
        statusNode.textContent = "Payment returned; verifying in terminal...";
        await send("/callback", {{
          razorpay_payment_id: response.razorpay_payment_id,
          razorpay_order_id: response.razorpay_order_id,
          razorpay_signature: response.razorpay_signature
        }});
      }},
      modal: {{
        ondismiss: async function () {{
          statusNode.textContent = "Checkout dismissed";
          await send("/dismissed", {{}});
        }}
      }}
    }};
    const checkout = new Razorpay(options);
    checkout.on("payment.failed", async function () {{
      statusNode.textContent = "Payment failed";
      await send("/failed", {{}});
    }});
    document.getElementById("pay").onclick = function (event) {{
      checkout.open();
      event.preventDefault();
    }};
  </script>
</body>
</html>"""
    return html.encode("utf-8")


def _collect_callback(
    *,
    key_id: str,
    order_id: str,
    amount: Decimal,
    currency: str,
) -> _CallbackResult:
    token = secrets.token_urlsafe(24)
    result = _CallbackResult(status="timeout")
    done = threading.Event()
    page = _checkout_html(
        token=token,
        key_id=key_id,
        order_id=order_id,
        amount=amount,
        currency=currency,
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def _json(self, status: int, payload: dict[str, object]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if urlparse(self.path).path != f"/pay/{token}":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(page)

        def do_POST(self):
            nonlocal result
            path = urlparse(self.path).path
            if path not in {
                f"/callback/{token}",
                f"/dismissed/{token}",
                f"/failed/{token}",
            }:
                self.send_error(404)
                return
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                self.send_error(415)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_error(400)
                return
            if length < 0 or length > 4096:
                self.send_error(413)
                return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.send_error(400)
                return
            if path == f"/callback/{token}":
                required = {
                    "razorpay_payment_id",
                    "razorpay_order_id",
                    "razorpay_signature",
                }
                if not isinstance(payload, dict) or set(payload) != required:
                    self.send_error(400)
                    return
                if not all(
                    isinstance(payload[key], str) and payload[key].strip()
                    for key in required
                ):
                    self.send_error(400)
                    return
                result = _CallbackResult(
                    status="callback",
                    payload={key: payload[key].strip() for key in required},
                )
            elif path == f"/failed/{token}":
                result = _CallbackResult(status="failed")
            else:
                result = _CallbackResult(status="dismissed")
            self._json(200, {"accepted": True})
            done.set()

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = 1.0
    url = f"http://127.0.0.1:{server.server_port}/pay/{token}"
    print(f"Open this one-time local checkout URL in your browser:\n{url}", flush=True)
    deadline = time.monotonic() + _CALLBACK_TIMEOUT_SECONDS
    try:
        while time.monotonic() < deadline and not done.is_set():
            server.handle_request()
    finally:
        server.server_close()
    return result


async def _finish_admission(
    *,
    payment_session: AsyncSession,
    authority: DurableActivationAuthority,
    organization_id: uuid.UUID,
    admission_id: uuid.UUID,
    execution_id: uuid.UUID,
    outcome: str,
) -> None:
    await _set_org(payment_session, organization_id)
    await authority.finish_provider_admission(
        admission_id=admission_id,
        execution_id=execution_id,
        outcome=outcome,
    )
    await payment_session.commit()


async def _wait_for_subscription_active(
    *,
    organization_id: uuid.UUID,
    subscription_id: uuid.UUID,
    timeout_seconds: int = 120,
) -> bool:
    engine = _engine_from_env("PAY24F_APP_DATABASE_URL")
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    deadline = time.monotonic() + timeout_seconds
    try:
        while time.monotonic() < deadline:
            async with maker() as session:
                await _set_org(session, organization_id)
                status = (
                    await session.execute(
                        text(
                            "SELECT status::text FROM public.member_subscriptions_v2 "
                            "WHERE id=:subscription_id AND org_id=:organization_id"
                        ),
                        {
                            "subscription_id": subscription_id,
                            "organization_id": organization_id,
                        },
                    )
                ).scalar_one_or_none()
                await session.rollback()
            if status == "active":
                return True
            await asyncio.sleep(2)
        return False
    finally:
        await engine.dispose()


async def run_live_payment(args) -> int:
    evidence = _load_pay24f_evidence(args.pay24f_evidence)
    certified_sha = args.certified_sha
    if evidence.get("certified_sha") != certified_sha:
        raise SystemExit("PAY-24-F evidence SHA does not match PAY-24-G candidate")
    _measure_exact_release(args, certified_sha)

    organization_id = uuid.UUID(str(evidence["organization_id"]))
    subscription_id = uuid.UUID(str(evidence["subscription_id"]))
    if str(organization_id) != args.internal_org:
        raise SystemExit("PAY-24-G internal organization mismatch")

    await _require_exact_stage1(
        organization_id=organization_id,
        certified_sha=certified_sha,
    )

    prepared = await _prepare_existing_checkout(
        app_url_name="PAY24F_APP_DATABASE_URL",
        organization_id=organization_id,
        subscription_id=subscription_id,
    )
    if not prepared.provider_order_ref or not prepared.provider_order_ref.startswith("order_"):
        raise SystemExit("PAY-24-G requires an attached live Razorpay order")
    if prepared.currency_code != _CANARY_CURRENCY:
        raise SystemExit("PAY-24-G canary permits INR only")
    if prepared.amount <= 0 or prepared.amount > _MAX_CANARY_AMOUNT:
        raise SystemExit("PAY-24-G canary amount exceeds the INR 10.00 hard limit")
    if str(prepared.finance_checkout_intent_id) != str(evidence["finance_checkout_intent_id"]):
        raise SystemExit("PAY-24-G Finance checkout intent drift")
    if str(prepared.finance_invoice_id) != str(evidence["finance_invoice_id"]):
        raise SystemExit("PAY-24-G Finance invoice drift")

    phrase = (
        f"AUTHORIZE LIVE PAYMENT INR {prepared.amount:.2f} "
        f"{prepared.provider_order_ref}"
    )
    _interactive_exact(
        "This next step can move real money. Type exactly:\n"
        f"{phrase}\n> ",
        phrase,
    )

    live_config = RazorpayLiveConfig(
        mode="live",
        key_id=_required("RAZORPAY_LIVE_KEY_ID"),
        key_secret=_required("RAZORPAY_LIVE_KEY_SECRET"),
        merchant_reference=_required("RAZORPAY_LIVE_MERCHANT_REFERENCE"),
    )
    payments_client = RazorpayLivePaymentsClient(
        config=live_config,
        transport=RazorpayLiveHTTPTransport(),
    )

    payment_engine = _engine_from_env("PAY24F_PAYMENT_DATABASE_URL")
    payment_maker = async_sessionmaker(
        payment_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    execution_id = uuid.uuid4()
    admission = None
    try:
        async with payment_maker() as payment_session:
            await _set_org(payment_session, organization_id)
            authority = DurableActivationAuthority(payment_session)
            admission = await authority.request_current_provider_admission(
                capability=ActivationCapability.CHECKOUT,
                logical_operation_id=(
                    f"pay24g-live-payment:{prepared.finance_checkout_intent_id}"
                ),
                operation_sha=_operation_sha(prepared),
                lease_seconds=_ADMISSION_LEASE_SECONDS,
            )
            await payment_session.commit()

            await _set_org(payment_session, organization_id)
            started = await authority.start_provider_admission(
                admission_id=admission.admission_id,
                execution_id=execution_id,
            )
            await payment_session.commit()
            if started.state != "active":
                raise SystemExit("PAY-24-G provider admission did not become active")

            callback = await asyncio.to_thread(
                _collect_callback,
                key_id=live_config.key_id,
                order_id=prepared.provider_order_ref,
                amount=prepared.amount,
                currency=prepared.currency_code,
            )
            if callback.status != "callback" or callback.payload is None:
                await _finish_admission(
                    payment_session=payment_session,
                    authority=authority,
                    organization_id=organization_id,
                    admission_id=admission.admission_id,
                    execution_id=execution_id,
                    outcome="unknown",
                )
                raise SystemExit(
                    "PAY-24-G checkout ended without a verified success callback; "
                    "do not retry until provider reconciliation"
                )

            reconciliation_engine = _engine_from_env(
                "PAY24G_RECONCILIATION_DATABASE_URL"
            )
            reconciliation_maker = async_sessionmaker(
                reconciliation_engine,
                class_=AsyncSession,
                expire_on_commit=False,
            )
            try:
                async with reconciliation_maker() as reconciliation:
                    await _assert_reconciliation_identity(reconciliation)
                    service = FinanceTerminalLivePaymentEvidenceService(
                        reconciliation,
                        key_secret=live_config.key_secret,
                        payments_client=payments_client,
                    )
                    confirmed = await service.confirm_captured_payment(
                        ConfirmTerminalLivePaymentCommand(
                            provider_order_ref=callback.payload[
                                "razorpay_order_id"
                            ],
                            provider_payment_ref=callback.payload[
                                "razorpay_payment_id"
                            ],
                            checkout_signature=callback.payload[
                                "razorpay_signature"
                            ],
                            expected_amount_subunits=int(prepared.amount * 100),
                            expected_currency=prepared.currency_code,
                        )
                    )
                    await reconciliation.commit()
            except Exception:
                await _finish_admission(
                    payment_session=payment_session,
                    authority=authority,
                    organization_id=organization_id,
                    admission_id=admission.admission_id,
                    execution_id=execution_id,
                    outcome="unknown",
                )
                raise
            finally:
                await reconciliation_engine.dispose()

            await _finish_admission(
                payment_session=payment_session,
                authority=authority,
                organization_id=organization_id,
                admission_id=admission.admission_id,
                execution_id=execution_id,
                outcome="completed",
            )

        app_engine = _engine_from_env("PAY24F_APP_DATABASE_URL")
        app_maker = async_sessionmaker(
            app_engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        try:
            async with app_maker() as app_session:
                await _set_org(app_session, organization_id)
                applied = await FinancePaymentApplicationGateService(
                    app_session
                ).apply_confirmed_payment(
                    ApplyConfirmedPaymentCommand(
                        payment_id=confirmed.payment_id,
                        invoice_id=prepared.finance_invoice_id,
                        amount=prepared.amount,
                        currency_code=prepared.currency_code,
                        idempotency_key=(
                            f"pay24g:apply:{confirmed.payment_id}"
                        ),
                        internal_actor="ops_admin",
                        reason="PAY-24-G internal live payment canary",
                    )
                )
                await app_session.commit()
        finally:
            await app_engine.dispose()

        entitlement_active = await _wait_for_subscription_active(
            organization_id=organization_id,
            subscription_id=subscription_id,
        )

        print(
            json.dumps(
                {
                    "pay24g_live_payment": "PASS",
                    "organization_id": str(organization_id),
                    "subscription_id": str(subscription_id),
                    "finance_payment_id": str(confirmed.payment_id),
                    "finance_invoice_id": str(prepared.finance_invoice_id),
                    "invoice_status": applied.invoice_status,
                    "amount": str(prepared.amount),
                    "currency_code": prepared.currency_code,
                    "entitlement_active": entitlement_active,
                    "real_money_movement": True,
                    "rollback_required_after_canary": True,
                },
                sort_keys=True,
            )
        )
        if not entitlement_active:
            return 3
        return 0
    finally:
        await payment_engine.dispose()


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("run-live-payment", nargs="?")
    p.add_argument("--certified-sha", required=True)
    p.add_argument("--internal-org", required=True)
    p.add_argument("--pay24f-evidence", required=True)
    p.add_argument("--release-attestation")
    p.add_argument("--release-git-root")
    return p


async def async_main() -> int:
    args = parser().parse_args()
    return await run_live_payment(args)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main()))
