from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from app.core.redis_production_readiness import (
    RedisProductionContractError,
    validate_redis_production_settings,
)


ROOT = Path(__file__).resolve().parents[1]
TLS_URL = "rediss://default:secret@redis.example.test:6379/1?ssl_cert_reqs=required"


def _config(**overrides):
    values = {
        "ENVIRONMENT": "production",
        "REDIS_URL": TLS_URL.replace("/1?", "/0?"),
        "CELERY_BROKER_URL": TLS_URL,
        "CELERY_RESULT_BACKEND": TLS_URL.replace("/1?", "/2?"),
        "REDIS_PRODUCTION_TOPOLOGY": "self_managed_ha",
        "REDIS_PERSISTENCE_MODE": "aof_everysec_rdb",
        "REDIS_MAXMEMORY_POLICY": "noeviction",
        "REDIS_HA_MIN_REPLICAS": 1,
        "REDIS_VM_OVERCOMMIT_MEMORY": 1,
        "REDIS_MANAGED_PROVIDER_ATTESTED": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_self_managed_production_contract_accepts_verified_tls_aof_ha() -> None:
    contract = validate_redis_production_settings(_config())
    assert contract is not None
    assert contract.topology == "self_managed_ha"
    assert contract.persistence_mode == "aof_everysec_rdb"
    assert contract.maxmemory_policy == "noeviction"
    assert contract.ha_min_replicas == 1
    assert contract.vm_overcommit_memory == 1
    assert contract.managed_provider_attested is False


def test_managed_ha_contract_requires_explicit_provider_attestation() -> None:
    contract = validate_redis_production_settings(
        _config(
            REDIS_PRODUCTION_TOPOLOGY="managed_ha",
            REDIS_PERSISTENCE_MODE="managed_durable",
            REDIS_VM_OVERCOMMIT_MEMORY=-1,
            REDIS_MANAGED_PROVIDER_ATTESTED=True,
        )
    )
    assert contract is not None
    assert contract.topology == "managed_ha"
    assert contract.managed_provider_attested is True


@pytest.mark.parametrize("field", ["REDIS_URL", "CELERY_BROKER_URL", "CELERY_RESULT_BACKEND"])
def test_production_rejects_plain_redis_transport(field: str) -> None:
    value = TLS_URL.replace("rediss://", "redis://")
    with pytest.raises(RedisProductionContractError, match="rediss://"):
        validate_redis_production_settings(_config(**{field: value}))


def test_production_rejects_missing_redis_authentication() -> None:
    with pytest.raises(RedisProductionContractError, match="authenticated"):
        validate_redis_production_settings(
            _config(CELERY_BROKER_URL="rediss://redis.example.test:6379/1?ssl_cert_reqs=required")
        )


@pytest.mark.parametrize("cert_reqs", ["none", "optional", ""])
def test_production_rejects_unverified_tls(cert_reqs: str) -> None:
    url = f"rediss://default:secret@redis.example.test:6379/1?ssl_cert_reqs={cert_reqs}"
    with pytest.raises(RedisProductionContractError, match="ssl_cert_reqs=required"):
        validate_redis_production_settings(_config(CELERY_BROKER_URL=url))


def test_production_rejects_redis_eviction_policy() -> None:
    with pytest.raises(RedisProductionContractError, match="noeviction"):
        validate_redis_production_settings(_config(REDIS_MAXMEMORY_POLICY="allkeys-lru"))


def test_production_rejects_single_unreplicated_redis() -> None:
    with pytest.raises(RedisProductionContractError, match="at least 1"):
        validate_redis_production_settings(_config(REDIS_HA_MIN_REPLICAS=0))


def test_self_managed_rejects_wrong_persistence_mode() -> None:
    with pytest.raises(RedisProductionContractError, match="aof_everysec_rdb"):
        validate_redis_production_settings(_config(REDIS_PERSISTENCE_MODE="rdb_only"))


def test_self_managed_rejects_wrong_overcommit_contract() -> None:
    with pytest.raises(RedisProductionContractError, match="OVERCOMMIT_MEMORY=1"):
        validate_redis_production_settings(_config(REDIS_VM_OVERCOMMIT_MEMORY=0))


def test_managed_ha_rejects_missing_provider_attestation() -> None:
    with pytest.raises(RedisProductionContractError, match="ATTESTED=true"):
        validate_redis_production_settings(
            _config(
                REDIS_PRODUCTION_TOPOLOGY="managed_ha",
                REDIS_PERSISTENCE_MODE="managed_durable",
                REDIS_VM_OVERCOMMIT_MEMORY=-1,
                REDIS_MANAGED_PROVIDER_ATTESTED=False,
            )
        )


def test_nonproduction_remains_unchanged() -> None:
    config = _config(
        ENVIRONMENT="test",
        REDIS_URL="redis://localhost:6379/0",
        CELERY_BROKER_URL="redis://localhost:6379/1",
        CELERY_RESULT_BACKEND="redis://localhost:6379/2",
        REDIS_PRODUCTION_TOPOLOGY="",
        REDIS_PERSISTENCE_MODE="",
        REDIS_MAXMEMORY_POLICY="",
        REDIS_HA_MIN_REPLICAS=0,
        REDIS_VM_OVERCOMMIT_MEMORY=-1,
    )
    assert validate_redis_production_settings(config) is None


def test_celery_fail_closed_guard_precedes_broker_construction_and_keeps_p5_baseline() -> None:
    source = (ROOT / "app/core/celery_app.py").read_text(encoding="utf-8")
    guard = "validate_redis_production_settings(settings)"
    assert guard in source
    assert source.index(guard) < source.index("celery_app = Celery(")
    assert 'settings.process_profile in {"worker", "maintenance", "beat"}' in source
    assert "task_acks_late=True" in source
    assert "task_reject_on_worker_lost=True" in source
    assert "worker_prefetch_multiplier=1" in source


def test_production_identity_overlay_requires_redis_contract_without_beat_db_widening() -> None:
    path = ROOT / "deploy/docker-compose.production-identities.yml"
    source = path.read_text(encoding="utf-8")
    overlay = yaml.load(source, Loader=yaml.BaseLoader)
    required = {
        "REDIS_PRODUCTION_TOPOLOGY",
        "REDIS_PERSISTENCE_MODE",
        "REDIS_MAXMEMORY_POLICY",
        "REDIS_HA_MIN_REPLICAS",
        "REDIS_VM_OVERCOMMIT_MEMORY",
        "REDIS_MANAGED_PROVIDER_ATTESTED",
    }
    for service_name in (
        "api",
        "celery-worker",
        "celery-maintenance-worker",
        "celery-beat",
        "flower",
    ):
        env = overlay["services"][service_name]["environment"]
        assert required <= set(env)
        for key in required:
            assert ":?required}" in env[key]

    beat_env = overlay["services"]["celery-beat"]["environment"]
    for key in (
        "DATABASE_URL",
        "AUTH_DATABASE_URL",
        "WORKER_DATABASE_URL",
        "MAINTENANCE_DATABASE_URL",
        "FINANCE_CONFIG_DATABASE_URL",
    ):
        assert beat_env[key] == ""


def test_real_readiness_probe_is_tls_auth_persistence_ha_and_host_bound() -> None:
    source = (ROOT / "scripts/verify_redis_production_readiness.py").read_text(
        encoding="utf-8"
    )
    for phrase in (
        "ssl.CERT_REQUIRED",
        'client.config_get("*")',
        '"appendonly"',
        '"appendfsync"',
        '"maxmemory-policy"',
        'client.info("persistence")',
        'client.info("replication")',
        'Path("/proc/sys/vm/overcommit_memory")',
        "P6R_REAL_REDIS_PRODUCTION_PREFLIGHT=PASS",
    ):
        assert phrase in source
