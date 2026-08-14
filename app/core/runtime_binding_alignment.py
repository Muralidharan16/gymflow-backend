"""P2D compatibility proof between runtime LOGIN defaults and P2B role settings."""

from __future__ import annotations

from app.core.cluster_role_contract import (
    ContractBundle,
    ContractViolation,
    load_contract_bundle,
)
from app.core.runtime_principal_attestation import (
    RuntimeBindingContract,
    load_runtime_binding_contract,
)


def _normalize(value: object) -> str:
    return str(value).strip().lower()


def validate_runtime_binding_cluster_setting_alignment(
    contract: RuntimeBindingContract | None = None,
    bundle: ContractBundle | None = None,
) -> tuple[ContractViolation, ...]:
    """Require LOGIN defaults to preserve every directly inherited P2B default.

    P2D deployment LOGINs can add stricter operational defaults, but they may
    not contradict a setting owned by any directly inherited capability role.
    P3A keeps auth isolated from app_runtime, so auth LOGIN settings are checked
    only against its direct auth_runtime/app_user capabilities; those capability
    roles intentionally own no cluster defaults today. API, worker, and lifecycle
    maintenance bindings continue to preserve the defaults owned by their direct
    runtime capability roles.
    """

    runtime_contract = contract or load_runtime_binding_contract()
    cluster_contract = bundle or load_contract_bundle()
    settings_by_role = cluster_contract.role_settings.get("settings_by_role", {})
    violations: list[ContractViolation] = []

    for component, binding in sorted(runtime_contract.bindings.items()):
        required: dict[str, tuple[str, str]] = {}
        for role in binding.direct_capabilities:
            role_settings = settings_by_role.get(role, {})
            if not isinstance(role_settings, dict):
                violations.append(
                    ContractViolation(
                        code="runtime.cluster_setting_contract",
                        subject=f"{component}:{role}",
                        message="P2B settings_by_role entry must be an object.",
                    )
                )
                continue

            for name, raw_value in role_settings.items():
                value = _normalize(raw_value)
                previous = required.get(name)
                if previous is not None and previous[0] != value:
                    violations.append(
                        ContractViolation(
                            code="runtime.cluster_setting_conflict",
                            subject=f"{component}:{name}",
                            message=(
                                f"Inherited capabilities {previous[1]} and {role} "
                                f"declare conflicting P2B defaults {previous[0]!r} "
                                f"and {value!r}."
                            ),
                        )
                    )
                    continue
                required[name] = (value, role)

        for name, (expected, source_role) in sorted(required.items()):
            actual = binding.session_settings.get(name)
            if actual is None or _normalize(actual) != expected:
                violations.append(
                    ContractViolation(
                        code="runtime.cluster_setting_alignment",
                        subject=f"{component}:{name}",
                        message=(
                            f"Runtime LOGIN setting must preserve P2B {source_role} "
                            f"default {expected!r}; found {actual!r}."
                        ),
                    )
                )

    return tuple(violations)
