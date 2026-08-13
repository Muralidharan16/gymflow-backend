from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0020_contacts_hardened.py"
)


def _source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _upgrade(source: str) -> str:
    return source[source.index("def upgrade():") : source.index("def downgrade():")]


def _downgrade(source: str) -> str:
    return source[source.index("def downgrade():") :]


def test_0020_downgrade_uses_exact_trigger_syntax_and_no_cascade() -> None:
    source = _source()
    downgrade = _downgrade(source)

    assert "TRIGGGER" not in downgrade
    assert "CASCADE" not in downgrade

    expected = (
        "DROP TRIGGER trg_prevent_soft_delete_resurrection ON public.branch_contacts;",
        "DROP TRIGGER trg_prevent_audit_update ON public.branch_contacts_audit;",
        "DROP TRIGGER trg_branch_contacts_updated_at ON public.branch_contacts;",
        "DROP TRIGGER trg_audit_branch_contacts ON public.branch_contacts;",
        "DROP TRIGGER trg_ensure_primary_contact_insert ON public.branch_contacts;",
        "DROP TRIGGER trg_ensure_primary_contact_update ON public.branch_contacts;",
        "DROP TRIGGER trg_ensure_primary_contact_delete ON public.branch_contacts;",
    )
    for statement in expected:
        assert downgrade.count(statement) == 1


def test_0020_domain_lifecycle_is_fail_closed() -> None:
    source = _source()
    upgrade = _upgrade(source)
    downgrade = _downgrade(source)

    assert "_0020_preflight_upgrade_domain()" in upgrade
    assert "_0020_preflight_downgrade_domain()" in downgrade
    assert "CREATE EXTENSION IF NOT EXISTS citext" not in upgrade
    assert "DROP TABLE public.branch_contacts_audit RESTRICT;" in downgrade
    assert "DROP TABLE public.branch_contacts RESTRICT;" in downgrade
    assert "DROP TABLE app_private.partition_metadata RESTRICT;" in downgrade
    assert "DROP TYPE public.contact_kind_enum RESTRICT;" in downgrade
    assert "DROP TYPE public.visibility_scope_enum RESTRICT;" in downgrade
    assert "DROP TYPE public.audit_action_enum RESTRICT;" in downgrade
    assert "DROP TYPE public.verification_method_enum RESTRICT;" in downgrade
