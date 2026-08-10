from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0006_branch_security_audit.py"
)


def _source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _upgrade_source(source: str) -> str:
    return source[source.index("def upgrade() -> None:") : source.index("def downgrade() -> None:")]


def _downgrade_source(source: str) -> str:
    return source[source.index("def downgrade() -> None:") :]


def test_0006_rejects_partition_helper_adoption_instead_of_replacing_it() -> None:
    source = _source()
    upgrade = _upgrade_source(source)

    assert "target function public.create_next_month_partition" in source
    assert "to_regprocedure(" in source
    assert "CREATE FUNCTION create_next_month_partition" in upgrade
    assert "CREATE OR REPLACE FUNCTION create_next_month_partition" not in upgrade


def test_0006_downgrade_blocks_audit_and_outbox_data_loss() -> None:
    source = _source()
    downgrade = _downgrade_source(source)

    assert "_preflight_downgrade()" in downgrade
    assert "downgrade would discard populated audit/outbox relation" in source
    assert "'branch_audit_log'" in source
    assert "'outbox_events'" in source


def test_0006_downgrade_orders_policy_before_table_and_never_cascades() -> None:
    source = _source()
    downgrade = _downgrade_source(source)

    assert "CASCADE" not in downgrade
    assert "IF EXISTS" not in downgrade
    assert "DROP FUNCTION public.create_next_month_partition(TEXT, TEXT[]) RESTRICT" in downgrade
    assert "DROP TABLE public.outbox_events RESTRICT" in downgrade
    assert "DROP TABLE public.branch_audit_log RESTRICT" in downgrade
    assert downgrade.index("DROP POLICY tenant_isolation_audit ON branch_audit_log") < downgrade.index(
        "DROP TABLE public.branch_audit_log RESTRICT"
    )
