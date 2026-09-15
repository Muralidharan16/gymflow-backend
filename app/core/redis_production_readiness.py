"""P6 fail-closed production Redis declaration validation.

Redis and Celery remain delivery/coordination infrastructure.  This module
validates the deployment contract only; it never turns broker state into
business authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse


class RedisProductionContractError(RuntimeError):
    """Raised when production Redis/Celery declarations are unsafe."""


@dataclass(frozen=True)
class RedisProductionContract:
    topology: str
    persistence_mode: str
    maxmemory_policy: str
    ha_min_replicas: int
    vm_overcommit_memory: int
    managed_provider_attested: bool


_REQUIRED_REDIS_URL_FIELDS = (
    "REDIS_URL",
    "CELERY_BROKER_URL",
    "CELERY_RESULT_BACKEND",
)
_SUPPORTED_TOPOLOGIES = {"self_managed_ha", "managed_ha"}


def _validate_verified_tls_url(name: str, value: str) -> None:
    parsed = urlparse(str(value or "").strip())
    if parsed.scheme.lower() != "rediss" or not parsed.hostname:
        raise RedisProductionContractError(
            f"{name} must use rediss:// with a concrete host in production"
        )
    if parsed.password is None or not parsed.password:
        raise RedisProductionContractError(
            f"{name} must carry authenticated Redis credentials in production"
        )
    query = parse_qs(parsed.query, keep_blank_values=True)
    cert_reqs = [item.strip().lower() for item in query.get("ssl_cert_reqs", [])]
    if cert_reqs != ["required"]:
        raise RedisProductionContractError(
            f"{name} must set ssl_cert_reqs=required in production"
        )


def validate_redis_production_settings(config: Any) -> RedisProductionContract | None:
    """Validate the P6 production declaration used by Celery and Beat.

    Non-production processes are deliberately unchanged.  Production worker,
    maintenance and Beat/Flower entrypoints call this before broker activity.
    The deployment readiness probe separately verifies real Redis server state.
    """

    environment = str(getattr(config, "ENVIRONMENT", "") or "").strip().lower()
    if environment != "production":
        return None

    for field in _REQUIRED_REDIS_URL_FIELDS:
        _validate_verified_tls_url(field, str(getattr(config, field, "") or ""))

    topology = str(
        getattr(config, "REDIS_PRODUCTION_TOPOLOGY", "") or ""
    ).strip().lower()
    if topology not in _SUPPORTED_TOPOLOGIES:
        raise RedisProductionContractError(
            "REDIS_PRODUCTION_TOPOLOGY must be self_managed_ha or managed_ha"
        )

    persistence_mode = str(
        getattr(config, "REDIS_PERSISTENCE_MODE", "") or ""
    ).strip().lower()
    maxmemory_policy = str(
        getattr(config, "REDIS_MAXMEMORY_POLICY", "") or ""
    ).strip().lower()
    ha_min_replicas = int(getattr(config, "REDIS_HA_MIN_REPLICAS", 0))
    vm_overcommit_memory = int(
        getattr(config, "REDIS_VM_OVERCOMMIT_MEMORY", -1)
    )
    managed_attested = bool(
        getattr(config, "REDIS_MANAGED_PROVIDER_ATTESTED", False)
    )

    if maxmemory_policy != "noeviction":
        raise RedisProductionContractError(
            "REDIS_MAXMEMORY_POLICY must be noeviction in production"
        )
    if ha_min_replicas < 1:
        raise RedisProductionContractError(
            "REDIS_HA_MIN_REPLICAS must be at least 1 in production"
        )

    if topology == "self_managed_ha":
        if persistence_mode != "aof_everysec_rdb":
            raise RedisProductionContractError(
                "self-managed production Redis requires REDIS_PERSISTENCE_MODE="
                "aof_everysec_rdb"
            )
        if vm_overcommit_memory != 1:
            raise RedisProductionContractError(
                "self-managed production Redis requires "
                "REDIS_VM_OVERCOMMIT_MEMORY=1"
            )
        if managed_attested:
            raise RedisProductionContractError(
                "self-managed Redis must not claim managed-provider attestation"
            )
    else:
        if persistence_mode not in {"managed_durable", "aof_everysec_rdb"}:
            raise RedisProductionContractError(
                "managed production Redis requires a durable persistence attestation"
            )
        if not managed_attested:
            raise RedisProductionContractError(
                "managed production Redis requires REDIS_MANAGED_PROVIDER_ATTESTED=true"
            )

    return RedisProductionContract(
        topology=topology,
        persistence_mode=persistence_mode,
        maxmemory_policy=maxmemory_policy,
        ha_min_replicas=ha_min_replicas,
        vm_overcommit_memory=vm_overcommit_memory,
        managed_provider_attested=managed_attested,
    )
