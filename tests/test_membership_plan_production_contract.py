from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import CheckConstraint, ForeignKeyConstraint

from app.models.membership_plan import MembershipPlan
from app.routers.membership_plans import _validate_effective_validity_window
from app.schemas.membership_plan import MembershipPlanCreate, MembershipPlanUpdate


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    ROOT
    / "alembic"
    / "versions"
    / "7c2f91e4ab63_harden_membership_plan_invariants.py"
)


def _create_payload(**overrides):
    payload = {
        "name": "Production Plan",
        "price": "19.99",
        "duration_value": 1,
        "duration_unit": "months",
    }
    payload.update(overrides)
    return payload


def test_membership_plan_money_is_decimal_and_scale_bounded() -> None:
    plan = MembershipPlanCreate(**_create_payload())
    assert plan.price == Decimal("19.99")
    assert isinstance(plan.price, Decimal)

    largest = MembershipPlanCreate(
        **_create_payload(price="9999999999.99")
    )
    assert largest.price == Decimal("9999999999.99")

    with pytest.raises(ValidationError):
        MembershipPlanCreate(**_create_payload(price="19.999"))
    with pytest.raises(ValidationError):
        MembershipPlanCreate(**_create_payload(price="10000000000.00"))

    update = MembershipPlanUpdate(price="0.01")
    assert update.price == Decimal("0.01")
    with pytest.raises(ValidationError):
        MembershipPlanUpdate(price="0.001")


def test_membership_plan_validity_requires_timezone_and_forward_window() -> None:
    with pytest.raises(ValidationError):
        MembershipPlanCreate(
            **_create_payload(
                valid_from="2026-08-12T09:00:00",
                valid_until="2026-08-12T10:00:00Z",
            )
        )

    with pytest.raises(ValidationError):
        MembershipPlanUpdate(valid_until=datetime(2026, 8, 12, 10, 0, 0))

    valid = MembershipPlanCreate(
        **_create_payload(
            valid_from="2026-08-12T09:00:00Z",
            valid_until="2026-08-12T10:00:00Z",
        )
    )
    assert valid.valid_from is not None
    assert valid.valid_from.utcoffset() is not None

    with pytest.raises(ValidationError):
        MembershipPlanCreate(
            **_create_payload(
                valid_from="2026-08-12T10:00:00Z",
                valid_until="2026-08-12T10:00:00Z",
            )
        )


def test_partial_update_validity_uses_persisted_counterpart() -> None:
    persisted_from = datetime(2026, 8, 12, 10, 0, tzinfo=timezone.utc)
    persisted_until = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)

    _validate_effective_validity_window(persisted_from, persisted_until)

    with pytest.raises(HTTPException) as exc_info:
        _validate_effective_validity_window(
            persisted_from,
            datetime(2026, 8, 12, 9, 0, tzinfo=timezone.utc),
        )
    assert exc_info.value.status_code == 400

    with pytest.raises(HTTPException):
        _validate_effective_validity_window(
            datetime(2026, 8, 12, 13, 0, tzinfo=timezone.utc),
            persisted_until,
        )


def test_membership_plan_orm_declares_database_authority_constraints() -> None:
    constraints = {
        constraint.name: constraint
        for constraint in MembershipPlan.__table__.constraints
        if constraint.name is not None
    }

    validity = constraints["ck_membership_plans_valid_window"]
    assert isinstance(validity, CheckConstraint)
    validity_sql = str(validity.sqltext).lower()
    assert "valid_from is null" in validity_sql
    assert "valid_until is null" in validity_sql
    assert "valid_until > valid_from" in validity_sql

    tenant_fk = constraints["fk_membership_plans_branch_tenant"]
    assert isinstance(tenant_fk, ForeignKeyConstraint)
    assert tenant_fk.ondelete == "CASCADE"
    assert [element.parent.name for element in tenant_fk.elements] == [
        "branch_id",
        "org_id",
    ]
    assert [element.target_fullname for element in tenant_fk.elements] == [
        "org_branches.id",
        "org_branches.org_id",
    ]


def test_membership_plan_hardening_migration_is_append_only_and_bounded() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'revision = "7c2f91e4ab63"' in source
    assert 'down_revision = "f9a0b1c2d3e4"' in source
    assert source.count("NOT VALID") >= 2
    assert (
        "VALIDATE CONSTRAINT ck_membership_plans_valid_window" in source
    )
    assert (
        "VALIDATE CONSTRAINT fk_membership_plans_branch_tenant" in source
    )
    assert "FOREIGN KEY (branch_id, org_id)" in source
    assert "REFERENCES public.org_branches (id, org_id)" in source
    assert "ON DELETE CASCADE" in source
    assert "SET ROLE" not in source
    assert "GRANT " not in source
    assert "REVOKE " not in source
    assert "UPDATE public.membership_plans" not in source
    assert "DELETE FROM public.membership_plans" not in source
    assert "TRUNCATE" not in source
