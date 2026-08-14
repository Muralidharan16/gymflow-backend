from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INITIAL = ROOT / "alembic/versions/aa9303384b66_initial_schema.py"

_EXPECTED_ENUMS = {
    "orgtier": ("basic", "pro", "elite"),
    "staffrole": ("owner", "admin", "trainer", "receptionist"),
    "importstatus": ("processing", "completed", "failed"),
    "memberstatus": ("active", "inactive", "frozen", "expired", "blocked"),
    "checkinmethod": ("qr", "fingerprint", "manual", "rfid", "face", "door_lock"),
    "attendancedenialreason": (
        "subscription_expired",
        "no_active_subscription",
        "account_frozen",
        "not_found",
    ),
    "subscriptionstatus": ("active", "expired", "frozen", "cancelled", "pending"),
    "freezestatus": ("requested", "active", "completed", "cancelled"),
    "paymentmethod": ("cash", "upi", "card", "bank_transfer", "cheque", "online"),
    "paymenttype": ("subscription", "registration", "addon", "penalty", "refund"),
    "paymentstatus": ("pending", "completed", "failed", "refunded"),
    "invoicetype": ("bill_of_supply", "tax_invoice"),
    "invoicestatus": ("draft", "issued", "paid", "void"),
}


def _source() -> str:
    return INITIAL.read_text(encoding="utf-8")


def _tree() -> ast.Module:
    return ast.parse(_source(), filename=str(INITIAL))


def _function_source(name: str) -> str:
    source = _source()
    node = next(
        item for item in _tree().body
        if isinstance(item, ast.FunctionDef) and item.name == name
    )
    lines = source.splitlines()
    return "\n".join(lines[node.lineno - 1 : node.end_lineno])


def _literal_initial_enum_map() -> dict[str, tuple[str, ...]]:
    assignment = next(
        item for item in _tree().body
        if isinstance(item, ast.AnnAssign)
        and isinstance(item.target, ast.Name)
        and item.target.id == "_INITIAL_ENUMS"
    )
    value = ast.literal_eval(assignment.value)
    return {str(name): tuple(labels) for name, labels in value.items()}


def test_initial_enum_inventory_is_complete_and_exact() -> None:
    assert _literal_initial_enum_map() == _EXPECTED_ENUMS

    source = _source()
    for type_name, labels in _EXPECTED_ENUMS.items():
        assert f"name='{type_name}'" in source
        for label in labels:
            assert repr(label) in source


def test_initial_enum_downgrade_validates_owner_kind_and_ordered_labels() -> None:
    helper = _function_source("_require_initial_enum_contract")
    query_helper = _function_source("_initial_enum_rows")

    assert "owner_name'] != 'migration_owner'" in helper
    assert "type_kind'] != 'e'" in helper
    assert "actual_labels != expected_labels" in helper
    assert "ORDER BY enum_data.enumsortorder" in query_helper
    assert "namespace_data.nspname = 'public'" in query_helper


def test_initial_enum_downgrade_is_explicit_and_fail_closed() -> None:
    downgrade = _function_source("downgrade")

    assert "_require_initial_enum_contract(bind)" in downgrade
    assert "_require_initial_enums_absent(bind)" in downgrade
    assert "DROP TYPE public.{type_name};" in downgrade
    assert "CASCADE" not in "\n".join(
        line for line in downgrade.splitlines()
        if not line.lstrip().startswith("#")
    ).upper()
    assert "IF EXISTS" not in "\n".join(
        line for line in downgrade.splitlines()
        if not line.lstrip().startswith("#")
    ).upper()
