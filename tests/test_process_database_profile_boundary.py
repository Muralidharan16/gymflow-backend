from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from pydantic import ValidationError



_BASE = {
    "REDIS_URL": "redis://localhost:6379/0",
    "CELERY_BROKER_URL": "redis://localhost:6379/1",
    "CELERY_RESULT_BACKEND": "redis://localhost:6379/2",
    "SECRET_KEY": "test-secret",
    "AWS_ACCESS_KEY_ID": "test",
    "AWS_SECRET_ACCESS_KEY": "test",
    "ENVIRONMENT": "production",
}
_API = "postgresql+asyncpg://api_login@localhost/doers"
_AUTH = "postgresql+asyncpg://auth_login@localhost/doers"
_WORKER = "postgresql+asyncpg://worker_login@localhost/doers"
_MAINTENANCE = "postgresql+asyncpg://maintenance_login@localhost/doers"
_FINANCE_CONFIG = "postgresql+psycopg://finance_config_deployment@localhost/doers"
_PAYMENT = "postgresql+asyncpg://finance_payment_deployment@localhost/doers"
_ENTITLEMENT = "postgresql+asyncpg://entitlement_deployment@db.internal/doers?ssl=verify-full"
_P4E_METRICS = "https://otel.example.test/v1/metrics"


def _settings(**values):
    # BaseSettings deliberately reads ambient process variables. These tests prove
    # the profile contract in isolation, so CI/runtime database variables must not
    # bleed into a synthetic process profile. Import Settings only after the
    # synthetic environment is installed because app.core.config also constructs
    # its process-global settings object at import time.
    merged = _BASE | values
    synthetic_env = {
        key: str(value)
        for key, value in merged.items()
        if value is not None
    }
    with patch.dict(os.environ, synthetic_env, clear=True):
        from app.core.config import Settings

        return Settings(_env_file=None, **merged)


def test_api_profile_exposes_only_api_and_auth_database_components() -> None:
    settings = _settings(
        DOERS_PROCESS_PROFILE="api",
        DATABASE_URL=_API,
        AUTH_DATABASE_URL=_AUTH,
    )
    assert settings.DATABASE_URL == _API
    assert settings.AUTH_DATABASE_URL == _AUTH
    assert settings.WORKER_DATABASE_URL == ""
    assert settings.MAINTENANCE_DATABASE_URL == ""
    assert settings.FINANCE_CONFIG_DATABASE_URL == ""
    assert settings.FINANCE_PAYMENT_DATABASE_URL == ""
    assert settings.ENTITLEMENT_DATABASE_URL == ""
    assert settings.database_component_enabled("api")
    assert settings.database_component_enabled("auth")
    assert not settings.database_component_enabled("finance_payment")
    assert not settings.database_component_enabled("worker")
    assert not settings.database_component_enabled("maintenance")
    assert not settings.database_component_enabled("finance_config")
    assert not settings.database_component_enabled("entitlement")


def test_api_payment_profile_exposes_api_auth_and_payment_only() -> None:
    settings = _settings(
        DOERS_PROCESS_PROFILE="api_payment",
        DATABASE_URL=_API,
        AUTH_DATABASE_URL=_AUTH,
        FINANCE_PAYMENT_DATABASE_URL=_PAYMENT,
    )
    assert settings.DATABASE_URL == _API
    assert settings.AUTH_DATABASE_URL == _AUTH
    assert settings.FINANCE_PAYMENT_DATABASE_URL == _PAYMENT
    assert settings.WORKER_DATABASE_URL == ""
    assert settings.MAINTENANCE_DATABASE_URL == ""
    assert settings.FINANCE_CONFIG_DATABASE_URL == ""
    assert settings.database_component_enabled("api")
    assert settings.database_component_enabled("auth")
    assert settings.database_component_enabled("finance_payment")
    assert not settings.database_component_enabled("worker")
    assert not settings.database_component_enabled("maintenance")
    assert not settings.database_component_enabled("finance_config")
    assert not settings.database_component_enabled("entitlement")


def test_api_profile_rejects_payment_database_credential_exposure() -> None:
    with pytest.raises(ValidationError, match="forbidden database variables"):
        _settings(
            DOERS_PROCESS_PROFILE="api",
            DATABASE_URL=_API,
            AUTH_DATABASE_URL=_AUTH,
            FINANCE_PAYMENT_DATABASE_URL=_PAYMENT,
        )


def test_api_payment_profile_requires_payment_database_identity() -> None:
    with pytest.raises(ValidationError, match="missing required database variables"):
        _settings(
            DOERS_PROCESS_PROFILE="api_payment",
            DATABASE_URL=_API,
            AUTH_DATABASE_URL=_AUTH,
        )


def test_api_profile_rejects_worker_database_credential_exposure() -> None:
    with pytest.raises(ValidationError, match="forbidden database variables"):
        _settings(
            DOERS_PROCESS_PROFILE="api",
            DATABASE_URL=_API,
            AUTH_DATABASE_URL=_AUTH,
            WORKER_DATABASE_URL=_WORKER,
            FINANCE_CONFIG_DATABASE_URL="",
        )


def test_worker_profile_has_only_worker_database_identity() -> None:
    settings = _settings(
        DOERS_PROCESS_PROFILE="worker",
        CELERY_WORKER_PROFILE="worker",
        WORKER_DATABASE_URL=_WORKER,
    )
    assert settings.WORKER_DATABASE_URL == _WORKER
    assert settings.AUTH_DATABASE_URL == ""
    assert settings.MAINTENANCE_DATABASE_URL == ""
    assert settings.FINANCE_CONFIG_DATABASE_URL == ""
    assert settings.FINANCE_PAYMENT_DATABASE_URL == ""
    assert settings.ENTITLEMENT_DATABASE_URL == ""
    assert "invalid.invalid" in settings.DATABASE_URL
    assert settings.database_component_enabled("worker")
    assert not settings.database_component_enabled("api")
    assert not settings.database_component_enabled("finance_payment")
    assert not settings.database_component_enabled("maintenance")
    assert not settings.database_component_enabled("finance_config")
    assert not settings.database_component_enabled("entitlement")


def test_worker_profile_rejects_api_database_credential_exposure() -> None:
    with pytest.raises(ValidationError, match="forbidden database variables"):
        _settings(
            DOERS_PROCESS_PROFILE="worker",
            CELERY_WORKER_PROFILE="worker",
            WORKER_DATABASE_URL=_WORKER,
            DATABASE_URL=_API,
        )


def test_maintenance_profile_has_only_maintenance_database_identity() -> None:
    settings = _settings(
        DOERS_PROCESS_PROFILE="maintenance",
        CELERY_WORKER_PROFILE="maintenance",
        MAINTENANCE_DATABASE_URL=_MAINTENANCE,
        P4E_METRICS_OTLP_ENDPOINT=_P4E_METRICS,
    )
    assert settings.MAINTENANCE_DATABASE_URL == _MAINTENANCE
    assert settings.P4E_METRICS_OTLP_ENDPOINT == _P4E_METRICS
    assert settings.WORKER_DATABASE_URL == ""
    assert settings.AUTH_DATABASE_URL == ""
    assert settings.FINANCE_CONFIG_DATABASE_URL == ""
    assert settings.FINANCE_PAYMENT_DATABASE_URL == ""
    assert settings.ENTITLEMENT_DATABASE_URL == ""
    assert "invalid.invalid" in settings.DATABASE_URL
    assert settings.database_component_enabled("maintenance")
    assert not settings.database_component_enabled("finance_payment")
    assert not settings.database_component_enabled("worker")
    assert not settings.database_component_enabled("finance_config")
    assert not settings.database_component_enabled("entitlement")


def test_finance_config_profile_has_only_finance_config_database_identity() -> None:
    settings = _settings(
        DOERS_PROCESS_PROFILE="finance_config",
        FINANCE_CONFIG_DATABASE_URL=_FINANCE_CONFIG,
    )
    assert settings.FINANCE_CONFIG_DATABASE_URL == _FINANCE_CONFIG
    assert settings.AUTH_DATABASE_URL == ""
    assert settings.WORKER_DATABASE_URL == ""
    assert settings.MAINTENANCE_DATABASE_URL == ""
    assert settings.FINANCE_PAYMENT_DATABASE_URL == ""
    assert settings.ENTITLEMENT_DATABASE_URL == ""
    assert "invalid.invalid" in settings.DATABASE_URL
    assert settings.database_component_enabled("finance_config")
    assert not settings.database_component_enabled("api")
    assert not settings.database_component_enabled("finance_payment")
    assert not settings.database_component_enabled("worker")
    assert not settings.database_component_enabled("maintenance")


def test_entitlement_worker_profile_has_only_entitlement_database_identity() -> None:
    settings = _settings(
        DOERS_PROCESS_PROFILE="entitlement_worker",
        CELERY_WORKER_PROFILE="entitlement",
        ENTITLEMENT_DATABASE_URL=_ENTITLEMENT,
        AWS_ACCESS_KEY_ID="",
        AWS_SECRET_ACCESS_KEY="",
    )
    assert settings.ENTITLEMENT_DATABASE_URL == _ENTITLEMENT
    assert settings.AUTH_DATABASE_URL == ""
    assert settings.WORKER_DATABASE_URL == ""
    assert settings.MAINTENANCE_DATABASE_URL == ""
    assert settings.FINANCE_CONFIG_DATABASE_URL == ""
    assert settings.FINANCE_PAYMENT_DATABASE_URL == ""
    assert "invalid.invalid" in settings.DATABASE_URL
    assert settings.database_component_enabled("entitlement")
    assert not settings.database_component_enabled("api")
    assert not settings.database_component_enabled("worker")
    assert not settings.database_component_enabled("maintenance")


def test_entitlement_worker_rejects_other_database_credentials() -> None:
    with pytest.raises(ValidationError, match="forbidden database variables"):
        _settings(
            DOERS_PROCESS_PROFILE="entitlement_worker",
            CELERY_WORKER_PROFILE="entitlement",
            ENTITLEMENT_DATABASE_URL=_ENTITLEMENT,
            WORKER_DATABASE_URL=_WORKER,
            AWS_ACCESS_KEY_ID="",
            AWS_SECRET_ACCESS_KEY="",
        )



def test_entitlement_worker_requires_verify_full_database_tls() -> None:
    with pytest.raises(ValidationError, match="verify-full"):
        _settings(
            DOERS_PROCESS_PROFILE="entitlement_worker",
            CELERY_WORKER_PROFILE="entitlement",
            ENTITLEMENT_DATABASE_URL=(
                "postgresql+asyncpg://entitlement_deployment@db.internal/doers"
            ),
            AWS_ACCESS_KEY_ID="",
            AWS_SECRET_ACCESS_KEY="",
        )


def test_entitlement_worker_rejects_unrelated_cloud_and_provider_secrets() -> None:
    with pytest.raises(ValidationError, match="forbidden provider/cloud secrets"):
        _settings(
            DOERS_PROCESS_PROFILE="entitlement_worker",
            CELERY_WORKER_PROFILE="entitlement",
            ENTITLEMENT_DATABASE_URL=_ENTITLEMENT,
            AWS_ACCESS_KEY_ID="should-not-be-present",
            AWS_SECRET_ACCESS_KEY="",
        )

def test_finance_config_profile_rejects_ordinary_database_credentials() -> None:
    with pytest.raises(ValidationError, match="forbidden database variables"):
        _settings(
            DOERS_PROCESS_PROFILE="finance_config",
            FINANCE_CONFIG_DATABASE_URL=_FINANCE_CONFIG,
            DATABASE_URL=_API,
        )


def test_beat_profile_has_no_database_identity() -> None:
    settings = _settings(DOERS_PROCESS_PROFILE="beat")
    assert "invalid.invalid" in settings.DATABASE_URL
    assert settings.AUTH_DATABASE_URL == ""
    assert settings.WORKER_DATABASE_URL == ""
    assert settings.MAINTENANCE_DATABASE_URL == ""
    assert settings.FINANCE_CONFIG_DATABASE_URL == ""
    assert settings.FINANCE_PAYMENT_DATABASE_URL == ""
    assert settings.ENTITLEMENT_DATABASE_URL == ""
    assert not any(
        settings.database_component_enabled(component)
        for component in (
            "api", "auth", "finance_payment", "worker", "maintenance", "finance_config", "entitlement"
        )
    )


def test_beat_profile_rejects_any_database_credential() -> None:
    with pytest.raises(ValidationError, match="forbidden database variables"):
        _settings(DOERS_PROCESS_PROFILE="beat", DATABASE_URL=_API)


def test_production_requires_explicit_process_profile() -> None:
    with pytest.raises(ValidationError, match="DOERS_PROCESS_PROFILE"):
        _settings(DATABASE_URL=_API)


def test_celery_profile_must_match_process_profile() -> None:
    with pytest.raises(ValidationError, match="CELERY_WORKER_PROFILE"):
        _settings(
            DOERS_PROCESS_PROFILE="worker",
            CELERY_WORKER_PROFILE="maintenance",
            WORKER_DATABASE_URL=_WORKER,
        )
