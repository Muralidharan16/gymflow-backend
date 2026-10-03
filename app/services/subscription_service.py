import uuid
import logging
from datetime import date, timedelta
from decimal import Decimal
from fastapi import HTTPException, status
from app.models.subscription import MemberSubscription, MemberFreezeLog
from app.models.payment import Payment, Invoice
from app.models.member import Member
from app.models.enums import (
    SubscriptionStatus, FreezeStatus, MemberStatus,
    PaymentMethod, PaymentType, PaymentStatus
)
from app.repositories.subscription_repo import SubscriptionRepository
from app.repositories.member_repo import MemberRepository
from app.repositories.payment_repo import PaymentRepository, InvoiceRepository
from app.services.invoice_service import InvoiceService
from app.core.redis import redis_client

logger = logging.getLogger(__name__)

class SubscriptionService:
    def __init__(
        self,
        sub_repo: SubscriptionRepository,
        member_repo: MemberRepository,
        payment_repo: PaymentRepository,
        invoice_repo: InvoiceRepository,
        session
    ):
        self.sub_repo = sub_repo
        self.member_repo = member_repo
        self.payment_repo = payment_repo
        self.invoice_repo = invoice_repo
        self.session = session

    async def assign_plan(
        self,
        gym_id: uuid.UUID,
        member_id: uuid.UUID,
        plan_id: uuid.UUID,
        start_date: date,
        amount_paid: Decimal,
        payment_method: PaymentMethod,
        staff_id: uuid.UUID,
    ) -> MemberSubscription:
        """PAY-15: legacy subscription + payment/invoice write path is retired."""
        del (
            gym_id,
            member_id,
            plan_id,
            start_date,
            amount_paid,
            payment_method,
            staff_id,
        )
        raise RuntimeError(
            "PAY-15 legacy subscription payment writes are retired; "
            "use modern subscriptions with Finance Core"
        )

    async def freeze_subscription(
        self,
        gym_id: uuid.UUID,
        sub_id: uuid.UUID,
        days_requested: int,
        reason: str,
        staff_id: uuid.UUID,
    ) -> None:
        del gym_id, sub_id, days_requested, reason, staff_id
        raise RuntimeError(
            "PAY-24-C legacy entitlement mutation is retired; "
            "use the canonical entitlement command authority"
        )

    async def unfreeze_subscription(
        self,
        gym_id: uuid.UUID,
        sub_id: uuid.UUID,
        staff_id: uuid.UUID,
    ) -> None:
        del gym_id, sub_id, staff_id
        raise RuntimeError(
            "PAY-24-C legacy entitlement mutation is retired; "
            "use the canonical entitlement command authority"
        )

    async def cancel_subscription(
        self,
        gym_id: uuid.UUID,
        sub_id: uuid.UUID,
        reason: str,
        staff_id: uuid.UUID,
    ) -> None:
        del gym_id, sub_id, reason, staff_id
        raise RuntimeError(
            "PAY-24-C legacy entitlement mutation is retired; "
            "use the canonical entitlement command authority"
        )

    async def _invalidate_cache(self, member: Member):
        """Invalidate all access tokens in Redis."""
        try:
            tokens = []
            if member.qr_token:
                tokens.append(f"{member.qr_token}:access")
            if member.member_uid:
                tokens.append(f"{member.member_uid}:access")
            if member.fingerprint_id:
                tokens.append(f"{member.fingerprint_id}:access")
            
            if tokens:
                await redis_client.delete(*tokens)
        except Exception as e:
            logger.error(f"Failed to invalidate cache for member {member.id}: {e}")
