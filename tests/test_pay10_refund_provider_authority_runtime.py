from __future__ import annotations

import os

import psycopg
import pytest


APP_URL = os.environ.get("PAY10_APP_DATABASE_URL")
REFUND_URL = os.environ.get("PAY10_REFUND_DATABASE_URL")
RECON_URL = os.environ.get("PAY10_RECON_DATABASE_URL")
WORKER_URL = os.environ.get("PAY10_WORKER_DATABASE_URL")
ADMIN_URL = os.environ.get("PAY10_ADMIN_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not all((APP_URL, REFUND_URL, RECON_URL, WORKER_URL, ADMIN_URL)),
    reason="PAY-10 isolated PG16 harness is not configured",
)


NEW_TABLES = (
    "credit_note_series",
    "refund_credit_note_links",
    "refund_provider_evidence",
)


def _admin_rows(sql: str, params=()):
    assert ADMIN_URL
    with psycopg.connect(ADMIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def _admin_scalar(sql: str, params=()):
    rows = _admin_rows(sql, params)
    assert len(rows) == 1
    return rows[0][0]


def test_pay10_relations_are_force_rls_and_owned_outside_runtime_roles():
    rows = _admin_rows(
        """
        SELECT
            c.relname,
            c.relrowsecurity,
            c.relforcerowsecurity,
            pg_catalog.pg_get_userbyid(c.relowner)
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='finance'
          AND c.relname=ANY(%s)
        ORDER BY c.relname
        """,
        (list(NEW_TABLES),),
    )
    assert [row[0] for row in rows] == sorted(NEW_TABLES)
    assert all(row[1] is True and row[2] is True for row in rows)
    assert all(row[3] == "migration_owner" for row in rows)


def test_pay10_runtime_identities_have_no_direct_new_finance_table_authority():
    runtime_urls = {
        "app": APP_URL,
        "refund": REFUND_URL,
        "reconciliation": RECON_URL,
        "worker": WORKER_URL,
    }
    for label, url in runtime_urls.items():
        assert url
        with psycopg.connect(url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        session_user::text,
                        current_user::text,
                        r.rolsuper,
                        r.rolcreaterole,
                        r.rolcreatedb,
                        r.rolreplication,
                        r.rolbypassrls
                    FROM pg_catalog.pg_roles r
                    WHERE r.rolname=current_user
                    """
                )
                identity = cur.fetchone()
                assert identity is not None
                assert identity[0] == identity[1]
                assert identity[2:] == (False, False, False, False, False)

                for table_name in NEW_TABLES:
                    with pytest.raises(psycopg.Error):
                        cur.execute(
                            f"SELECT count(*) FROM finance.{table_name}"
                        )
                    conn.rollback()

        assert label


def test_pay10_new_tables_have_no_runtime_grants_or_public_grants():
    rows = _admin_rows(
        """
        SELECT table_name,grantee,privilege_type
        FROM information_schema.role_table_grants
        WHERE table_schema='finance'
          AND table_name=ANY(%s)
          AND (
              grantee='PUBLIC'
              OR grantee IN (
                  'app_runtime',
                  'worker_runtime',
                  'finance_runtime',
                  'finance_payment_runtime',
                  'finance_refund_runtime',
                  'finance_reconciliation_runtime',
                  'finance_read_runtime',
                  'finance_maintenance_runtime'
              )
          )
        ORDER BY table_name,grantee,privilege_type
        """,
        (list(NEW_TABLES),),
    )
    assert rows == []


def test_pay10_provider_evidence_uses_pay2_immutable_history_guard():
    rows = _admin_rows(
        """
        SELECT
            t.tgname,
            p.proname,
            owner.rolname,
            p.prosecdef,
            t.tgenabled
        FROM pg_catalog.pg_trigger t
        JOIN pg_catalog.pg_class c ON c.oid=t.tgrelid
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_catalog.pg_proc p ON p.oid=t.tgfoid
        JOIN pg_catalog.pg_roles owner ON owner.oid=p.proowner
        WHERE n.nspname='finance'
          AND c.relname='refund_provider_evidence'
          AND NOT t.tgisinternal
        """
    )
    assert rows == [
        (
            "pay10_immutable_refund_provider_evidence",
            "pay2_reject_finance_immutable_history_mutation",
            "app_security_owner",
            True,
            "O",
        )
    ]


def test_pay10_refund_command_attempt_identity_columns_and_constraints_exist():
    columns = {
        row[0]
        for row in _admin_rows(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema='finance'
              AND table_name='refund_execution_commands'
            """
        )
    }
    assert {
        "request_sha256",
        "first_attempted_at",
        "provider_accepted_at",
        "completed_at",
    } <= columns

    constraints = {
        row[0]
        for row in _admin_rows(
            """
            SELECT conname
            FROM pg_catalog.pg_constraint
            WHERE connamespace='finance'::regnamespace
            """
        )
    }
    assert {
        "uq_pay10_refunds_id_org",
        "uq_pay10_credit_notes_id_org",
        "fk_pay10_refund_credit_link_refund_org",
        "fk_pay10_refund_credit_link_credit_org",
        "uq_pay10_refund_credit_link",
        "uq_pay10_refund_credit_note_single_refund",
        "chk_pay10_refund_credit_link_amount",
        "chk_pay10_refund_command_request_hash",
        "chk_pay10_refund_command_attempt_timestamps",
        "chk_pay10_refund_command_completion_timestamps",
        "uq_pay10_refund_evidence_hash",
    } <= constraints


def test_pay10_provider_event_and_provider_refund_identity_indexes_are_unique():
    rows = _admin_rows(
        """
        SELECT indexname,indexdef
        FROM pg_catalog.pg_indexes
        WHERE schemaname='finance'
          AND indexname IN (
              'uq_pay10_refund_provider_event',
              'uq_pay10_refund_command_provider_ref'
          )
        ORDER BY indexname
        """
    )
    assert [row[0] for row in rows] == [
        "uq_pay10_refund_command_provider_ref",
        "uq_pay10_refund_provider_event",
    ]
    assert all(" UNIQUE INDEX " in f" {row[1]} " for row in rows)


def test_pay10_app_security_owner_has_only_declared_new_table_privileges():
    rows = _admin_rows(
        """
        SELECT table_name,privilege_type
        FROM information_schema.role_table_grants
        WHERE table_schema='finance'
          AND table_name=ANY(%s)
          AND grantee='app_security_owner'
        ORDER BY table_name,privilege_type
        """,
        (list(NEW_TABLES),),
    )
    actual = {(row[0], row[1]) for row in rows}
    assert actual == {
        ("credit_note_series", "INSERT"),
        ("credit_note_series", "SELECT"),
        ("credit_note_series", "UPDATE"),
        ("refund_credit_note_links", "INSERT"),
        ("refund_credit_note_links", "SELECT"),
        ("refund_provider_evidence", "INSERT"),
        ("refund_provider_evidence", "SELECT"),
    }
    assert _admin_scalar(
        """
        SELECT pg_catalog.has_table_privilege(
            'app_security_owner',
            'finance.refund_provider_evidence',
            'TRIGGER'
        )
        """
    ) is False
