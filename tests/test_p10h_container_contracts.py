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
