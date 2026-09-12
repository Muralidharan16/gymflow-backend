from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_IDENTITIES = ROOT / "deploy" / "docker-compose.production-identities.yml"
ENV_EXAMPLE = ROOT / ".env.example"
CONFIG = ROOT / "app" / "core" / "config.py"


def _service_blocks(source: str) -> dict[str, str]:
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


def test_p4c_provider_and_webhook_secrets_are_process_isolated() -> None:
    services = _service_blocks(PRODUCTION_IDENTITIES.read_text(encoding="utf-8"))
    api = services["api"]
    worker = services["celery-worker"]
    maintenance = services["celery-maintenance-worker"]

    assert "NOTIFICATION_EMAIL_PROVIDER_MODE: disabled" in api
    assert 'P4C_RESEND_API_KEY: ""' in api
    assert "RESEND_WEBHOOK_SECRET: ${RESEND_WEBHOOK_SECRET:-}" in api
    assert 'NOTIFICATION_METRICS_OTLP_ENDPOINT: ""' in api
    assert "${P4C_RESEND_API_KEY" not in api

    assert "NOTIFICATION_EMAIL_PROVIDER_MODE: ${NOTIFICATION_EMAIL_PROVIDER_MODE:-disabled}" in worker
    assert "P4C_RESEND_API_KEY: ${P4C_RESEND_API_KEY:-}" in worker
    assert 'RESEND_WEBHOOK_SECRET: ""' in worker
    assert "NOTIFICATION_METRICS_OTLP_ENDPOINT: ${NOTIFICATION_METRICS_OTLP_ENDPOINT:-}" in worker
    assert "${RESEND_WEBHOOK_SECRET" not in worker

    assert "NOTIFICATION_EMAIL_PROVIDER_MODE: disabled" in maintenance
    assert 'P4C_RESEND_API_KEY: ""' in maintenance
    assert 'RESEND_WEBHOOK_SECRET: ""' in maintenance
    assert "NOTIFICATION_METRICS_OTLP_ENDPOINT: ${NOTIFICATION_METRICS_OTLP_ENDPOINT:-}" in maintenance

    for service_name in ("celery-beat", "flower"):
        block = services[service_name]
        assert "NOTIFICATION_EMAIL_PROVIDER_MODE: disabled" in block
        assert 'P4C_RESEND_API_KEY: ""' in block
        assert 'RESEND_WEBHOOK_SECRET: ""' in block
        assert 'NOTIFICATION_METRICS_OTLP_ENDPOINT: ""' in block
        assert "${P4C_RESEND_API_KEY" not in block
        assert "${RESEND_WEBHOOK_SECRET" not in block
        assert "${NOTIFICATION_METRICS_OTLP_ENDPOINT" not in block


def test_p4c_example_configuration_is_fail_closed() -> None:
    source = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "NOTIFICATION_EMAIL_PROVIDER_MODE=disabled" in source
    assert "P4C_RESEND_API_KEY=\n" in source
    assert "RESEND_WEBHOOK_SECRET=\n" in source
    assert "NOTIFICATION_METRICS_OTLP_ENDPOINT=\n" in source
    assert "NOTIFICATION_METRICS_EXPORT_INTERVAL_SECONDS=30" in source
    assert "NOTIFICATION_METRICS_EXPORT_TIMEOUT_SECONDS=5" in source


def test_production_settings_encode_p4c_separation_and_metrics_requirement() -> None:
    source = CONFIG.read_text(encoding="utf-8")
    assert "RESEND_WEBHOOK_SECRET is restricted to the API profile" in source
    assert "P4C_RESEND_API_KEY is required when Resend notifications are enabled" in source
    assert "P4C Resend sending and notification-metrics authority is restricted away from the API profile" in source
    assert "P4C notification sending/webhook credentials are forbidden for maintenance" in source
    assert "P4C notification provider/webhook/metrics configuration is forbidden for beat profiles" in source
    assert "_validate_notification_metrics(" in source
