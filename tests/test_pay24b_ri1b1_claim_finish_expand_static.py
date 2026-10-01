from __future__ import annotations

import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    ROOT
    / "alembic"
    / "versions"
    / "zz77d8e9f0a67_pay24b_ri1b1_claim_finish_expand.py"
)
FUNCTION_SQL_NAMES = (
    "_CLAIM_EXPANDED_SQL",
    "_FINISH_EXPANDED_SQL",
    "_CLAIM_ORIGINAL_SQL",
    "_FINISH_ORIGINAL_SQL",
)


def _source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _tree(source: str) -> ast.Module:
    return ast.parse(source)


def _function_source(source: str, name: str) -> str:
    for node in _tree(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == name:
                segment = ast.get_source_segment(source, node)
                assert segment is not None
                return segment
    raise AssertionError(f"missing function: {name}")


def _assigned_string(source: str, name: str) -> str:
    for node in _tree(source).body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == name:
            value = ast.literal_eval(node.value)
            assert isinstance(value, str)
            return value
    raise AssertionError(f"missing string assignment: {name}")


def _normalized_sql(value: str) -> str:
    return " ".join(value.strip().rstrip(";").split()).upper()


def _executed_literal_sql(source: str) -> list[str]:
    statements: list[str] = []
    for node in ast.walk(_tree(source)):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in {"execute", "exec_driver_sql"}:
            continue
        argument = node.args[0]
        if (
            isinstance(argument, ast.Call)
            and isinstance(argument.func, ast.Attribute)
            and argument.func.attr == "text"
            and argument.args
        ):
            argument = argument.args[0]
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            statements.append(_normalized_sql(argument.value))
    return statements


def _indices(value: str, needle: str) -> list[int]:
    return [match.start() for match in re.finditer(re.escape(needle), value)]


def test_ri1b1_revision_is_exact_successor_of_b1a_bridge() -> None:
    source = _source()
    assert 'revision = "zz77d8e9f0a67"' in source
    assert 'down_revision = "zz67d8e9f0a66"' in source


def test_ri1b1_expand_is_claim_finish_only() -> None:
    source = _source()
    assert "claim_finance_provider_operation" in source
    assert "finish_finance_provider_operation" in source
    assert "reserve_finance_provider_operation" not in source
    assert "reconcile_finance_provider_operation" not in source


def test_catalog_preflight_avoids_app_secure_name_resolution() -> None:
    source = _source()
    observation = _function_source(source, "_function_observation")
    compact = " ".join(observation.split())

    assert "to_regprocedure" not in source
    assert "FROM pg_catalog.pg_proc AS p" in compact
    assert "JOIN pg_catalog.pg_namespace AS n ON n.oid=p.pronamespace" in compact
    assert (
        "JOIN pg_catalog.pg_roles AS owner_role ON owner_role.oid=p.proowner"
        in compact
    )
    assert "n.nspname='app_secure'" in compact
    assert "p.proname=:function_name" in compact
    assert "p.prokind='f'" in compact
    assert ").mappings().all()" in compact
    assert "if len(rows) != 1:" in observation
    assert "pg_catalog.oidvectortypes(p.proargtypes)" in observation
    assert 'row["argument_types"] != expected_argument_types' in observation
    assert not re.search(
        r"GRANT\s+USAGE\s+ON\s+SCHEMA\s+app_secure\s+TO\s+migration_owner",
        source,
        re.IGNORECASE,
    )


def test_owner_switch_is_local_verified_and_reset_to_session_identity() -> None:
    source = _source()
    statements = _executed_literal_sql(source)
    role_commands = [
        statement
        for statement in statements
        if re.match(r"^(?:SET(?: LOCAL)? ROLE|RESET ROLE)\b", statement)
    ]

    assert "SET ROLE APP_SECURITY_OWNER" not in role_commands
    assert "SET LOCAL ROLE APP_SECURITY_OWNER" in role_commands
    assert "RESET ROLE" in role_commands
    assert set(role_commands) == {
        "SET LOCAL ROLE APP_SECURITY_OWNER",
        "RESET ROLE",
    }
    assert (
        "SELECT PG_CATALOG.PG_HAS_ROLE("
        "SESSION_USER, 'APP_SECURITY_OWNER', 'SET')"
    ) in statements

    identity = _function_source(source, "_identity")
    require_owner = _function_source(source, "_require_migration_owner")
    enter = _function_source(source, "_enter_security_owner")
    reset = _function_source(source, "_reset_role")

    assert "SELECT session_user::text, current_user::text" in identity
    assert "session_name != current_name" in require_owner
    assert 'session_name != "migration_owner"' in require_owner
    assert enter.index("SET LOCAL ROLE app_security_owner") < enter.index(
        "session_name, current_name = _identity(bind)"
    )
    assert 'current_name != "app_security_owner"' in enter
    assert reset.index("RESET ROLE") < reset.index(
        "_require_migration_owner(bind)"
    )


def test_upgrade_owner_scope_precedes_ddl_and_reset_precedes_postflight() -> None:
    upgrade = _function_source(_source(), "upgrade")
    assert upgrade.count("_enter_security_owner(bind)") == 1
    assert upgrade.count("_reset_role(bind)") == 1
    enter = upgrade.index("_enter_security_owner(bind)")
    claim = upgrade.index("op.execute(_CLAIM_EXPANDED_SQL)")
    finish = upgrade.index("op.execute(_FINISH_EXPANDED_SQL)")
    reset = upgrade.index("_reset_role(bind)")

    assert enter < claim < finish < reset
    for marker in (
        "REVOKE ALL ON FUNCTION app_secure.claim_finance_provider_operation",
        "REVOKE ALL ON FUNCTION app_secure.finish_finance_provider_operation",
        "TO app_runtime",
        "TO finance_payment_runtime",
    ):
        positions = _indices(upgrade, marker)
        assert positions
        assert all(enter < position < reset for position in positions)
    assert len(_indices(upgrade, "_require_expand_baseline(")) == 2
    assert max(_indices(upgrade, "_require_expand_baseline(")) < enter
    assert len(_indices(upgrade, "_require_expanded(")) == 2
    assert reset < min(_indices(upgrade, "_require_expanded("))


def test_downgrade_owner_scope_precedes_ddl_and_reset_precedes_postflight() -> None:
    downgrade = _function_source(_source(), "downgrade")
    assert downgrade.count("_enter_security_owner(bind)") == 1
    assert downgrade.count("_reset_role(bind)") == 1
    enter = downgrade.index("_enter_security_owner(bind)")
    claim = downgrade.index("op.execute(_CLAIM_ORIGINAL_SQL)")
    finish = downgrade.index("op.execute(_FINISH_ORIGINAL_SQL)")
    reset = downgrade.index("_reset_role(bind)")

    assert enter < claim < finish < reset
    for marker in (
        "FROM finance_payment_runtime",
        "TO app_runtime",
        "REVOKE ALL ON FUNCTION app_secure.claim_finance_provider_operation",
        "REVOKE ALL ON FUNCTION app_secure.finish_finance_provider_operation",
    ):
        positions = _indices(downgrade, marker)
        assert positions
        assert all(enter < position < reset for position in positions)
    assert len(_indices(downgrade, "_require_expanded(")) == 2
    assert max(_indices(downgrade, "_require_expanded(")) < enter
    assert len(_indices(downgrade, "_require_expand_baseline(")) == 2
    assert reset < min(_indices(downgrade, "_require_expand_baseline("))


def test_ri1b1_expand_retains_app_and_adds_payment_authority() -> None:
    source = _source()
    upgrade = _function_source(source, "upgrade")
    assert "PAY-8 provider claim requires app_runtime or finance_payment_runtime" in source
    assert (
        "PAY-8 provider completion requires app_runtime or finance_payment_runtime"
        in source
    )
    assert "TO finance_payment_runtime" in upgrade
    assert "TO app_runtime" in upgrade
    assert "FROM app_runtime" not in upgrade
    assert not re.search(
        r"GRANT\s+finance_payment_runtime\s+TO\s+app_runtime\b",
        source,
        re.IGNORECASE,
    )
    assert not re.search(
        r"GRANT\s+app_runtime\s+TO\s+finance_payment_runtime\b",
        source,
        re.IGNORECASE,
    )


def test_ri1b1_preserves_each_security_definer_fence() -> None:
    source = _source()
    for name in FUNCTION_SQL_NAMES:
        sql = _assigned_string(source, name)
        assert sql.lstrip().startswith("CREATE OR REPLACE FUNCTION app_secure.")
        assert sql.count("SECURITY DEFINER") == 1
        assert sql.count("SET search_path=pg_catalog,public,finance") == 1
        assert sql.count("SET row_security=on") == 1


def test_ri1b1_downgrade_restores_legacy_authority() -> None:
    source = _source()
    downgrade = _function_source(source, "downgrade")
    assert "_CLAIM_ORIGINAL_SQL" in downgrade
    assert "_FINISH_ORIGINAL_SQL" in downgrade
    assert "FROM finance_payment_runtime" in downgrade
    assert "TO app_runtime" in downgrade
