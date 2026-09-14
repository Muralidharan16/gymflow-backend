from __future__ import annotations

import os
import secrets
import signal
import subprocess
import time
from pathlib import Path

import pytest
import redis


ROOT = Path(__file__).resolve().parents[2]
DATABASE = "gymflow_p6s_test"


def _run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    subprocess.run(command, cwd=ROOT, env=env, check=True, text=True)


def _psql(sql: str) -> None:
    subprocess.run(
        ["sudo", "-u", "postgres", "psql", "-X", "-v", "ON_ERROR_STOP=1", "-d", "postgres"],
        cwd=ROOT,
        input=sql,
        check=True,
        text=True,
    )


def _wait_redis(url: str, *, replica: bool = False) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        client = redis.Redis.from_url(url, decode_responses=True, socket_timeout=1)
        try:
            if client.ping() is True:
                if not replica:
                    return
                info = client.info("replication")
                if info.get("role") == "slave" and info.get("master_link_status") == "up":
                    return
        except (redis.RedisError, OSError):
            pass
        finally:
            client.close()
        time.sleep(0.2)
    raise RuntimeError("P6-S Redis runtime did not become ready")


@pytest.fixture(scope="session", autouse=True)
def p6s_runtime_environment(tmp_path_factory: pytest.TempPathFactory):
    base = tmp_path_factory.mktemp("p6s-runtime")
    certs = base / "tls"
    primary_dir = base / "primary"
    replica_dir = base / "replica"
    certs.mkdir()
    primary_dir.mkdir()
    replica_dir.mkdir()

    _run(["sudo", "sysctl", "-w", "vm.overcommit_memory=1"])

    _run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-days", "1", "-nodes",
            "-keyout", str(certs / "ca.key"), "-out", str(certs / "ca.crt"),
            "-subj", "/CN=DOERS P6S CI CA",
        ]
    )
    _run(
        [
            "openssl", "req", "-newkey", "rsa:2048", "-sha256", "-nodes",
            "-keyout", str(certs / "server.key"), "-out", str(certs / "server.csr"),
            "-subj", "/CN=localhost",
        ]
    )
    ext = certs / "server.ext"
    ext.write_text(
        "basicConstraints=CA:FALSE\n"
        "keyUsage=digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n"
        "subjectAltName=DNS:localhost,IP:127.0.0.1\n",
        encoding="utf-8",
    )
    _run(
        [
            "openssl", "x509", "-req", "-sha256", "-days", "1",
            "-in", str(certs / "server.csr"), "-CA", str(certs / "ca.crt"),
            "-CAkey", str(certs / "ca.key"), "-CAcreateserial",
            "-out", str(certs / "server.crt"), "-extfile", str(ext),
        ]
    )

    redis_password = secrets.token_hex(24)
    primary_conf = base / "primary.conf"
    replica_conf = base / "replica.conf"
    common = (
        "port 0\n"
        "tls-cert-file {cert}\n"
        "tls-key-file {key}\n"
        "tls-ca-cert-file {ca}\n"
        "tls-auth-clients no\n"
        "requirepass {password}\n"
        "appendonly yes\n"
        "appendfsync everysec\n"
        "save 60 1\n"
        "maxmemory 128mb\n"
        "maxmemory-policy noeviction\n"
    ).format(
        cert=certs / "server.crt",
        key=certs / "server.key",
        ca=certs / "ca.crt",
        password=redis_password,
    )
    primary_conf.write_text(
        common + f"tls-port 16382\ndir {primary_dir}\n",
        encoding="utf-8",
    )
    replica_conf.write_text(
        common
        + f"tls-port 16383\ndir {replica_dir}\n"
        + f"masterauth {redis_password}\n"
        + "replicaof 127.0.0.1 16382\n"
        + "tls-replication yes\n",
        encoding="utf-8",
    )

    primary = subprocess.Popen(["redis-server", str(primary_conf)], cwd=ROOT)
    replica = None
    try:
        query = f"ssl_cert_reqs=required&ssl_ca_certs={certs / 'ca.crt'}"
        base_url = f"rediss://:{redis_password}@localhost"
        primary_ping = f"{base_url}:16382/0?{query}"
        _wait_redis(primary_ping)
        replica = subprocess.Popen(["redis-server", str(replica_conf)], cwd=ROOT)
        replica_ping = f"{base_url}:16383/0?{query}"
        _wait_redis(replica_ping, replica=True)

        os.environ.update(
            {
                "REDIS_URL": primary_ping,
                "CELERY_BROKER_URL": f"{base_url}:16382/1?{query}",
                "CELERY_RESULT_BACKEND": f"{base_url}:16382/2?{query}",
                "REDIS_PRODUCTION_TOPOLOGY": "self_managed_ha",
                "REDIS_PERSISTENCE_MODE": "aof_everysec_rdb",
                "REDIS_MAXMEMORY_POLICY": "noeviction",
                "REDIS_HA_MIN_REPLICAS": "1",
                "REDIS_VM_OVERCOMMIT_MEMORY": "1",
                "REDIS_MANAGED_PROVIDER_ATTESTED": "false",
                "CELERY_BEAT_OWNERSHIP_KEY": "doers:p6s:ci:beat-owner:v1",
                "CELERY_BEAT_OWNERSHIP_TTL_SECONDS": "6",
                "CELERY_BEAT_OWNERSHIP_RETRY_SECONDS": "0.5",
                "P6S_SCHEDULER_FAULTS": "1",
                "P6S_DISPOSABLE_DATABASE": DATABASE,
                "P6S_REDIS_PASSWORD": redis_password,
                "SECRET_KEY": "p6s-ci-not-production",
                "AWS_ACCESS_KEY_ID": "p6s-test",
                "AWS_SECRET_ACCESS_KEY": "p6s-test",
                "S3_BUCKET_NAME": "gymflow-p6s-test",
            }
        )

        _run(["bash", "scripts/ci/bootstrap_cluster_roles.sh"])
        migration_password = secrets.token_hex(16)
        app_password = secrets.token_hex(16)
        worker_password = secrets.token_hex(16)
        _psql(
            f"""
            ALTER ROLE migration_owner PASSWORD '{migration_password}';
            CREATE ROLE app_p6s_runtime LOGIN PASSWORD '{app_password}'
              NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
            GRANT app_runtime TO app_p6s_runtime WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;
            ALTER ROLE app_p6s_runtime SET row_security='on';
            ALTER ROLE app_p6s_runtime SET statement_timeout='15s';
            ALTER ROLE app_p6s_runtime SET lock_timeout='2s';
            CREATE ROLE worker_p6s_runtime LOGIN PASSWORD '{worker_password}'
              NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
            GRANT worker_runtime TO worker_p6s_runtime WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;
            ALTER ROLE worker_p6s_runtime SET row_security='on';
            ALTER ROLE worker_p6s_runtime SET statement_timeout='15s';
            ALTER ROLE worker_p6s_runtime SET lock_timeout='2s';
            CREATE DATABASE {DATABASE} OWNER migration_owner;
            REVOKE ALL ON DATABASE {DATABASE} FROM PUBLIC;
            GRANT CONNECT ON DATABASE {DATABASE} TO migration_owner,app_p6s_runtime,worker_p6s_runtime;
            """
        )

        os.environ.update(
            {
                "DATABASE_URL": f"postgresql+asyncpg://migration_owner:{migration_password}@127.0.0.1:5432/{DATABASE}",
                "TEST_ADMIN_DATABASE_URL": f"postgresql://migration_owner:{migration_password}@127.0.0.1:5432/{DATABASE}",
                "P6S_APP_DATABASE_URL": f"postgresql://app_p6s_runtime:{app_password}@127.0.0.1:5432/{DATABASE}",
                "WORKER_DATABASE_URL": f"postgresql+asyncpg://worker_p6s_runtime:{worker_password}@127.0.0.1:5432/{DATABASE}",
            }
        )

        _run(["bash", "scripts/ci/provision_infrastructure_extensions.sh", DATABASE])
        _run([sys.executable, "-s", "scripts/verify_alembic_graph.py"])
        _run([sys.executable, "-s", "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"], env=os.environ.copy())
        _run([sys.executable, "-s", "-m", "alembic", "-c", "alembic.ini", "current", "--check-heads"], env=os.environ.copy())
        _run(
            [
                sys.executable,
                "scripts/verify_redis_production_readiness.py",
                "--host", "localhost",
                "--port", "16382",
                "--ca-cert", str(certs / "ca.crt"),
                "--password-env", "P6S_REDIS_PASSWORD",
                "--expected-replicas", "1",
                "--require-local-overcommit",
            ],
            env=os.environ.copy(),
        )
        print("P6S_DISPOSABLE_RUNTIME_READY=PASS")
        yield
    finally:
        for process in (replica, primary):
            if process is not None and process.poll() is None:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
