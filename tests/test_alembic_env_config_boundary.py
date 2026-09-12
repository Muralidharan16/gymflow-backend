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


def _is_context_call(node: ast.AST, attribute: str) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "context"
        and node.func.attr == attribute
    )


def _is_named_call(node: ast.AST, name: str) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == name
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

    head_guard = next(
        node
        for node in do_run.body
        if isinstance(node, ast.If)
        and any(
            _is_named_call(candidate, "_destination_targets_head")
            for candidate in ast.walk(node.test)
        )
    )
    preflight_call = next(
        node
        for node in ast.walk(head_guard)
        if _is_named_call(node, "assert_external_role_preflight")
    )
    configure_call = next(
        node
        for node in ast.walk(do_run)
        if _is_context_call(node, "configure")
    )
    begin_transaction_call = next(
        node
        for node in ast.walk(do_run)
        if _is_context_call(node, "begin_transaction")
    )
    run_migrations_call = next(
        node
        for node in ast.walk(do_run)
        if _is_context_call(node, "run_migrations")
    )

    assert preflight_call.lineno < configure_call.lineno
    assert preflight_call.lineno < begin_transaction_call.lineno
    assert preflight_call.lineno < run_migrations_call.lineno


def test_alembic_non_destination_commands_handle_absent_destination_rev_exactly() -> None:
    repository = Path(__file__).resolve().parents[1]
    env_path = repository / "alembic" / "env.py"
    source = env_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(env_path))

    destination_probe = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_destination_targets_head"
    )
    revision_try = next(
        node
        for node in destination_probe.body
        if isinstance(node, ast.Try)
        and any(
            _is_context_call(candidate, "get_revision_argument")
            for candidate in ast.walk(node)
        )
    )
    key_error_handler = next(
        handler
        for handler in revision_try.handlers
        if isinstance(handler.type, ast.Name)
        and handler.type.id == "KeyError"
    )

    handler_constants = {
        node.value
        for node in ast.walk(key_error_handler)
        if isinstance(node, ast.Constant)
    }
    false_returns = [
        node
        for node in ast.walk(key_error_handler)
        if isinstance(node, ast.Return)
        and isinstance(node.value, ast.Constant)
        and node.value.value is False
    ]

    assert "destination_rev" in handler_constants
    assert len(false_returns) == 1


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
