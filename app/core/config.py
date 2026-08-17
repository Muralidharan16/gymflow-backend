"""Validated application settings and production process identity boundaries."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

from pydantic import model_validator

from app.core.settings_schema import DoersSettingsSchema


_PROCESS_PROFILE_MANIFEST = (
    Path(__file__).resolve().parents[2]
    / "security"
    / "runtime_identity"
    / "process_profiles.v1.json"
)
_DISABLED_ASYNC_URL = "postgresql+asyncpg://disabled@invalid.invalid/doers_disabled"


def _process_manifest() -> dict:
    raw = json.loads(_PROCESS_PROFILE_MANIFEST.read_text(encoding="utf-8"))
    if raw.get("schema_version") != 1 or not isinstance(raw.get("profiles"), dict):
        raise ValueError("unsupported P2E process profile manifest")
    return raw


def _validate_notification_metrics(endpoint: str, interval: float, timeout: float) -> None:
    parsed = urlparse(endpoint.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(
            "NOTIFICATION_METRICS_OTLP_ENDPOINT must be an HTTP(S) URL"
        )
    if not 1 <= interval <= 300:
        raise ValueError(
            "NOTIFICATION_METRICS_EXPORT_INTERVAL_SECONDS must be in the range [1, 300]"
        )
    if not 0 < timeout <= 60:
        raise ValueError(
            "NOTIFICATION_METRICS_EXPORT_TIMEOUT_SECONDS must be in the range (0, 60]"
        )


class Settings(DoersSettingsSchema):
    def _raw_runtime_value(self, component: str) -> str:
        from app.core.runtime_principal_attestation import load_runtime_binding_contract

        binding = load_runtime_binding_contract().bindings.get(component)
        if binding is None:
            raise ValueError(f"unknown runtime component: {component!r}")
        for field_name, field_info in type(self).model_fields.items():
            if field_info.alias == binding.environment_variable:
                return str(getattr(self, field_name) or "").strip()
        raise ValueError(f"runtime component {component!r} has no settings field")

    @model_validator(mode="after")
    def validate_database_identity_boundaries(self):
        from app.core.runtime_principal_attestation import (
            load_runtime_binding_contract,
            validate_runtime_url_configuration,
        )

        runtime = load_runtime_binding_contract()
        if self.ENVIRONMENT != "production":
            if not self._raw_runtime_value("api"):
                raise ValueError("application database configuration is required")
            return self

        manifest = _process_manifest()
        profiles = manifest["profiles"]
        profile_name = self.process_profile
        if set(profiles) != {"api", "worker", "maintenance", "beat"}:
            raise ValueError("P2E process manifest has invalid profile coverage")
        profile = profiles.get(profile_name)
        if profile is None:
            raise ValueError(
                "DOERS_PROCESS_PROFILE is required in production and must be "
                "api, worker, maintenance, or beat"
            )

        governed_variables = set(manifest.get("database_environment_variables", ()))
        runtime_variables = {
            binding.environment_variable for binding in runtime.bindings.values()
        }
        if governed_variables != runtime_variables:
            raise ValueError("P2E process manifest drifted from P2D runtime bindings")

        components = tuple(profile.get("runtime_components", ()))
        if any(component not in runtime.bindings for component in components):
            raise ValueError(f"invalid runtime component in process profile {profile_name!r}")

        required_variables = set(profile.get("required_database_variables", ()))
        expected_required = {
            runtime.bindings[component].environment_variable
            for component in components
        }
        forbidden_variables = set(profile.get("forbidden_database_variables", ()))
        if (
            required_variables != expected_required
            or required_variables & forbidden_variables
            or required_variables | forbidden_variables != governed_variables
        ):
            raise ValueError(f"process profile {profile_name!r} has an invalid variable partition")

        present = {
            binding.environment_variable: self._raw_runtime_value(component)
            for component, binding in runtime.bindings.items()
        }
        missing = sorted(name for name in required_variables if not present[name])
        forbidden_present = sorted(
            name for name in forbidden_variables if present[name]
        )
        if missing:
            raise ValueError(
                f"process profile {profile_name!r} is missing required database variables: {missing!r}"
            )
        if forbidden_present:
            raise ValueError(
                f"process profile {profile_name!r} received forbidden database variables: "
                f"{forbidden_present!r}"
            )

        expected_worker_profile = profile.get("celery_worker_profile") or ""
        if self.CELERY_WORKER_PROFILE.strip().lower() != expected_worker_profile:
            raise ValueError(
                "CELERY_WORKER_PROFILE does not match the production process profile"
            )

        urls = {component: self._raw_runtime_value(component) for component in components}
        violations = validate_runtime_url_configuration(urls)
        if violations:
            detail = "; ".join(
                f"[{item.code}] {item.subject}: {item.message}" for item in violations
            )
            raise ValueError("Production database identity configuration is unsafe: " + detail)

        notification_mode = self.NOTIFICATION_EMAIL_PROVIDER_MODE.strip().lower()
        if notification_mode not in {"disabled", "resend"}:
            raise ValueError(
                "NOTIFICATION_EMAIL_PROVIDER_MODE must be disabled or resend"
            )
        notification_key = self.P4C_RESEND_API_KEY.strip()
        webhook_secret = self.RESEND_WEBHOOK_SECRET.strip()
        notification_metrics_endpoint = self.NOTIFICATION_METRICS_OTLP_ENDPOINT.strip()

        if profile_name == "worker":
            if webhook_secret:
                raise ValueError(
                    "RESEND_WEBHOOK_SECRET is restricted to the API profile"
                )
            if notification_mode == "disabled":
                if notification_key or notification_metrics_endpoint:
                    raise ValueError(
                        "disabled notification workers must not receive P4C provider or metrics configuration"
                    )
            else:
                if not notification_key:
                    raise ValueError(
                        "P4C_RESEND_API_KEY is required when Resend notifications are enabled"
                    )
                if not self.NOTIFICATION_EMAIL_FROM.strip():
                    raise ValueError(
                        "NOTIFICATION_EMAIL_FROM is required when Resend notifications are enabled"
                    )
                resend_url = urlparse(self.RESEND_API_BASE_URL.strip())
                if resend_url.scheme != "https" or not resend_url.netloc:
                    raise ValueError(
                        "RESEND_API_BASE_URL must be an HTTPS URL in production"
                    )
                if not 0 < self.NOTIFICATION_PROVIDER_TIMEOUT_SECONDS <= 60:
                    raise ValueError(
                        "NOTIFICATION_PROVIDER_TIMEOUT_SECONDS must be in the range (0, 60]"
                    )
                _validate_notification_metrics(
                    notification_metrics_endpoint,
                    self.NOTIFICATION_METRICS_EXPORT_INTERVAL_SECONDS,
                    self.NOTIFICATION_METRICS_EXPORT_TIMEOUT_SECONDS,
                )
        elif profile_name == "api":
            if notification_mode != "disabled" or notification_key or notification_metrics_endpoint:
                raise ValueError(
                    "P4C Resend sending and notification-metrics authority is restricted away from the API profile"
                )
        elif profile_name == "maintenance":
            if notification_mode != "disabled" or notification_key or webhook_secret:
                raise ValueError(
                    "P4C notification sending/webhook credentials are forbidden for maintenance"
                )
            if notification_metrics_endpoint:
                _validate_notification_metrics(
                    notification_metrics_endpoint,
                    self.NOTIFICATION_METRICS_EXPORT_INTERVAL_SECONDS,
                    self.NOTIFICATION_METRICS_EXPORT_TIMEOUT_SECONDS,
                )
        else:
            if notification_mode != "disabled" or notification_key or webhook_secret or notification_metrics_endpoint:
                raise ValueError(
                    "P4C notification provider/webhook/metrics configuration is forbidden for beat profiles"
                )

        search_mode = self.SEARCH_PROVIDER_MODE.strip().lower()
        if search_mode not in {"disabled", "opensearch"}:
            raise ValueError("SEARCH_PROVIDER_MODE must be disabled or opensearch")

        search_runtime_values = (
            self.OPENSEARCH_URL.strip(),
            self.OPENSEARCH_USERNAME.strip(),
            self.OPENSEARCH_PASSWORD.strip(),
            self.SEARCH_METRICS_OTLP_ENDPOINT.strip(),
        )
        if profile_name != "worker":
            if search_mode != "disabled" or any(search_runtime_values):
                raise ValueError(
                    "OpenSearch and search-metrics configuration is restricted to the worker profile"
                )
            return self

        if search_mode == "disabled":
            if any(search_runtime_values):
                raise ValueError(
                    "disabled production search workers must not receive provider or metrics endpoints/credentials"
                )
            return self

        provider_url = urlparse(self.OPENSEARCH_URL.strip())
        if provider_url.scheme not in {"http", "https"} or not provider_url.netloc:
            raise ValueError("OPENSEARCH_URL must be an HTTP(S) URL in production")
        if not self.OPENSEARCH_INDEX.strip():
            raise ValueError("OPENSEARCH_INDEX is required when OpenSearch is enabled")
        if bool(self.OPENSEARCH_USERNAME.strip()) != bool(self.OPENSEARCH_PASSWORD.strip()):
            raise ValueError("OpenSearch basic auth requires both username and password")
        if self.OPENSEARCH_TIMEOUT_SECONDS <= 0 or self.OPENSEARCH_TIMEOUT_SECONDS > 60:
            raise ValueError("OPENSEARCH_TIMEOUT_SECONDS must be in the range (0, 60]")

        metrics_url = urlparse(self.SEARCH_METRICS_OTLP_ENDPOINT.strip())
        if metrics_url.scheme not in {"http", "https"} or not metrics_url.netloc:
            raise ValueError(
                "SEARCH_METRICS_OTLP_ENDPOINT is required for production OpenSearch workers"
            )
        if not 1 <= self.SEARCH_METRICS_EXPORT_INTERVAL_SECONDS <= 300:
            raise ValueError(
                "SEARCH_METRICS_EXPORT_INTERVAL_SECONDS must be in the range [1, 300]"
            )
        if not 0 < self.SEARCH_METRICS_EXPORT_TIMEOUT_SECONDS <= 60:
            raise ValueError(
                "SEARCH_METRICS_EXPORT_TIMEOUT_SECONDS must be in the range (0, 60]"
            )
        return self

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    @property
    def process_profile(self) -> str:
        return self.DOERS_PROCESS_PROFILE.strip().lower()

    def database_component_enabled(self, component: str) -> bool:
        if component not in {"api", "auth", "worker", "maintenance"}:
            raise ValueError(f"unknown database component: {component!r}")
        if not self.is_production:
            return True
        profile = _process_manifest()["profiles"].get(self.process_profile)
        if profile is None:
            return False
        return component in set(profile.get("runtime_components", ()))

    def _exposed_runtime_value(self, component: str, *, allow_empty: bool = False) -> str:
        raw = self._raw_runtime_value(component)
        if not self.is_production:
            return raw
        if self.database_component_enabled(component):
            return raw
        return "" if allow_empty else _DISABLED_ASYNC_URL

    @property
    def DATABASE_URL(self) -> str:
        return self._exposed_runtime_value("api")

    @property
    def AUTH_DATABASE_URL(self) -> str:
        return self._exposed_runtime_value("auth", allow_empty=True)

    @property
    def WORKER_DATABASE_URL(self) -> str:
        return self._exposed_runtime_value("worker", allow_empty=True)

    @property
    def MAINTENANCE_DATABASE_URL(self) -> str:
        return self._exposed_runtime_value("maintenance", allow_empty=True)

    @property
    def worker_database_url(self) -> str:
        raw = self._raw_runtime_value("worker")
        if self.is_production:
            return raw if self.database_component_enabled("worker") else _DISABLED_ASYNC_URL
        return raw or self._raw_runtime_value("api")

    @property
    def maintenance_database_url(self) -> str:
        raw = self._raw_runtime_value("maintenance")
        if self.is_production:
            return raw if self.database_component_enabled("maintenance") else _DISABLED_ASYNC_URL
        return raw or self._raw_runtime_value("api")

    @property
    def celery_worker_profile(self) -> str:
        return self.CELERY_WORKER_PROFILE.strip().lower()

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"


settings = Settings()