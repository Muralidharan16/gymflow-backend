from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
D=(ROOT/"Dockerfile").read_text()
C=(ROOT/"docker-compose.yml").read_text()
I=(ROOT/".dockerignore").read_text()
def test_p10h_image_is_multistage_locked_and_nonroot():
    assert D.count("FROM ") >= 2
    assert "requirements-test.lock" in D and "requirements.txt" not in D
    assert "--no-deps -r /tmp/requirements.lock" in D
    assert "USER 10001:10001" in D
    assert "apt-get" not in D and "build-essential" not in D
def test_p10h_image_has_exec_cmd_healthcheck_and_no_reload():
    assert 'HEALTHCHECK' in D and "/_system/ready" in D
    assert 'CMD ["wget","-q","-T","2","-O","/dev/null","http://127.0.0.1:8000/_system/ready"]' in D
    assert "urllib.request" not in D
    assert 'CMD ["uvicorn","app.main:app"' in D
    assert "--reload" not in D
    assert D.count("EXPOSE ") == 1 and "EXPOSE 8000" in D
def test_p10h_context_excludes_repository_secrets_and_test_payloads():
    for token in (".git",".github",".env","*.pem","*.key","tests","docs"): assert token in I
def test_compose_uses_canonical_celery_and_no_reload():
    assert "app.core.celery_app:celery_app" in C
    assert "app.tasks.celery_app" not in C
    assert "--reload" not in C



def test_production_static_scratch_uses_writable_tmp_boundary():
    source = (ROOT / "app/main.py").read_text(encoding="utf-8")
    assert "tempfile.gettempdir()" in source
    assert "if settings.is_production" in source
    assert '"doers-static"' in source



def test_hardened_runtime_keeps_required_metrics_fail_closed_boundary():
    workflow = (ROOT / ".github/workflows/p10h-container-hardening.yml").read_text(encoding="utf-8")
    assert "scripts/ci/p8o_otlp_collector.py" in workflow
    assert "P8_METRICS_OTLP_ENDPOINT=http://127.0.0.1:4324/v1/metrics" in workflow
    assert "p10h-runtime-metrics.jsonl" in workflow



def test_graceful_shutdown_cleanup_happens_after_sigterm_proof():
    workflow = (ROOT / ".github/workflows/p10h-container-hardening.yml").read_text(encoding="utf-8")
    graceful = workflow.split("- name: Prove graceful SIGTERM", 1)[1]
    assert "docker stop --timeout 15 p10h-api" in graceful
    assert "trap 'docker rm -f p10h-api" in graceful
    runtime = workflow.split("- name: Run with mandatory runtime restrictions", 1)[1].split("- name: Prove graceful SIGTERM", 1)[0]
    assert "docker rm -f p10h-api" not in runtime



def test_p10h_base_image_is_exact_low_vulnerability_alpine_and_runtime_has_no_pip():
    assert "python:3.12.14-alpine3.24@sha256:c4634f578a412db396771b61b064c6e546c9d6414c7fb5b1b05d5871f1885f7b" in D
    assert "/usr/local/lib/python3.12/site-packages/pip" in D
    assert '"$VIRTUAL_ENV/lib/python3.12/site-packages/pip"' in D
    assert "USER 10001:10001" in D


def test_p10h_context_excludes_historical_auth_response_captures():
    assert "login_out.json" in I
    assert "reg_out.json" in I
