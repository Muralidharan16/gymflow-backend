import uuid
from datetime import date, timedelta
from decimal import Decimal
from fastapi import HTTPException
from app.models.subscription import MemberSubscription, MemberFreezeLog
from app.models.payment import Payment, Invoice
from app.models.member import Member
from app.models.enums import (
    SubscriptionStatus, FreezeStatus, MemberStatus,
    PaymentMethod, PaymentType, PaymentStatus, InvoiceType, InvoiceStatus
)
from app.repositories.subscription_repo import SubscriptionRepository, PlanRepository, FreezeLogRepository
from app.repositories.member_repo import MemberRepository
from app.repositories.payment_repo import PaymentRepository, InvoiceRepository
from app.services.invoice_service import InvoiceService
from app.core.redis import redis_client


class SubscriptionService:
    def __init__(
        self,
        sub_repo: SubscriptionRepository,
        plan_repo: PlanRepository,
        freeze_repo: FreezeLogRepository,
        member_repo: MemberRepository,
        payment_repo: PaymentRepository,
        invoice_repo: InvoiceRepository,
        session
    ):
        self.sub_repo = sub_repo
        self.plan_repo = plan_repo
        self.freeze_repo = freeze_repo
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
        staff_id: uuid.UUID
    ) -> MemberSubscription:
        plan = await self.plan_repo.get_by_id(plan_id, gym_id)
        if not plan:
            raise HTTPException(status_code=404, detail="Plan not found")

        member = await self.member_repo.get_by_id(member_id, gym_id)
        if not member:
            raise HTTPException(status_code=404, detail="Member not found")

        end_date = start_date + timedelta(days=plan.duration_days)

        # 1. Create subscription
        sub = MemberSubscription(
            gym_id=gym_id,
            member_id=member_id,
            plan_id=plan_id,
            start_date=start_date,
            end_date=end_date,
            status=SubscriptionStatus.active,
            created_by=staff_id
        )
        sub = await self.sub_repo.create(sub)

        # 2. Create payment
        payment = Payment(
            gym_id=gym_id,
            member_id=member_id,
            subscription_id=sub.id,
            collected_by=staff_id,
            amount=amount_paid,
            discount_amount=Decimal(0),
            payment_method=payment_method,
            payment_type=PaymentType.subscription,
            status=PaymentStatus.completed,
        )
        payment = await self.payment_repo.create(payment)

        # 3. Create invoice using InvoiceService (handles tax logic)
        invoice_service = InvoiceService(self.invoice_repo, self.session)
        await invoice_service.generate_invoice(gym_id, payment, sub, member)

        # 4. Invalidate cache
        await self._invalidate_cache(member)

        return sub

    async def freeze_subscription(self, gym_id: uuid.UUID, sub_id: uuid.UUID, days_requested: int, reason: str, staff_id: uuid.UUID):
        sub = await self.sub_repo.get_by_id(sub_id, gym_id)
        if not sub or sub.status != SubscriptionStatus.active:
            raise HTTPException(400, {"error": "SUBSCRIPTION_NOT_ACTIVE"})

        plan = await self.plan_repo.get_by_id(sub.plan_id, gym_id)
        if sub.total_freeze_days + days_requested > plan.max_freeze_days:
            raise HTTPException(400, {
                "error": "FREEZE_DAYS_EXCEEDED",
                "message": f"Plan allows {plan.max_freeze_days} freeze days. {sub.total_freeze_days} already used."
            })

        sub.status = SubscriptionStatus.frozen
        sub.freeze_start_date = date.today()
        await self.sub_repo.update(sub)

        member = await self.member_repo.get_by_id(sub.member_id, gym_id)
        member.status = MemberStatus.frozen
        await self.member_repo.update(member)

        freeze_log = MemberFreezeLog(
            gym_id=gym_id,
            member_id=sub.member_id,
            subscription_id=sub.id,
            created_by=staff_id,
            freeze_start=date.today(),
            reason=reason,
            status=FreezeStatus.active
        )
        await self.freeze_repo.create(freeze_log)
        await self._invalidate_cache(member)

    async def unfreeze_subscription(self, gym_id: uuid.UUID, sub_id: uuid.UUID, staff_id: uuid.UUID):
        sub = await self.sub_repo.get_by_id(sub_id, gym_id)
        if not sub or sub.status != SubscriptionStatus.frozen:
            raise HTTPException(400, {"error": "SUBSCRIPTION_NOT_FROZEN"})

        actual_days = (date.today() - sub.freeze_start_date).days
        sub.end_date = sub.end_date + timedelta(days=actual_days)
        sub.status = SubscriptionStatus.active
        sub.freeze_end_date = date.today()
        sub.total_freeze_days += actual_days
        await self.sub_repo.update(sub)

        member = await self.member_repo.get_by_id(sub.member_id, gym_id)
        member.status = MemberStatus.active
        await self.member_repo.update(member)

        freeze_log = await self.freeze_repo.get_active_for_subscription(sub.id)
        if freeze_log:
            freeze_log.status = FreezeStatus.completed
            freeze_log.freeze_end = date.today()
            await self.freeze_repo.update(freeze_log)

        await self._invalidate_cache(member)

    async def cancel_subscription(self, gym_id: uuid.UUID, sub_id: uuid.UUID, reason: str, staff_id: uuid.UUID):
        sub = await self.sub_repo.get_by_id(sub_id, gym_id)
        if not sub or sub.status not in (SubscriptionStatus.active, SubscriptionStatus.frozen):
            raise HTTPException(400, {"error": "SUBSCRIPTION_NOT_ACTIVE_OR_FROZEN"})

        sub.status = SubscriptionStatus.cancelled
        sub.cancelled_at = date.today()
        sub.cancellation_reason = reason
        await self.sub_repo.update(sub)

        member = await self.member_repo.get_by_id(sub.member_id, gym_id)
        member.status = MemberStatus.inactive
        await self.member_repo.update(member)
        await self._invalidate_cache(member)

    async def _invalidate_cache(self, member: Member):
        if member.qr_token:
            await redis_client.delete(f"{member.qr_token}:access")
        if member.member_uid:
            await redis_client.delete(f"{member.member_uid}:access")
        if member.fingerprint_id:
            await redis_client.delete(f"{member.fingerprint_id}:access")
