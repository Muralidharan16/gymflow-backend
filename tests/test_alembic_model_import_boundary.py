from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap


_RUNTIME_ONLY_SETTINGS = (
    "REDIS_URL",
    "CELERY_BROKER_URL",
    "CELERY_RESULT_BACKEND",
    "SECRET_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
)


def test_app_models_import_does_not_load_runtime_task_stack() -> None:
    """Alembic metadata import must not initialize runtime services."""

    repository = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()

    for variable in _RUNTIME_ONLY_SETTINGS:
        environment.pop(variable, None)

    environment["DATABASE_URL"] = (
        "postgresql+asyncpg://migration_owner@127.0.0.1:9/"
        "rb1c_import_boundary"
    )
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONHASHSEED"] = "0"

    program = textwrap.dedent(
        """
        import json
        import sys

        import app.models
        from app.models import Base
        import app.models.address as address_models

        forbidden = (
            "app.tasks.geocoding",
            "app.core.celery_app",
            "app.core.config",
        )
        loaded_forbidden = [
            module
            for module in forbidden
            if module in sys.modules
        ]
        assert not loaded_forbidden, loaded_forbidden

        address_tables = {
            value.__table__.key
            for value in vars(address_models).values()
            if isinstance(value, type)
            and value.__module__ == address_models.__name__
            and hasattr(value, "__table__")
        }
        assert address_tables

        metadata_tables = set(Base.metadata.tables)
        missing_tables = sorted(address_tables - metadata_tables)
        assert not missing_tables, missing_tables

        print(
            json.dumps(
                {
                    "address_table_count": len(address_tables),
                    "forbidden_modules_loaded": loaded_forbidden,
                    "missing_metadata_tables": missing_tables,
                },
                sort_keys=True,
            )
        )
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", program],
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, (
        f"returncode={completed.returncode}\n"
        f"stdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )
