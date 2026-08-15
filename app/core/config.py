"""Validated application settings and production process identity boundaries."""

from __future__ import annotations

import hmac
import ipaddress
import json
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import model_validator

from app.core.settings_schema import DoersSettingsSchema


_PROCESS_PROFILE_MANIFEST = (
    Path(__file__).resolve().parents[2]
    / "security"
    / "runtime_identity"
    / "process_profiles.v1.json"
)
_DISABLED_ASYNC_URL = "postgresql+asyncpg://disabled@invalid.invalid/doers_disabled"
_MIN_PRODUCTION_SECRET_LENGTH = 32


def _process_manifest() -> dict:
    raw = json.loads(_PROCESS_PROFILE_MANIFEST.read_text(encoding="utf-8"))
    if raw.get("schema_version") != 1 or not isinstance(raw.get("profiles"), dict):
        raise ValueError("unsupported P2E process profile manifest")
    return raw


def _validated_https_origin(
    name: str,
    value: str,
    *,
    allow_trailing_slash: bool,
) -> tuple[str, int]:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port or 443
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid HTTPS origin") from exc

    allowed_paths = {"", "/"} if allow_trailing_slash else {""}
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in allowed_paths
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{name} must be an HTTPS origin without credentials, query, or fragment")

    host = parsed.hostname.rstrip(".").lower()
    if host == "*":
        raise ValueError(f"{name} must use an explicit production host")
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError(f"{name} must not use localhost in production")

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (address.is_loopback or address.is_unspecified):
        raise ValueError(f"{name} must not use loopback or unspecified addresses in production")

    return host, port


def _validate_production_secret(name: str, value: str) -> str:
    normalized = str(value or "").strip()
    if len(normalized) < _MIN_PRODUCTION_SECRET_LENGTH:
        raise ValueError(f"{name} must be at least {_MIN_PRODUCTION_SECRET_LENGTH} characters in production")
    if normalized.lower().startswith("replace-with-"):
        raise ValueError(f"{name} still contains a development placeholder")
    return normalized


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
        return self

    @model_validator(mode="after")
    def validate_production_security_boundaries(self):
        if self.ENVIRONMENT != "production" or self.process_profile != "api":
            return self

        frontend_origin = _validated_https_origin(
            "FRONTEND_URL",
            self.FRONTEND_URL,
            allow_trailing_slash=True,
        )
        _validated_https_origin(
            "BACKEND_BASE_URL",
            self.BACKEND_BASE_URL,
            allow_trailing_slash=True,
        )

        cors_origins = self.cors_origins_list
        if not cors_origins:
            raise ValueError("CORS_ORIGINS must contain at least one explicit production origin")
        normalized_cors = {
            _validated_https_origin(
                "CORS_ORIGINS entry",
                origin,
                allow_trailing_slash=False,
            )
            for origin in cors_origins
        }
        if frontend_origin not in normalized_cors:
            raise ValueError("CORS_ORIGINS must explicitly include FRONTEND_URL in production")

        secret_key = _validate_production_secret("SECRET_KEY", self.SECRET_KEY)
        control_token = _validate_production_secret(
            "INTERNAL_CONTROL_TOKEN",
            self.INTERNAL_CONTROL_TOKEN,
        )
        if hmac.compare_digest(secret_key, control_token):
            raise ValueError("INTERNAL_CONTROL_TOKEN must be distinct from SECRET_KEY")
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
