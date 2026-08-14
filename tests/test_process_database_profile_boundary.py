from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app.core.config import Settings


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


def _settings(**values) -> Settings:
    # BaseSettings deliberately reads ambient process variables. These tests prove
    # the profile contract in isolation, so CI/runtime database variables must not
    # bleed into a synthetic process profile.
    with patch.dict(os.environ, {}, clear=True):
        return Settings(_env_file=None, **(_BASE | values))


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
    assert settings.database_component_enabled("api")
    assert settings.database_component_enabled("auth")
    assert not settings.database_component_enabled("worker")
    assert not settings.database_component_enabled("maintenance")


def test_api_profile_rejects_worker_database_credential_exposure() -> None:
    with pytest.raises(ValidationError, match="forbidden database variables"):
        _settings(
            DOERS_PROCESS_PROFILE="api",
            DATABASE_URL=_API,
            AUTH_DATABASE_URL=_AUTH,
            WORKER_DATABASE_URL=_WORKER,
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
    assert "invalid.invalid" in settings.DATABASE_URL
    assert settings.database_component_enabled("worker")
    assert not settings.database_component_enabled("api")
    assert not settings.database_component_enabled("maintenance")


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
    )
    assert settings.MAINTENANCE_DATABASE_URL == _MAINTENANCE
    assert settings.WORKER_DATABASE_URL == ""
    assert settings.AUTH_DATABASE_URL == ""
    assert "invalid.invalid" in settings.DATABASE_URL
    assert settings.database_component_enabled("maintenance")
    assert not settings.database_component_enabled("worker")


def test_beat_profile_has_no_database_identity() -> None:
    settings = _settings(DOERS_PROCESS_PROFILE="beat")
    assert "invalid.invalid" in settings.DATABASE_URL
    assert settings.AUTH_DATABASE_URL == ""
    assert settings.WORKER_DATABASE_URL == ""
    assert settings.MAINTENANCE_DATABASE_URL == ""
    assert not any(
        settings.database_component_enabled(component)
        for component in ("api", "auth", "worker", "maintenance")
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
