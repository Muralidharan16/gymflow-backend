from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool


_FINANCE_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")

# Canonical Finance fixture reset set. Keep this explicit so a new finance
# table/FK cannot be silently absorbed by CASCADE; the live PostgreSQL FK graph
# below is the authority on whether this set remains safe to truncate.
FINANCE_TEST_TABLES: tuple[str, ...] = (
    "outbox_events",
    "audit_events",
    "ledger_entry_lines",
    "ledger_entries",
    "credit_note_lines",
    "credit_notes",
    "refund_execution_commands",
    "refunds",
    "payment_events",
    "payment_allocations",
    "payments",
    "tax_records",
    "invoice_lines",
    "invoices",
    "idempotency_keys",
    "brand_ref_series",
    "invoice_series",
    "billing_parties",
    "ledger_accounts",
    "tax_codes",
    "bank_accounts",
    "brands",
    "divisions",
    "gst_registrations",
    "legal_entities",
)


def _required_url(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required for Finance Core integration tests")
    return value


def _validated_urls() -> tuple[str, str]:
    runtime_url = _required_url("FINANCE_CORE_TEST_DATABASE_URL")
    admin_url = _required_url("FINANCE_CORE_TEST_ADMIN_DATABASE_URL")

    runtime = make_url(runtime_url)
    admin = make_url(admin_url)
    runtime_db = runtime.database or ""
    admin_db = admin.database or ""

    if "test" not in runtime_db.lower() or "test" not in admin_db.lower():
        raise RuntimeError(
            "Finance Core test URLs must target disposable databases whose names contain 'test'"
        )
    if runtime_db != admin_db:
        raise RuntimeError(
            "Finance Core runtime/admin URLs must target the same disposable database"
        )
    if runtime_url == admin_url or runtime.username == admin.username:
        raise RuntimeError(
            "Finance Core admin cleanup must use a distinct database identity"
        )

    return runtime_url, admin_url


def _validated_finance_tables(table_names: Iterable[str]) -> tuple[str, ...]:
    names = tuple(dict.fromkeys(table_names))
    if not names:
        raise RuntimeError("Finance cleanup requires at least one explicit table")
    invalid = [name for name in names if not _FINANCE_IDENTIFIER.fullmatch(name)]
    if invalid:
        raise RuntimeError(f"Unsafe Finance cleanup identifiers: {invalid!r}")
    return names


async def truncate_finance_test_tables(
    session: AsyncSession,
    table_names: Iterable[str] = FINANCE_TEST_TABLES,
) -> None:
    """Truncate an explicit FK-closed Finance test set without CASCADE.

    The live PostgreSQL catalog is authoritative. If any FK child relation is
    outside the requested set (including another schema), cleanup fails closed
    instead of silently deleting that dependent relation through CASCADE.
    """

    names = _validated_finance_tables(table_names)
    dependencies = (
        await session.execute(
            text(
                """
                SELECT
                    child_namespace.nspname AS child_schema,
                    child.relname AS child_table,
                    parent_namespace.nspname AS parent_schema,
                    parent.relname AS parent_table,
                    constraint_data.conname AS constraint_name
                FROM pg_catalog.pg_constraint AS constraint_data
                JOIN pg_catalog.pg_class AS child
                  ON child.oid = constraint_data.conrelid
                JOIN pg_catalog.pg_namespace AS child_namespace
                  ON child_namespace.oid = child.relnamespace
                JOIN pg_catalog.pg_class AS parent
                  ON parent.oid = constraint_data.confrelid
                JOIN pg_catalog.pg_namespace AS parent_namespace
                  ON parent_namespace.oid = parent.relnamespace
                WHERE constraint_data.contype = 'f'
                  AND parent_namespace.nspname = 'finance'
                  AND parent.relname = ANY(CAST(:table_names AS text[]))
                  AND NOT (
                      child_namespace.nspname = 'finance'
                      AND child.relname = ANY(CAST(:table_names AS text[]))
                  )
                ORDER BY child_namespace.nspname, child.relname,
                         parent.relname, constraint_data.conname
                """
            ),
            {"table_names": list(names)},
        )
    ).mappings().all()

    if dependencies:
        rendered = ", ".join(
            f"{row['child_schema']}.{row['child_table']} -> "
            f"{row['parent_schema']}.{row['parent_table']} "
            f"({row['constraint_name']})"
            for row in dependencies
        )
        raise RuntimeError(
            "Refusing Finance cleanup because requested table set is not FK-closed: "
            + rendered
        )

    relations = ", ".join(f'finance."{name}"' for name in names)
    await session.execute(text(f"TRUNCATE TABLE {relations} RESTART IDENTITY"))


@asynccontextmanager
async def finance_admin_session() -> AsyncIterator[AsyncSession]:
    """Yield a guarded, short-lived test-admin session.

    This helper is test-only. Application/service code must never import it.
    Destructive fixture operations are deliberately kept off the Finance Core
    runtime identity so runtime privilege proofs remain meaningful.
    """

    _runtime_url, admin_url = _validated_urls()
    engine = create_async_engine(
        admin_url,
        poolclass=NullPool,
        pool_pre_ping=True,
        echo=False,
    )
    session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    try:
        async with session_factory() as session:
            db_name = (
                await session.execute(text("SELECT current_database()"))
            ).scalar_one()
            if "test" not in str(db_name).lower():
                raise RuntimeError(
                    f"Refusing Finance Core admin operation on non-test database: {db_name}"
                )
            yield session
    finally:
        await engine.dispose()
