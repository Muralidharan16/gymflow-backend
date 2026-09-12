from __future__ import annotations

import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    ROOT
    / "alembic/versions/zf07d8e9f0a40_p4e_operational_snapshots.py"
)
P4D = ROOT / "alembic/versions/zc07d8e9f0a3d_p4d_refund_authority_boundary.py"


def _source() -> str:
    return MIGRATION.read_text()


def _function_block(source: str, name: str, next_marker: str) -> str:
    start = source.index(f"CREATE FUNCTION app_secure.{name}()")
    end = source.index(next_marker, start)
    return source[start:end]


def test_p4e_slice1a_revision_and_parent_are_exact() -> None:
    tree = ast.parse(_source())
    values: dict[str, object] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in {
                "revision",
                "down_revision",
            }:
                values[target.id] = ast.literal_eval(node.value)
    assert values == {
        "revision": "zf07d8e9f0a40",
        "down_revision": "ze07d8e9f0a3f",
    }


def test_snapshots_are_no_argument_aggregate_only_security_definer() -> None:
    source = _source()
    search = _function_block(
        source,
        "search_operational_snapshot",
        'op.execute(\n        r"""\n        CREATE FUNCTION app_secure.refund_execution_operational_snapshot()',
    )
    refund = _function_block(
        source,
        "refund_execution_operational_snapshot",
        'for signature in (_SEARCH_SNAPSHOT, _REFUND_SNAPSHOT):',
    )

    for body in (search, refund):
        assert "LANGUAGE sql STABLE SECURITY DEFINER" in body
        assert "SET row_security=on" in body
        assert "SET search_path=" in body
        assert "INSERT INTO" not in body
        assert "UPDATE " not in body
        assert "DELETE FROM" not in body
        assert "provider_refund_ref" not in body
        assert "provider_evidence_sha256" not in body
        assert "last_error_code" not in body
        assert "logical_obligation_key" not in body

    assert "CREATE FUNCTION app_secure.search_operational_snapshot()" in search
    assert (
        "CREATE FUNCTION app_secure.refund_execution_operational_snapshot()"
        in refund
    )


def test_snapshot_output_shapes_exclude_high_cardinality_identifiers() -> None:
    source = _source()

    expected = {
        "search_operational_snapshot": {
            "pending_count",
            "processing_count",
            "dead_letter_count",
            "reconciliation_candidate_count",
            "oldest_actionable_age_seconds",
        },
        "refund_execution_operational_snapshot": {
            "pending_count",
            "processing_count",
            "retry_pending_count",
            "provider_accepted_count",
            "reconciliation_pending_count",
            "dead_letter_count",
            "oldest_unresolved_age_seconds",
        },
    }
    forbidden = {
        "tenant_id",
        "organization_id",
        "branch_id",
        "outbox_id",
        "command_id",
        "refund_id",
        "payment_id",
        "legal_entity_id",
        "division_id",
        "brand_id",
        "source_id",
        "logical_obligation_key",
        "provider_refund_ref",
        "provider_evidence_sha256",
        "last_error_code",
    }

    for name, required in expected.items():
        match = re.search(
            rf"CREATE FUNCTION app_secure\.{name}\(\)\s+"
            r"RETURNS TABLE\((.*?)\)\s+LANGUAGE",
            source,
            flags=re.DOTALL,
        )
        assert match is not None
        returns = match.group(1)
        output_names = {
            item.strip().split()[0]
            for item in returns.split(",")
            if item.strip()
        }
        assert output_names == required
        assert forbidden.isdisjoint(output_names)


def test_search_snapshot_reuses_certified_p4b_semantics() -> None:
    source = _source()
    body = _function_block(
        source,
        "search_operational_snapshot",
        'op.execute(\n        r"""\n        CREATE FUNCTION app_secure.refund_execution_operational_snapshot()',
    )

    assert "'branch.search_index'" in body
    assert "'branch.search_deindex'" in body
    assert "status='pending'" in body
    assert "status='processing'" in body
    assert "status='dead_lettered'" in body
    assert (
        "s.search_provider_ack_version\n"
        "                        IS DISTINCT FROM s.search_visibility_version"
        in body
    )
    assert "s.search_provider_reconciled_at IS NULL" in body
    assert (
        "s.search_provider_reconciled_at\n"
        "                        < pg_catalog.clock_timestamp() - INTERVAL '24 hours'"
        in body
    )
    assert "existing.status IN ('pending','processing')" in body


def test_refund_snapshot_reuses_certified_p4d_command_and_parent_semantics() -> None:
    source = _source()
    body = _function_block(
        source,
        "refund_execution_operational_snapshot",
        'for signature in (_SEARCH_SNAPSHOT, _REFUND_SNAPSHOT):',
    )
    for status in (
        "pending",
        "processing",
        "retry_pending",
        "provider_accepted",
        "reconciliation_pending",
        "dead_lettered",
    ):
        assert f"c.status='{status}'" in body or f"'{status}'" in body

    for terminal in ("succeeded", "rejected", "cancelled"):
        assert f"c.status='{terminal}'" not in body

    assert "JOIN finance.refunds AS r ON r.id = c.refund_id" in body
    assert "WHERE r.status IN ('requested','approved','processing')" in body

    predecessor = P4D.read_text()
    assert "JOIN finance.refunds r ON r.id = c.refund_id" in predecessor
    assert "WHERE r.status IN ('requested','approved','processing')" in predecessor


def test_p4e_adds_zero_direct_table_acl_and_execute_is_maintenance_only() -> None:
    source = _source()

    # zc07 already provides the SECURITY DEFINER owner the table reads needed
    # by both snapshots. P4E must not widen direct table authority at all.
    predecessor = P4D.read_text()
    assert (
        "GRANT SELECT ON TABLE public.branch_outbox_events TO app_security_owner"
        in predecessor
    )
    assert "GRANT SELECT, UPDATE ON TABLE finance.refunds TO app_security_owner" in predecessor
    assert (
        "GRANT SELECT, INSERT, UPDATE ON TABLE finance.refund_execution_commands TO app_security_owner"
        in predecessor
    )

    assert "GRANT SELECT (" not in source
    assert "GRANT SELECT ON TABLE" not in source
    assert "GRANT SELECT, INSERT" not in source
    assert "GRANT UPDATE" not in source
    assert "GRANT DELETE" not in source
    assert "REVOKE SELECT (" not in source

    assert (
        'op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")'
        in source
    )
    assert (
        'f"GRANT EXECUTE ON FUNCTION {signature} TO {_MAINTENANCE}"'
        in source
    )

    for blocked in (
        "app_runtime",
        "auth_runtime",
        "worker_runtime",
        "finance_config_runtime",
    ):
        assert f"TO {blocked}" not in source


def test_migration_catalog_proofs_do_not_require_app_secure_schema_usage() -> None:
    source = _source()

    # migration_owner intentionally has no app_secure USAGE. Migration
    # pre/post/downgrade proofs therefore must resolve objects through system
    # catalogs/OIDs rather than schema-qualified reg* name resolution.
    assert "to_regprocedure" not in source
    assert "to_regclass" not in source
    assert "pg_catalog.pg_proc" in source
    assert "pg_catalog.pg_namespace" in source
    assert "CAST(:function_oid AS oid)" in source
    assert "CAST(:relation_oid AS oid)" in source


def test_no_rls_or_provider_execution_escape_hatches() -> None:
    source = _source()
    normalized = source.upper()

    assert "BYPASSRLS" not in normalized
    assert "DISABLE ROW LEVEL SECURITY" not in normalized
    assert "NO FORCE ROW LEVEL SECURITY" not in normalized
    assert "ALTER TABLE" not in normalized
    assert "RAZORPAY" not in normalized
    assert "CREATE_REFUND" not in normalized
    assert "EXECUTE_REFUND" not in normalized


def test_downgrade_drops_only_p4e_functions_and_no_acl_or_data() -> None:
    source = _source()
    downgrade = source.split("def downgrade() -> None:", 1)[1]

    assert (
        "DROP FUNCTION app_secure.refund_execution_operational_snapshot()"
        in downgrade
    )
    assert (
        "DROP FUNCTION app_secure.search_operational_snapshot()"
        in downgrade
    )
    assert "DROP TABLE" not in downgrade
    assert "DELETE FROM" not in downgrade
    assert "UPDATE " not in downgrade
    assert "TRUNCATE" not in downgrade
    assert "REVOKE SELECT" not in downgrade
    assert "GRANT " not in downgrade
