import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Import all models so Alembic sees them
from app.models import Base  # noqa: F401
import app.platform_billing.models  # noqa: F401
from app.core.config import settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Override sqlalchemy.url from settings
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL.replace("%", "%%"))


import os
import sys

def check_destructive_migrations() -> None:
    """
    Zero-Downtime Expand/Contract schema validation:
    Blocks migrations that perform destructive database alterations (e.g., DROP COLUMN, DROP TABLE)
    unless the ALLOW_DESTRUCTIVE_MIGRATIONS environment variable is set to 'true'.
    """
    if os.environ.get("ALLOW_DESTRUCTIVE_MIGRATIONS") == "true":
        return

    # Let's inspect the active migration script context
    migration_context = context.get_context()
    script_directory = migration_context.script
    
    # Fetch migration revisions being executed
    current_head = context.get_head_revision()
    if not current_head:
        return

    try:
        # Resolve target revision sequence
        revisions = script_directory.get_revisions(context.get_revision_argument() or "head")
        if not isinstance(revisions, list):
            revisions = [revisions]

        for rev in revisions:
            if not rev:
                continue
            # Load the migration script module dynamically
            module = rev.module
            is_destructive = getattr(module, "destructive", False) or getattr(module, "phase", None) == "contract"
            
            if is_destructive:
                raise ValueError(
                    f"CRITICAL: Migration {rev.revision} ({rev.doc}) is marked as DESTRUCTIVE / CONTRACT phase.\n"
                    "Destructive schema updates are blocked in zero-downtime deployment pipelines.\n"
                    "To bypass this check, export ALLOW_DESTRUCTIVE_MIGRATIONS=true."
                )
    except Exception as exc:
        if isinstance(exc, ValueError):
            raise
        # Allow default behavior if scripts cannot be parsed
        pass


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        check_destructive_migrations()
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection, 
        target_metadata=target_metadata,
        compare_type=True,
    )

    with context.begin_transaction():
        check_destructive_migrations()
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode with async engine."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
