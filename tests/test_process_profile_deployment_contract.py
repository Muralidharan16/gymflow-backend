from __future__ import annotations

import json
from pathlib import Path

from app.core.runtime_principal_attestation import load_runtime_binding_contract


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "security/runtime_identity/process_profiles.v1.json"
PRODUCTION_OVERLAY = ROOT / "deploy/docker-compose.production-identities.yml"


def test_process_profile_manifest_is_closed_against_p2d_runtime_bindings() -> None:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    runtime = load_runtime_binding_contract()

    governed = set(data["database_environment_variables"])
    assert governed == {
        binding.environment_variable for binding in runtime.bindings.values()
    }
    assert set(data["profiles"]) == {"api", "worker", "maintenance", "beat"}

    for profile_name, profile in data["profiles"].items():
        components = tuple(profile["runtime_components"])
        required = set(profile["required_database_variables"])
        forbidden = set(profile["forbidden_database_variables"])
        assert required == {
            runtime.bindings[component].environment_variable
            for component in components
        }
        assert not required & forbidden
        assert required | forbidden == governed
        expected_worker_profile = (
            profile_name if profile_name in {"worker", "maintenance"} else None
        )
        assert profile["celery_worker_profile"] == expected_worker_profile


def test_production_compose_overlay_compartmentalizes_database_inputs() -> None:
    text = PRODUCTION_OVERLAY.read_text(encoding="utf-8")

    api = text.split("  api:\n", 1)[1].split("\n  celery-worker:\n", 1)[0]
    worker = text.split("  celery-worker:\n", 1)[1].split(
        "\n  celery-maintenance-worker:\n", 1
    )[0]
    maintenance = text.split("  celery-maintenance-worker:\n", 1)[1].split(
        "\n  celery-beat:\n", 1
    )[0]
    beat = text.split("  celery-beat:\n", 1)[1].split("\n  flower:\n", 1)[0]
    flower = text.split("  flower:\n", 1)[1]

    assert "DOERS_PROCESS_PROFILE: api" in api
    assert "DOERS_PROCESS_PROFILE: worker" in worker
    assert "DOERS_PROCESS_PROFILE: maintenance" in maintenance
    assert "DOERS_PROCESS_PROFILE: beat" in beat
    assert "DOERS_PROCESS_PROFILE: beat" in flower

    assert 'WORKER_DATABASE_URL: ""' in api
    assert 'MAINTENANCE_DATABASE_URL: ""' in api
    assert 'DATABASE_URL: ""' in worker
    assert 'AUTH_DATABASE_URL: ""' in worker
    assert 'MAINTENANCE_DATABASE_URL: ""' in worker
    assert 'DATABASE_URL: ""' in maintenance
    assert 'AUTH_DATABASE_URL: ""' in maintenance
    assert 'WORKER_DATABASE_URL: ""' in maintenance
    for control_process in (beat, flower):
        for variable in (
            "DATABASE_URL",
            "AUTH_DATABASE_URL",
            "WORKER_DATABASE_URL",
            "MAINTENANCE_DATABASE_URL",
        ):
            assert f'{variable}: ""' in control_process
