from __future__ import annotations

import ast
from pathlib import Path


MIGRATION = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "c6d7e8f9a0b1_harden_member_subscription_runtime_boundary.py"


def _literal_sql() -> list[str]:
    tree = ast.parse(MIGRATION.read_text())
    statements: list[str] = []
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call) or not call.args:
            continue
        if not isinstance(call.func, ast.Attribute) or call.func.attr != "execute":
            continue
        arg = call.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            statements.append(arg.value)
        elif isinstance(arg, ast.JoinedStr):
            statements.append(
                "".join(
                    value.value
                    for value in arg.values
                    if isinstance(value, ast.Constant) and isinstance(value.value, str)
                )
            )
    return statements


def test_member_domain_runtime_migration_is_role_and_owner_inert() -> None:
    source = MIGRATION.read_text()
    sql = "\n".join(_literal_sql()).upper()

    assert 'REVISION = "C6D7E8F9A0B1"' in source.upper()
    assert 'DOWN_REVISION = "B5C6D7E8F9A0"' in source.upper()
    assert "CREATE ROLE" not in sql
    assert "ALTER ROLE" not in sql
    assert "BYPASSRLS" not in sql
    assert "OWNER TO" not in sql
    assert "GRANT DELETE" not in sql
    assert "GRANT TRUNCATE" not in sql
    assert "GRANT UPDATE ON TABLE" not in sql


def test_member_domain_runtime_migration_forces_rls_on_exact_domain_tables() -> None:
    source = MIGRATION.read_text()
    expected = {
        "members",
        "membership_plans",
        "organization_counters",
        "member_subscriptions_v2",
        "subscription_members",
        "member_measurements",
    }

    tree = ast.parse(source)
    assignments = {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id in {"_DIRECT_TENANT_TABLES", "_MEASUREMENTS"}
    }
    observed = set(assignments["_DIRECT_TENANT_TABLES"]) | {assignments["_MEASUREMENTS"]}
    assert observed == expected

    assert "ENABLE ROW LEVEL SECURITY" in source
    assert "FORCE ROW LEVEL SECURITY" in source
    assert "app.current_org_id" in source


def test_member_and_plan_updates_are_column_scoped() -> None:
    source = MIGRATION.read_text()
    tree = ast.parse(source)
    values: dict[str, set[str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if target.id in {
            "_MEMBER_UPDATE_COLUMNS",
            "_PLAN_UPDATE_COLUMNS",
            "_COUNTER_INSERT_COLUMNS",
            "_COUNTER_SELECT_COLUMNS",
            "_COUNTER_UPDATE_COLUMNS",
        }:
            values[target.id] = set(ast.literal_eval(node.value))

    assert values["_MEMBER_UPDATE_COLUMNS"] == {
        "address",
        "blood_group",
        "date_of_birth",
        "email",
        "emergency_contact_name",
        "emergency_contact_phone",
        "gender",
        "home_branch_id",
        "is_active",
        "name",
        "notes",
        "phone",
        "status",
        "updated_at",
        "updated_by",
    }
    assert {"org_id", "member_uid", "member_number", "is_migrated", "migrated_source"}.isdisjoint(
        values["_MEMBER_UPDATE_COLUMNS"]
    )

    assert values["_PLAN_UPDATE_COLUMNS"] == {
        "archived_at",
        "description",
        "duration_unit",
        "duration_value",
        "max_members",
        "name",
        "price",
        "status",
        "updated_at",
        "valid_from",
        "valid_until",
    }
    assert {"org_id", "branch_id", "plan_code", "currency", "created_by"}.isdisjoint(
        values["_PLAN_UPDATE_COLUMNS"]
    )

    assert values["_COUNTER_INSERT_COLUMNS"] == {"id", "org_id", "counter_key", "current_value"}
    assert values["_COUNTER_SELECT_COLUMNS"] == {"org_id", "counter_key", "current_value"}
    assert values["_COUNTER_UPDATE_COLUMNS"] == {"current_value", "updated_at"}


def test_modern_subscription_rows_are_read_create_only() -> None:
    source = MIGRATION.read_text()
    assert '"member_subscriptions_v2": ("SELECT", "INSERT")' in source
    assert '"subscription_members": ("SELECT", "INSERT")' in source
    assert '"member_measurements": ("SELECT", "INSERT")' in source


def test_measurement_policy_derives_tenant_from_parent_member_and_gym() -> None:
    source = MIGRATION.read_text()
    assert "public.members AS tenant_member" in source
    assert "tenant_member.id = member_measurements.member_id" in source
    assert "tenant_member.gym_id = member_measurements.gym_id" in source
    assert "tenant_member.org_id" in source
    assert "app.current_org_id" in source
