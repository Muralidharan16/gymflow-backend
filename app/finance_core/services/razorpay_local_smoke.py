from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from app.finance_core.domain.provider_boundary import FinanceProviderConfigError
from app.finance_core.domain.razorpay_sandbox import RazorpaySandboxConfig, validate_razorpay_sandbox_config


RAZORPAY_PROVIDER_MODE = "RAZORPAY_PROVIDER_MODE"
RAZORPAY_KEY_ID = "RAZORPAY_KEY_ID"
RAZORPAY_KEY_SECRET = "RAZORPAY_KEY_SECRET"
RAZORPAY_WEBHOOK_SECRET = "RAZORPAY_WEBHOOK_SECRET"


class RazorpayLocalSmokeErrorCode(str, Enum):
    MISSING_PROVIDER_MODE = "MISSING_PROVIDER_MODE"
    UNSAFE_PROVIDER_MODE = "UNSAFE_PROVIDER_MODE"
    MISSING_KEY_ID = "MISSING_KEY_ID"
    MISSING_KEY_SECRET = "MISSING_KEY_SECRET"
    MISSING_WEBHOOK_SECRET = "MISSING_WEBHOOK_SECRET"
    LIVE_KEY_REJECTED = "LIVE_KEY_REJECTED"
    UNSAFE_CONFIG = "UNSAFE_CONFIG"
    TEST_NETWORK_NOT_ALLOWED = "TEST_NETWORK_NOT_ALLOWED"


@dataclass(frozen=True)
class RazorpayLocalSmokeConfigError(Exception):
    code: RazorpayLocalSmokeErrorCode
    message: str

    def __str__(self) -> str:
        return f"{self.code.value}: {self.message}"


@dataclass(frozen=True)
class RazorpayLocalSmokeReadiness:
    provider_mode: str
    key_id_redacted: str
    key_secret_present: bool
    webhook_secret_present: bool
    payment_route_defaults_disabled: bool
    explicit_test_overrides_required: bool
    dry_run: bool
    network_execution_allowed: bool

    def to_safe_output(self) -> dict[str, str | bool]:
        return {
            "provider_mode": self.provider_mode,
            "key_id": self.key_id_redacted,
            "key_secret_present": self.key_secret_present,
            "webhook_secret_present": self.webhook_secret_present,
            "payment_route_defaults_disabled": self.payment_route_defaults_disabled,
            "explicit_test_overrides_required": self.explicit_test_overrides_required,
            "dry_run": self.dry_run,
            "network_execution_allowed": self.network_execution_allowed,
        }


@dataclass(frozen=True)
class RazorpayLocalSmokePlan:
    readiness: RazorpayLocalSmokeReadiness
    checklist: tuple[str, ...]

    def to_safe_output(self) -> dict[str, object]:
        return {
            "readiness": self.readiness.to_safe_output(),
            "checklist": list(self.checklist),
        }


def load_razorpay_test_mode_config_from_env(
    environ: Mapping[str, str] | None = None,
    *,
    require_webhook_secret: bool = False,
    merchant_reference: str = "vitara_local_smoke",
) -> RazorpaySandboxConfig:
    env = os.environ if environ is None else environ
    mode = _required_env(env, RAZORPAY_PROVIDER_MODE, RazorpayLocalSmokeErrorCode.MISSING_PROVIDER_MODE).lower()
    if mode not in {"test", "sandbox"}:
        raise RazorpayLocalSmokeConfigError(
            RazorpayLocalSmokeErrorCode.UNSAFE_PROVIDER_MODE,
            "Razorpay local smoke provider mode must be test or sandbox.",
        )

    key_id = _required_env(env, RAZORPAY_KEY_ID, RazorpayLocalSmokeErrorCode.MISSING_KEY_ID)
    _reject_live_marker(key_id)
    key_secret = _required_env(env, RAZORPAY_KEY_SECRET, RazorpayLocalSmokeErrorCode.MISSING_KEY_SECRET)
    _reject_live_marker(key_secret)
    webhook_secret = _env_value(env, RAZORPAY_WEBHOOK_SECRET)
    if require_webhook_secret and not webhook_secret:
        raise RazorpayLocalSmokeConfigError(
            RazorpayLocalSmokeErrorCode.MISSING_WEBHOOK_SECRET,
            "Razorpay webhook secret is required for local webhook smoke readiness.",
        )

    try:
        return validate_razorpay_sandbox_config(
            RazorpaySandboxConfig(
                mode=mode,  # type: ignore[arg-type]
                key_id=key_id,
                key_secret=key_secret,
                webhook_secret=webhook_secret or "__dry_run_webhook_secret_not_for_network__",
                merchant_reference=merchant_reference,
            )
        )
    except FinanceProviderConfigError as exc:
        raise RazorpayLocalSmokeConfigError(
            RazorpayLocalSmokeErrorCode.UNSAFE_CONFIG,
            "Razorpay local smoke config failed safety validation.",
        ) from exc


def build_razorpay_local_smoke_plan(
    *,
    environ: Mapping[str, str] | None = None,
    require_webhook_secret: bool = True,
    allow_test_network: bool = False,
) -> RazorpayLocalSmokePlan:
    if allow_test_network:
        raise RazorpayLocalSmokeConfigError(
            RazorpayLocalSmokeErrorCode.TEST_NETWORK_NOT_ALLOWED,
            "Phase 6Z dry-run harness does not permit real Razorpay network execution.",
        )

    config = load_razorpay_test_mode_config_from_env(environ, require_webhook_secret=require_webhook_secret)
    readiness = RazorpayLocalSmokeReadiness(
        provider_mode=config.mode,
        key_id_redacted=redact_razorpay_key_id(config.key_id),
        key_secret_present=True,
        webhook_secret_present=bool(config.webhook_secret),
        payment_route_defaults_disabled=True,
        explicit_test_overrides_required=True,
        dry_run=True,
        network_execution_allowed=False,
    )
    checklist = (
        "verify git working tree is clean",
        "verify provider mode is test or sandbox",
        "verify payment route defaults remain disabled",
        "inject sandbox/test checkout dependencies only in controlled local process",
        "do not create real Razorpay order in Phase 6Z dry-run",
        "verify webhook raw body and signature prerequisites without printing secrets",
        "verify checkout and webhook do not allocate, post ledger entries, or mark invoices paid",
        "verify internal apply remains explicit and subscription/entitlement updates remain unchanged",
    )
    return RazorpayLocalSmokePlan(readiness=readiness, checklist=checklist)


def redact_razorpay_key_id(key_id: str) -> str:
    normalized = key_id.strip()
    if len(normalized) <= 4:
        return "[REDACTED]"
    return f"[REDACTED]...{normalized[-4:]}"


def _required_env(env: Mapping[str, str], name: str, code: RazorpayLocalSmokeErrorCode) -> str:
    value = _env_value(env, name)
    if not value:
        raise RazorpayLocalSmokeConfigError(code, f"{name} is required for Razorpay local smoke readiness.")
    return value


def _env_value(env: Mapping[str, str], name: str) -> str:
    return str(env.get(name, "")).strip()


def _reject_live_marker(value: str) -> None:
    if "rzp_" + "live_" in value:
        raise RazorpayLocalSmokeConfigError(
            RazorpayLocalSmokeErrorCode.LIVE_KEY_REJECTED,
            "Razorpay live-mode key material is not allowed in local smoke readiness.",
        )
