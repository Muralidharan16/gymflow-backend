from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BINDINGS = json.loads(
    (ROOT / "security/runtime_identity/runtime_bindings.v1.json").read_text(
        encoding="utf-8"
    )
)
PROFILES = json.loads(
    (ROOT / "security/runtime_identity/process_profiles.v1.json").read_text(
        encoding="utf-8"
    )
)
DATABASE_CORE = (ROOT / "app/core/database.py").read_text(encoding="utf-8")
PAYMENT_DB = (ROOT / "app/core/payment_database.py").read_text(encoding="utf-8")
PAYMENT_API = (ROOT / "app/finance_core/api/payment_boundary.py").read_text(
    encoding="utf-8"
)
MEMBER_API = (ROOT / "app/routers/member_subscriptions_v2.py").read_text(
    encoding="utf-8"
)
PROD_IDENTITIES = (
    ROOT / "deploy/docker-compose.production-identities.yml"
).read_text(encoding="utf-8")


def test_ri1a_binds_only_finance_payment_from_reserved_finance_capabilities() -> None:
    payment = BINDINGS["bindings"]["finance_payment"]
    assert payment["environment_variable"] == "FINANCE_PAYMENT_DATABASE_URL"
    assert payment["runtime_capability"] == "finance_payment_runtime"
    assert payment["direct_capabilities"] == ["finance_payment_runtime"]
    assert payment["session_settings"] == {
        "row_security": "on",
        "statement_timeout": "15s",
        "lock_timeout": "2s",
        "idle_in_transaction_session_timeout": "30s",
    }
    assert set(BINDINGS["reserved_unbound_capabilities"]) == {
        "finance_runtime",
        "finance_refund_runtime",
        "finance_reconciliation_runtime",
        "finance_read_runtime",
        "finance_maintenance_runtime",
    }
    assert BINDINGS["bindings"]["api"]["direct_capabilities"] == [
        "app_runtime",
        "app_user",
    ]


def test_api_payment_profile_is_dormant_opt_in_and_ordinary_api_stays_closed() -> None:
    ordinary = PROFILES["profiles"]["api"]
    payment = PROFILES["profiles"]["api_payment"]
    assert ordinary["runtime_components"] == ["api", "auth"]
    assert "FINANCE_PAYMENT_DATABASE_URL" in ordinary["forbidden_database_variables"]
    assert payment["runtime_components"] == ["api", "auth", "finance_payment"]
    assert payment["required_database_variables"] == [
        "DATABASE_URL",
        "AUTH_DATABASE_URL",
        "FINANCE_PAYMENT_DATABASE_URL",
    ]
    assert "DOERS_PROCESS_PROFILE: api" in PROD_IDENTITIES
    assert "DOERS_PROCESS_PROFILE: api_payment" not in PROD_IDENTITIES


def test_payment_database_isolated_same_database_request_scoped_and_no_role_switch() -> None:
    for token in (
        "settings.FINANCE_PAYMENT_DATABASE_URL",
        "make_url(settings.DATABASE_URL)",
        "ordinary.database != payment.database",
        "ordinary.username == payment.username",
        "make_url(auth_raw).username == payment.username",
        "install_connection_identity_guard(",
        '"finance_payment"',
        "poolclass=NullPool",
        "initialize_request_session(",
        "await session.commit()",
        "await session.rollback()",
    ):
        assert token in PAYMENT_DB
    assert "SET ROLE" not in PAYMENT_DB.upper()
    assert "GRANT FINANCE_PAYMENT_RUNTIME" not in PAYMENT_DB.upper()
    assert "PG_HAS_ROLE" not in PAYMENT_DB.upper()


def test_b1a_and_b1b_checkout_routes_use_payment_identity_without_production_secret() -> None:
    route = PAYMENT_API.split("async def create_checkout_session(", 1)[1].split(
        "@router.get", 1
    )[0]
    member_route = MEMBER_API.split(
        "async def create_subscription_checkout_session(", 1
    )[1]
    assert "payment_db: AsyncSession = Depends(get_finance_payment_db)" in route
    assert (
        "payment_db: AsyncSession = Depends(get_finance_payment_db)"
        in member_route
    )
    assert "db: AsyncSession = Depends(get_db)" in member_route
    assert "FINANCE_PAYMENT_DATABASE_URL" not in PROD_IDENTITIES


def test_payment_request_context_preserves_attested_runtime_timeout_defaults() -> None:
    assert '_TIMEOUT_PROFILE_RUNTIME_DEFAULT = "runtime_default"' in DATABASE_CORE
    assert 'timeout_profile: str = _TIMEOUT_PROFILE_API' in DATABASE_CORE
    assert '"timeout_profile": normalized_timeout_profile' in DATABASE_CORE
    assert 'elif timeout_profile == _TIMEOUT_PROFILE_API:' in DATABASE_CORE
    assert 'timeout_profile="runtime_default"' in PAYMENT_DB
    assert "internal_maintenance=" not in PAYMENT_DB
