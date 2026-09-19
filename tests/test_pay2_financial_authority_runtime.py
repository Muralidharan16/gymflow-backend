from __future__ import annotations

import os

import psycopg
import pytest


DB_URL = os.environ.get("PAY2_RUNTIME_DATABASE_URL")
ATTACK_URL = os.environ.get("PAY2_ATTACK_DATABASE_URL")

# This file is an isolated PAY-2 authority/attack harness, not a generic
# repository runtime test. Broad inherited suites intentionally do not receive
# PAY-2 database credentials. A partially configured PAY-2 harness must still
# fail through _required() rather than being silently skipped.
pytestmark = pytest.mark.skipif(
    DB_URL is None and ATTACK_URL is None,
    reason="PAY-2 isolated runtime harness is not configured",
)


def _required(value: str | None, name: str) -> str:
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def test_pay2_head_has_exact_authority_objects():
    with psycopg.connect(_required(DB_URL, "PAY2_RUNTIME_DATABASE_URL")) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version_num FROM alembic_version")
            assert cur.fetchone()[0] == "zl07d8e9f0a46"
            cur.execute(
                """
                SELECT count(*)
                FROM pg_catalog.pg_trigger t
                JOIN pg_catalog.pg_class c ON c.oid=t.tgrelid
                JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='finance'
                  AND t.tgname IN ('pay2_immutable_finance_history_guard','pay2_posted_finance_ledger_guard')
                  AND NOT t.tgisinternal
                  AND t.tgenabled='O'
                """
            )
            assert cur.fetchone()[0] == 6


@pytest.mark.parametrize("statement, expected", [
    (
        "UPDATE finance.audit_events SET target_type='tampered' WHERE target_type='pay2_seed'",
        "PAY-2 immutable finance history",
    ),
    (
        "DELETE FROM finance.audit_events WHERE target_type='pay2_seed'",
        "PAY-2 immutable finance history",
    ),
    (
        "UPDATE finance.ledger_entries SET source_type='tampered' WHERE source_type='pay2_seed'",
        "PAY-2 posted ledger entry is immutable",
    ),
    (
        "DELETE FROM finance.ledger_entries WHERE source_type='pay2_seed'",
        "PAY-2 posted ledger entry is immutable",
    ),
])
def test_accidentally_broadened_runtime_acl_still_cannot_rewrite_evidence(statement, expected):
    with psycopg.connect(_required(ATTACK_URL, "PAY2_ATTACK_DATABASE_URL")) as conn:
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege) as exc:
                cur.execute(statement)
            assert expected in str(exc.value)
        conn.rollback()
