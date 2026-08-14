from dataclasses import replace

from app.core.runtime_binding_alignment import validate_runtime_binding_cluster_setting_alignment
from app.core.runtime_principal_attestation import load_runtime_binding_contract


def test_canonical_alignment_passes() -> None:
    assert validate_runtime_binding_cluster_setting_alignment() == ()


def test_auth_settings_follow_dedicated_auth_runtime_defaults() -> None:
    contract = load_runtime_binding_contract()
    auth = contract.bindings["auth"]
    assert auth.runtime_capability == "auth_runtime"
    assert auth.direct_capabilities == ("auth_runtime", "app_user")
    assert "app_runtime" not in auth.direct_capabilities
    assert auth.session_settings["statement_timeout"] == "5s"
    assert auth.session_settings["lock_timeout"] == "2s"
    assert auth.session_settings["row_security"] == "on"


def test_inherited_setting_misalignment_is_rejected() -> None:
    contract = load_runtime_binding_contract()
    api = contract.bindings["api"]
    settings = dict(api.session_settings)
    settings["statement_timeout"] = "6s"
    bindings = dict(contract.bindings)
    bindings["api"] = replace(api, session_settings=settings)
    drifted = replace(contract, bindings=bindings)
    codes = {item.code for item in validate_runtime_binding_cluster_setting_alignment(drifted)}
    assert "runtime.cluster_setting_alignment" in codes
