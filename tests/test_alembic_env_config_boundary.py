from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import sys


_RUNTIME_ONLY_SETTINGS = (
    "REDIS_URL",
    "CELERY_BROKER_URL",
    "CELERY_RESULT_BACKEND",
    "SECRET_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
)


def test_alembic_env_uses_explicit_database_url_boundary() -> None:
    repository = Path(__file__).resolve().parents[1]
    env_path = repository / "alembic" / "env.py"
    source = env_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(env_path))

    config_imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "app.core.config"
    ]
    assert not config_imports

    settings_uses = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and node.id == "settings"
    ]
    assert not settings_uses

    database_url_reads = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
        and isinstance(node.func.value.value, ast.Name)
        and node.func.value.value.id == "os"
        and node.func.value.attr == "environ"
        and node.func.attr == "get"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "DATABASE_URL"
    ]
    assert len(database_url_reads) == 1

    error_messages = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
    }
    assert (
        "DATABASE_URL is required for Alembic migrations."
        in error_messages
    )


def test_alembic_head_is_hard_gated_by_live_external_role_preflight() -> None:
    repository = Path(__file__).resolve().parents[1]
    env_path = repository / "alembic" / "env.py"
    source = env_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(env_path))

    assert "from app.core.cluster_role_preflight import assert_external_role_preflight" in source
    assert "context.get_revision_argument()" in source
    assert "context.get_head_revisions()" in source
    assert "offline HEAD execution is forbidden" in source

    do_run = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "do_run_migrations"
    )
    do_run_source = ast.get_source_segment(source, do_run)
    assert do_run_source is not None
    assert "if _destination_targets_head():" in do_run_source
    assert "assert_external_role_preflight(connection)" in do_run_source
    assert do_run_source.index("assert_external_role_preflight(connection)") < do_run_source.index(
        "context.configure("
    )
    assert do_run_source.index("assert_external_role_preflight(connection)") < do_run_source.index(
        "context.run_migrations()"
    )


def test_alembic_missing_database_url_fails_without_runtime_settings() -> None:
    repository = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()

    environment.pop("DATABASE_URL", None)

    for variable in _RUNTIME_ONLY_SETTINGS:
        environment.pop(variable, None)

    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONHASHSEED"] = "0"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(repository / "alembic.ini"),
            "current",
        ],
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    combined = f"{completed.stdout}\n{completed.stderr}"

    assert completed.returncode != 0
    assert (
        "DATABASE_URL is required for Alembic migrations."
        in combined
    )
    assert "ValidationError" not in combined

    for variable in _RUNTIME_ONLY_SETTINGS:
        assert f"{variable}\n  Field required" not in combined
