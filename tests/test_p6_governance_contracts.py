from __future__ import annotations

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "docs/architecture/p6_production_readiness_matrix.json"
SCOPE_PATH = ROOT / "docs/architecture/P6_CELERY_REDIS_SCHEDULER_SCOPE_AND_GATES.md"
ACCEPTANCE_PATH = ROOT / "docs/architecture/P6_ACCEPTANCE_MATRIX.md"
WORKFLOW_PATH = ROOT / ".github/workflows/p6-governance-production-readiness.yml"
CELERY_PATH = ROOT / "app/core/celery_app.py"
PROD_IDENTITIES_PATH = ROOT / "deploy/docker-compose.production-identities.yml"
LOCK_PATH = ROOT / "requirements-test.lock"

P5_MERGE_BASE = "52949397fc3f7d4822e0c31c5ee76a21f2c182fb"
P5_CERTIFIED_TREE = "388a89ef09daff7c719ae07ebc5d22d152af4c58"
P6_BRANCH = "hardening/p6-celery-redis-scheduler-production-readiness"

EXPECTED_SCENARIOS = {
    "redis_production_configuration_drift": "P6-R",
    "broker_restart_and_reconnect": "P6-B",
    "graceful_worker_sigterm": "P6-W",
    "late_ack_redelivery": "P6-W",
    "poison_message_containment": "P6-P",
    "beat_single_owner": "P6-S",
    "duplicate_scheduler_publication": "P6-S",
}

EXPECTED_FINAL_MARKERS = {
    "P6_REDIS_PRODUCTION_CONTRACT=PASS",
    "P6_BROKER_RECONNECT_RECOVERY=PASS",
    "P6_WORKER_SIGTERM_SAFE=PASS",
    "P6_LATE_ACK_REDELIVERY_SAFE=PASS",
    "P6_POISON_MESSAGE_CONTAINED=PASS",
    "P6_BEAT_SINGLE_OWNER=PASS",
    "P6_DUPLICATE_SCHEDULER_EFFECT_SAFE=PASS",
    "P6_NO_LOST_DURABLE_BUSINESS_WORK=PASS",
    "P6_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED",
}


def _matrix() -> dict:
    return json.loads(MATRIX_PATH.read_text(encoding="utf-8"))


def _workflow() -> dict:
    return yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_p6_matrix_binds_exact_merged_p5_baseline_and_branch() -> None:
    matrix = _matrix()
    assert matrix["schema_version"] == 1
    assert matrix["governance_stage"] == "P6-G"
    assert matrix["p5_merge_base_sha"] == P5_MERGE_BASE
    assert matrix["p5_certified_tree"] == P5_CERTIFIED_TREE
    assert matrix["candidate_branch"] == P6_BRANCH
    assert matrix["locked_dependencies"] == {
        "celery": "5.6.3",
        "redis_py": "6.4.0",
    }


def test_p6_matrix_freezes_exact_hard_gate_and_seven_fault_classes() -> None:
    matrix = _matrix()
    assert matrix["hard_gate"] == ["no_lost_durable_business_work"]
    assert {
        entry["id"]: entry["slice"] for entry in matrix["fault_scenarios"]
    } == EXPECTED_SCENARIOS
    assert len(matrix["fault_scenarios"]) == 7
    assert set(matrix["required_final_markers"]) == EXPECTED_FINAL_MARKERS

    for scenario in matrix["fault_scenarios"]:
        assert scenario["p6_certification"] == "not_yet_certified"
        assert scenario["injection_point"]
        assert scenario["expected_result"]
        assert scenario["required_proof"]


def test_p6_redis_contract_is_fail_closed_and_never_business_authority() -> None:
    contract = _matrix()["redis_production_contract"]
    persistence = contract["persistence"]
    security = contract["transport_security"]
    availability = contract["availability"]
    memory = contract["memory"]
    host = contract["host"]

    assert persistence == {
        "required": True,
        "self_managed_target": "aof_appendfsync_everysec_plus_periodic_rdb",
        "durable_storage_required": True,
        "managed_equivalent_allowed_with_attestation": True,
        "business_authority": False,
    }
    assert security["authentication_required"] is True
    assert security["tls_required"] is True
    assert security["certificate_identity_verification_required"] is True
    assert security["plain_redis_scheme_in_production"] == "forbidden"
    assert availability["single_unreplicated_instance_as_production_ha"] == "forbidden"
    assert availability["supported_model_must_be_declared"] is True
    assert availability["failover_reconnect_must_be_tested"] is True
    assert memory == {
        "maxmemory_policy": "noeviction",
        "silent_broker_key_eviction": "forbidden",
    }
    assert host["self_managed_linux_vm_overcommit_memory"] == 1
    assert host["managed_provider_equivalent_must_be_declared"] is True


def test_p6_inherits_p5_late_ack_and_authority_baseline() -> None:
    matrix = _matrix()
    inherited = matrix["inherited_boundaries"]
    celery_source = CELERY_PATH.read_text(encoding="utf-8")

    assert inherited["postgresql_is_durable_business_authority"] is True
    assert inherited["redis_or_celery_as_business_authority"] == "forbidden"
    assert inherited["task_acks_late"] is True
    assert inherited["task_reject_on_worker_lost"] is True
    assert inherited["worker_prefetch_multiplier"] == 1
    assert inherited["provider_refund_execution"] == "deferred_fail_closed"

    assert "task_acks_late=True" in celery_source
    assert "task_reject_on_worker_lost=True" in celery_source
    assert "worker_prefetch_multiplier=1" in celery_source


def test_p6_keeps_beat_without_database_credentials_and_forbids_privilege_widening() -> None:
    identities = yaml.load(
        PROD_IDENTITIES_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader
    )
    beat_env = identities["services"]["celery-beat"]["environment"]

    for key in (
        "DATABASE_URL",
        "AUTH_DATABASE_URL",
        "WORKER_DATABASE_URL",
        "MAINTENANCE_DATABASE_URL",
        "FINANCE_CONFIG_DATABASE_URL",
    ):
        assert beat_env[key] == ""

    scope = SCOPE_PATH.read_text(encoding="utf-8")
    assert "broadening Beat or worker database credentials" in scope
    assert "Embedded Beat" in scope
    assert "Redis ownership can never become business authority" in scope


def test_p6_dependency_and_acceptance_markers_are_locked() -> None:
    lock = LOCK_PATH.read_text(encoding="utf-8")
    acceptance = ACCEPTANCE_PATH.read_text(encoding="utf-8")
    scope = SCOPE_PATH.read_text(encoding="utf-8")

    assert "celery==5.6.3" in lock
    assert "redis==6.4.0" in lock
    for marker in EXPECTED_FINAL_MARKERS:
        assert marker in acceptance

    for phrase in (
        "PostgreSQL remains the authority for durable business work",
        "vm.overcommit_memory=1",
        "broker Redis memory policy must be `noeviction`",
        "SIGTERM",
        "Poison-message contract",
        "Celery Beat single-owner",
        "at-least-once delivery",
    ):
        assert phrase in scope


def test_p6_decisive_evidence_forbids_mock_only_production_fault_proof() -> None:
    requirements = _matrix()["decisive_runtime_requirements"]
    assert requirements == {
        "same_immutable_sha": True,
        "source_mutation_during_certification": "forbidden",
        "real_redis_required_for_broker_worker_scheduler_faults": True,
        "postgresql_major_for_durable_state_tests": 16,
        "production_equivalent_runtime_identities": True,
        "real_worker_process_required": True,
        "authoritative_durable_postcondition_verification": True,
        "mock_only_restart_reconnect_sigterm_redelivery_scheduler_evidence": "forbidden",
    }

    assert set(_matrix()["forbidden_actions"]) == {
        "merge",
        "retarget",
        "tag",
        "release",
        "deploy",
        "live_refund_provider_activation",
        "live_money_movement",
    }


def test_p6_governance_workflow_is_exact_base_and_branch_bound() -> None:
    workflow = _workflow()
    source = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["on"]["push"]["branches"] == [P6_BRANCH]
    assert "workflow_call" in workflow["on"]
    assert set(workflow["jobs"]) == {"governance"}

    job = workflow["jobs"]["governance"]
    assert job["env"]["P5_MERGE_BASE"] == P5_MERGE_BASE
    assert job["env"]["P5_CERTIFIED_TREE"] == P5_CERTIFIED_TREE
    assert "git merge-base --is-ancestor \"${P5_MERGE_BASE}\" HEAD" in source
    assert "tests/test_p6_governance_contracts.py" in source
    assert "tests/test_p5f_final_certification_contracts.py" in source
    assert "P6_GOVERNANCE_PRODUCTION_READINESS=PASS" in source
    assert "P6_NO_LOST_DURABLE_BUSINESS_WORK=GOVERNANCE_FROZEN" in source
    assert "P6_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED" in source
