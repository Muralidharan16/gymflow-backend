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
from app.core.cluster_identity_graph import assert_identity_graph_preflight
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

_P9L_BOUNDED_SESSION_FLAG = "P9L_BOUNDED_MIGRATION_SESSION"
_P9L_EXPECTED_LOCK_TIMEOUT_MS = 1500
_P9L_EXPECTED_STATEMENT_TIMEOUT_MS = 15000
_P9L_APPLICATION_NAME = "p9l_alembic_migration"


def _p9l_bounded_session_enabled() -> bool:
    return os.environ.get(_P9L_BOUNDED_SESSION_FLAG) == "1"


def _p9l_server_settings() -> dict[str, str]:
    """Return test-only startup settings for the P9-L migration rehearsal.

    The production role graph forbids persistent managed-role/database settings.
    P9-L therefore injects the frozen timeout ceilings only into the exact
    Alembic connection used by the rehearsal. Nothing is written to
    pg_db_role_setting and the canonical role verifier remains authoritative.
    """

    if not _p9l_bounded_session_enabled():
        return {}
    if os.environ.get("ENVIRONMENT") != "test":
        raise RuntimeError("P9-L bounded migration session is test-only")

    raw_lock = os.environ.get("P9L_LOCK_TIMEOUT_MS", "")
    raw_statement = os.environ.get("P9L_STATEMENT_TIMEOUT_MS", "")
    try:
        lock_timeout_ms = int(raw_lock)
        statement_timeout_ms = int(raw_statement)
    except ValueError as exc:
        raise RuntimeError("P9-L timeout values must be exact integers") from exc

    if lock_timeout_ms != _P9L_EXPECTED_LOCK_TIMEOUT_MS:
        raise RuntimeError(
            "P9-L lock timeout drift: "
            f"expected {_P9L_EXPECTED_LOCK_TIMEOUT_MS}ms, got {lock_timeout_ms}ms"
        )
    if statement_timeout_ms != _P9L_EXPECTED_STATEMENT_TIMEOUT_MS:
        raise RuntimeError(
            "P9-L statement timeout drift: "
            f"expected {_P9L_EXPECTED_STATEMENT_TIMEOUT_MS}ms, "
            f"got {statement_timeout_ms}ms"
        )

    return {
        "lock_timeout": f"{lock_timeout_ms}ms",
        "statement_timeout": f"{statement_timeout_ms}ms",
        "application_name": _P9L_APPLICATION_NAME,
    }


def _assert_p9l_session_timeouts(connection) -> None:
    if not _p9l_bounded_session_enabled():
        return

    rows = connection.exec_driver_sql(
        """
        SELECT name, setting::bigint AS setting_ms, unit
        FROM pg_catalog.pg_settings
        WHERE name IN ('lock_timeout', 'statement_timeout')
        ORDER BY name
        """
    ).mappings().all()
    observed = {
        row["name"]: (int(row["setting_ms"]), row["unit"])
        for row in rows
    }
    expected = {
        "lock_timeout": (_P9L_EXPECTED_LOCK_TIMEOUT_MS, "ms"),
        "statement_timeout": (_P9L_EXPECTED_STATEMENT_TIMEOUT_MS, "ms"),
    }
    if observed != expected:
        raise RuntimeError(
            f"P9-L Alembic session timeout mismatch: {observed!r}"
        )

    app_name = connection.exec_driver_sql(
        "SELECT current_setting('application_name')"
    ).scalar_one()
    if app_name != _P9L_APPLICATION_NAME:
        raise RuntimeError(
            "P9-L Alembic application_name mismatch: "
            f"expected {_P9L_APPLICATION_NAME!r}, got {app_name!r}"
        )

    print(
        "P9L_ALEMBIC_SESSION_TIMEOUTS=PASS "
        f"lock_timeout_ms={_P9L_EXPECTED_LOCK_TIMEOUT_MS} "
        f"statement_timeout_ms={_P9L_EXPECTED_STATEMENT_TIMEOUT_MS}"
    )


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
        # P9-L uses test-only connection startup settings. Prove the exact
        # migration backend received them without persisting role/database GUCs.
        _assert_p9l_session_timeouts(connection)
        # This is deliberately before context.configure()/begin_transaction():
        # HEAD may not mutate database state until the externally managed
        # PostgreSQL role/settings/membership contract has been proven live.
        assert_external_role_preflight(connection)
        # P2C is a second independent read-only hard gate. It proves that the
        # exact roles accepted above cannot reach peer capabilities through
        # MEMBER, SET, USAGE, helper, or ADMIN-option escalation paths.
        assert_identity_graph_preflight(connection)

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
    server_settings = _p9l_server_settings()
    connect_args = {"server_settings": server_settings} if server_settings else {}
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args=connect_args,
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
