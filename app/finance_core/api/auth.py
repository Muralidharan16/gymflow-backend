from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from fastapi import Depends, HTTPException, Header, status

from app.core.deps import Staff, get_current_active_staff


class FinancePaymentActorKind(str, Enum):
    TENANT_ADMIN = "tenant_admin"
    FINANCE_ADMIN = "finance_admin"
    INTERNAL_SYSTEM = "internal_system"
    CUSTOMER = "customer"
    WEBHOOK = "webhook"


@dataclass(frozen=True)
class FinancePaymentActor:
    kind: FinancePaymentActorKind
    organization_id: uuid.UUID | None
    facility_id: uuid.UUID | None = None
    staff_id: uuid.UUID | None = None
    role: str | None = None
    branch_ids: tuple[str, ...] = ()

    @classmethod
    def from_staff(cls, staff: Staff) -> "FinancePaymentActor":
        role = (staff.role or "").lower()
        if role in {"owner", "admin"} and staff.gym_id is None:
            kind = FinancePaymentActorKind.TENANT_ADMIN
        elif role in {"finance_admin", "superadmin", "compliance"} and staff.gym_id is None:
            kind = FinancePaymentActorKind.FINANCE_ADMIN
        else:
            kind = FinancePaymentActorKind.CUSTOMER

        return cls(
            kind=kind,
            organization_id=staff.org_id,
            facility_id=staff.gym_id,
            staff_id=staff.id,
            role=staff.role,
            branch_ids=tuple(staff.branch_ids or ()),
        )


def internal_system_actor(system_id: str = "finance_core") -> FinancePaymentActor:
    return FinancePaymentActor(
        kind=FinancePaymentActorKind.INTERNAL_SYSTEM,
        organization_id=None,
        role=system_id,
    )


def webhook_actor(provider_code: str = "razorpay") -> FinancePaymentActor:
    return FinancePaymentActor(
        kind=FinancePaymentActorKind.WEBHOOK,
        organization_id=None,
        role=provider_code,
    )


def _deny(code: str, message: str, http_status: int = status.HTTP_403_FORBIDDEN) -> None:
    raise HTTPException(
        status_code=http_status,
        detail={"code": code, "message": message},
    )


def _require_kind(actor: FinancePaymentActor, allowed: Iterable[FinancePaymentActorKind]) -> FinancePaymentActor:
    allowed_set = set(allowed)
    if actor.kind not in allowed_set:
        _deny("FINANCE_PAYMENT_FORBIDDEN", "Finance payment route access denied.")
    return actor


def require_checkout_actor(actor: FinancePaymentActor) -> FinancePaymentActor:
    return _require_kind(actor, {FinancePaymentActorKind.TENANT_ADMIN})


def require_checkout_status_actor(actor: FinancePaymentActor) -> FinancePaymentActor:
    return _require_kind(actor, {FinancePaymentActorKind.TENANT_ADMIN, FinancePaymentActorKind.FINANCE_ADMIN})


def require_finance_admin_actor(actor: FinancePaymentActor) -> FinancePaymentActor:
    return _require_kind(actor, {FinancePaymentActorKind.FINANCE_ADMIN})


def require_internal_payment_application_actor(actor: FinancePaymentActor) -> FinancePaymentActor:
    return _require_kind(actor, {FinancePaymentActorKind.INTERNAL_SYSTEM})


def require_webhook_signature_actor(signature: str | None) -> FinancePaymentActor:
    if not signature:
        _deny(
            "FINANCE_WEBHOOK_SIGNATURE_REQUIRED",
            "Finance webhook signature is required.",
            http_status=status.HTTP_401_UNAUTHORIZED,
        )
    return webhook_actor("razorpay")


def require_same_tenant(
    actor: FinancePaymentActor,
    *,
    organization_id: uuid.UUID,
    facility_id: uuid.UUID | None = None,
) -> FinancePaymentActor:
    if actor.organization_id != organization_id:
        _deny("FINANCE_PAYMENT_TENANT_MISMATCH", "Finance payment resource not found.", status.HTTP_404_NOT_FOUND)

    if actor.facility_id is not None and facility_id is not None and actor.facility_id != facility_id:
        _deny("FINANCE_PAYMENT_TENANT_MISMATCH", "Finance payment resource not found.", status.HTTP_404_NOT_FOUND)

    return actor


def get_finance_payment_actor(staff: Staff = Depends(get_current_active_staff)) -> FinancePaymentActor:
    return FinancePaymentActor.from_staff(staff)


def checkout_actor_dependency(
    actor: FinancePaymentActor = Depends(get_finance_payment_actor),
) -> FinancePaymentActor:
    return require_checkout_actor(actor)


def checkout_status_actor_dependency(
    actor: FinancePaymentActor = Depends(get_finance_payment_actor),
) -> FinancePaymentActor:
    return require_checkout_status_actor(actor)


def finance_admin_actor_dependency(
    actor: FinancePaymentActor = Depends(get_finance_payment_actor),
) -> FinancePaymentActor:
    return require_finance_admin_actor(actor)


def internal_payment_application_actor_dependency(
    x_finance_internal_actor: str | None = Header(default=None, alias="X-Finance-Internal-Actor"),
) -> FinancePaymentActor:
    # A later enablement phase may bind this to signed internal service identity.
    # Phase 6I records the contract only; disabled routes still reject first.
    if x_finance_internal_actor != "finance_core":
        _deny("FINANCE_INTERNAL_ACTOR_REQUIRED", "Finance internal actor is required.", status.HTTP_401_UNAUTHORIZED)
    return internal_system_actor(x_finance_internal_actor)


def webhook_actor_dependency(
    x_razorpay_signature: str | None = Header(default=None, alias="X-Razorpay-Signature"),
) -> FinancePaymentActor:
    return require_webhook_signature_actor(x_razorpay_signature)
