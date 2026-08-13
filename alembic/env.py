import os
import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Import all models so Alembic sees them
from app.models import Base  # noqa: F401
import app.finance_core.models  # noqa: F401
import app.platform_billing.models  # noqa: F401
from app.core.cluster_role_preflight import assert_external_role_preflight


config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Override sqlalchemy.url from the required DATABASE_URL environment variable
database_url = os.environ.get("DATABASE_URL")
if not database_url or not database_url.strip():
    raise RuntimeError(
        "DATABASE_URL is required for Alembic migrations."
    )

config.set_main_option(
    "sqlalchemy.url",
    database_url.replace("%", "%%"),
)


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


def _destination_targets_head() -> bool:
    """Return True only when the requested Alembic destination is a repo HEAD."""

    try:
        destination = context.get_revision_argument()
    except KeyError as exc:
        # Inspection commands such as ``alembic current --check-heads`` do not
        # populate Alembic's destination_rev context option. They cannot execute
        # revisions, so there is no migration destination to hard-gate here.
        # Keep this catch exact so an unrelated Alembic context failure is never
        # silently treated as a non-HEAD command.
        if exc.args != ("destination_rev",):
            raise
        return False

    if destination is None:
        return False

    if isinstance(destination, (tuple, list, set, frozenset)):
        destination_revisions = {value for value in destination if value}
    else:
        destination_revisions = {destination}

    head_revisions = set(context.get_head_revisions() or ())
    return bool(destination_revisions & head_revisions)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    if _destination_targets_head():
        raise RuntimeError(
            "Alembic HEAD requires a live PostgreSQL external-role preflight; "
            "offline HEAD execution is forbidden."
        )

    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_schemas=True,
    )

    with context.begin_transaction():
        check_destructive_migrations()
        context.run_migrations()


def do_run_migrations(connection) -> None:
    if _destination_targets_head():
        # This is deliberately before context.configure()/begin_transaction():
        # HEAD may not mutate database state until the externally managed
        # PostgreSQL role/settings/membership contract has been proven live.
        assert_external_role_preflight(connection)

    context.configure(
        connection=connection, 
        target_metadata=target_metadata,
        compare_type=True,
        include_schemas=True,
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