#!/usr/bin/env python3
"""Verify the P6 self-managed production Redis contract against a real server.

The probe is deliberately read-only with respect to Redis configuration. It
verifies the running server and host preconditions; it does not make Redis a
source of business authority.
"""

from __future__ import annotations

import argparse
import os
import ssl
import sys
import time
from pathlib import Path

import redis


class ReadinessError(RuntimeError):
    pass


def _one(config: dict[str, object], key: str) -> str:
    value = config.get(key)
    if value is None:
        raise ReadinessError(f"Redis CONFIG GET did not return {key!r}")
    return str(value).strip().lower()


def _connect(args: argparse.Namespace) -> redis.Redis:
    password = os.environ.get(args.password_env, "")
    if not password:
        raise ReadinessError(
            f"environment variable {args.password_env!r} must contain the Redis password"
        )
    ca = Path(args.ca_cert)
    if not ca.is_file():
        raise ReadinessError(f"CA certificate does not exist: {ca}")

    return redis.Redis(
        host=args.host,
        port=args.port,
        password=password,
        ssl=True,
        ssl_cert_reqs=ssl.CERT_REQUIRED,
        ssl_ca_certs=str(ca),
        socket_connect_timeout=args.timeout,
        socket_timeout=args.timeout,
        decode_responses=True,
    )


def _verify_server(client: redis.Redis, *, expected_replicas: int) -> None:
    if client.ping() is not True:
        raise ReadinessError("Redis PING did not return success")

    config = client.config_get("*")
    persistence_info = client.info("persistence")
    replication_info = client.info("replication")

    if _one(config, "appendonly") != "yes":
        raise ReadinessError("Redis appendonly must be yes")
    if _one(config, "appendfsync") != "everysec":
        raise ReadinessError("Redis appendfsync must be everysec")
    if not _one(config, "save"):
        raise ReadinessError("Redis periodic RDB save policy must be configured")
    if _one(config, "maxmemory-policy") != "noeviction":
        raise ReadinessError("Redis maxmemory-policy must be noeviction")
    if int(persistence_info.get("aof_enabled", 0)) != 1:
        raise ReadinessError("Redis INFO persistence does not report AOF enabled")

    role = str(replication_info.get("role", "")).strip().lower()
    connected = int(replication_info.get("connected_slaves", 0))
    if role != "master":
        raise ReadinessError(f"readiness endpoint must be the primary/master, got {role!r}")
    if connected < expected_replicas:
        raise ReadinessError(
            f"Redis connected replicas {connected} < required {expected_replicas}"
        )


def _wait_for_contract(
    client: redis.Redis, *, expected_replicas: int, timeout: float
) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            _verify_server(client, expected_replicas=expected_replicas)
            return
        except (ReadinessError, redis.RedisError, OSError) as exc:
            last_error = exc
        time.sleep(0.25)
    raise ReadinessError(
        f"Redis did not satisfy the production contract within {timeout}s: {last_error!r}"
    )


def verify(args: argparse.Namespace) -> None:
    client = _connect(args)
    _wait_for_contract(
        client,
        expected_replicas=args.expected_replicas,
        timeout=args.ready_timeout,
    )

    if args.require_local_overcommit:
        overcommit_path = Path("/proc/sys/vm/overcommit_memory")
        if not overcommit_path.is_file():
            raise ReadinessError("vm.overcommit_memory cannot be inspected on this host")
        value = overcommit_path.read_text(encoding="utf-8").strip()
        if value != "1":
            raise ReadinessError(
                f"vm.overcommit_memory must be 1 for self-managed Redis, got {value!r}"
            )

    print("P6R_REAL_REDIS_PRODUCTION_PREFLIGHT=PASS")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--ca-cert", required=True)
    parser.add_argument("--password-env", default="P6R_REDIS_PASSWORD")
    parser.add_argument("--expected-replicas", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--ready-timeout", type=float, default=30.0)
    parser.add_argument("--require-local-overcommit", action="store_true")
    return parser


def main() -> int:
    try:
        args = _parser().parse_args()
        if args.expected_replicas < 1:
            raise ReadinessError("--expected-replicas must be at least 1")
        verify(args)
    except (ReadinessError, redis.RedisError, OSError, ValueError) as exc:
        print(f"P6R readiness failure: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
