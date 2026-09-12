from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_IDENTITIES = ROOT / "deploy" / "docker-compose.production-identities.yml"
ENV_EXAMPLE = ROOT / ".env.example"


def _service_blocks(source: str) -> dict[str, str]:
    """Split the simple production identity overlay into top-level service blocks."""

    blocks: dict[str, list[str]] = {}
    current: str | None = None
    for line in source.splitlines():
        if line.startswith("  ") and not line.startswith("    ") and line.endswith(":"):
            current = line.strip()[:-1]
            blocks[current] = [line]
            continue
        if current is not None:
            blocks[current].append(line)
    return {name: "\n".join(lines) for name, lines in blocks.items()}


def test_only_ordinary_worker_receives_live_opensearch_configuration() -> None:
    source = PRODUCTION_IDENTITIES.read_text(encoding="utf-8")
    services = _service_blocks(source)

    assert set(services) >= {
        "api",
        "celery-worker",
        "celery-maintenance-worker",
        "celery-beat",
        "flower",
    }

    worker = services["celery-worker"]
    assert "DOERS_PROCESS_PROFILE: worker" in worker
    assert "SEARCH_PROVIDER_MODE: ${SEARCH_PROVIDER_MODE:-disabled}" in worker
    assert "OPENSEARCH_URL: ${OPENSEARCH_URL:-}" in worker
    assert "OPENSEARCH_USERNAME: ${OPENSEARCH_USERNAME:-}" in worker
    assert "OPENSEARCH_PASSWORD: ${OPENSEARCH_PASSWORD:-}" in worker
    assert "OPENSEARCH_VERIFY_TLS: ${OPENSEARCH_VERIFY_TLS:-true}" in worker
    assert "SEARCH_METRICS_OTLP_ENDPOINT: ${SEARCH_METRICS_OTLP_ENDPOINT:-}" in worker
    assert "SEARCH_METRICS_EXPORT_INTERVAL_SECONDS: ${SEARCH_METRICS_EXPORT_INTERVAL_SECONDS:-30}" in worker
    assert "SEARCH_METRICS_EXPORT_TIMEOUT_SECONDS: ${SEARCH_METRICS_EXPORT_TIMEOUT_SECONDS:-5}" in worker

    for service_name in (
        "api",
        "celery-maintenance-worker",
        "celery-beat",
        "flower",
    ):
        block = services[service_name]
        assert "SEARCH_PROVIDER_MODE: disabled" in block, service_name
        assert 'OPENSEARCH_URL: ""' in block, service_name
        assert 'OPENSEARCH_USERNAME: ""' in block, service_name
        assert 'OPENSEARCH_PASSWORD: ""' in block, service_name
        assert 'SEARCH_METRICS_OTLP_ENDPOINT: ""' in block, service_name
        assert "${OPENSEARCH_URL" not in block, service_name
        assert "${OPENSEARCH_USERNAME" not in block, service_name
        assert "${OPENSEARCH_PASSWORD" not in block, service_name
        assert "${SEARCH_METRICS_OTLP_ENDPOINT" not in block, service_name


def test_example_configuration_is_fail_closed_and_contains_no_provider_secret() -> None:
    source = ENV_EXAMPLE.read_text(encoding="utf-8")

    assert "SEARCH_PROVIDER_MODE=disabled" in source
    assert "OPENSEARCH_URL=\n" in source
    assert "OPENSEARCH_INDEX=branches-v1" in source
    assert "OPENSEARCH_USERNAME=\n" in source
    assert "OPENSEARCH_PASSWORD=\n" in source
    assert "OPENSEARCH_VERIFY_TLS=true" in source
    assert "SEARCH_METRICS_OTLP_ENDPOINT=\n" in source
    assert "SEARCH_METRICS_EXPORT_INTERVAL_SECONDS=30" in source
    assert "SEARCH_METRICS_EXPORT_TIMEOUT_SECONDS=5" in source


def test_non_worker_profiles_never_inherit_worker_database_or_search_credentials() -> None:
    services = _service_blocks(PRODUCTION_IDENTITIES.read_text(encoding="utf-8"))

    api = services["api"]
    assert 'WORKER_DATABASE_URL: ""' in api
    assert 'MAINTENANCE_DATABASE_URL: ""' in api

    maintenance = services["celery-maintenance-worker"]
    assert 'DATABASE_URL: ""' in maintenance
    assert 'AUTH_DATABASE_URL: ""' in maintenance
    assert 'WORKER_DATABASE_URL: ""' in maintenance

    for service_name in ("celery-beat", "flower"):
        block = services[service_name]
        assert 'DATABASE_URL: ""' in block, service_name
        assert 'AUTH_DATABASE_URL: ""' in block, service_name
        assert 'WORKER_DATABASE_URL: ""' in block, service_name
        assert 'MAINTENANCE_DATABASE_URL: ""' in block, service_name
