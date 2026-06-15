from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.subscription_lifecycle import (
    LifecycleV2Projection,
    SeriesSummary,
    SlotSummary,
    SubscriptionOperationalStatus,
    SubscriptionSeriesStatus,
    TermSummary,
    TimelineItem,
)
from app.repositories.subscription_lifecycle_repo import SubscriptionLifecycleRepository


class SubscriptionLifecycleQueryService:
    def __init__(self, db: AsyncSession):
        self.repo = SubscriptionLifecycleRepository(db)

    async def list_current_series(
        self,
        org_id: uuid.UUID,
        *,
        business_date: date | None = None,
        branch_id: uuid.UUID | None = None,
        search: str | None = None,
        primary_member_id: uuid.UUID | None = None,
        plan_id: uuid.UUID | None = None,
        operational_status: SubscriptionOperationalStatus | None = None,
        lifecycle_status: SubscriptionSeriesStatus | None = None,
        has_scheduled_renewal: bool | None = None,
        has_vacant_slots: bool | None = None,
        include_archived: bool = False,
        page: int = 1,
        size: int = 25,
    ) -> tuple[list[SeriesSummary], int]:
        return await self.repo.list_series_summaries(
            org_id,
            business_date=business_date or datetime.now(timezone.utc).date(),
            branch_id=branch_id,
            search=search,
            primary_member_id=primary_member_id,
            plan_id=plan_id,
            operational_status=operational_status,
            lifecycle_status=lifecycle_status,
            has_scheduled_renewal=has_scheduled_renewal,
            has_vacant_slots=has_vacant_slots,
            include_archived=include_archived,
            page=page,
            size=size,
        )

    async def get_series_detail(
        self,
        org_id: uuid.UUID,
        series_id: uuid.UUID,
        *,
        business_date: date | None = None,
    ) -> SeriesSummary:
        return await self.repo.get_series_detail(
            org_id,
            series_id,
            business_date=business_date or datetime.now(timezone.utc).date(),
        )

    async def list_upcoming_terms(
        self,
        org_id: uuid.UUID,
        *,
        business_date: date | None = None,
        branch_id: uuid.UUID | None = None,
        page: int = 1,
        size: int = 25,
    ) -> tuple[list[TermSummary], int]:
        return await self.repo.list_upcoming_terms(
            org_id,
            business_date=business_date or datetime.now(timezone.utc).date(),
            branch_id=branch_id,
            page=page,
            size=size,
        )

    async def list_history_terms(
        self,
        org_id: uuid.UUID,
        *,
        business_date: date | None = None,
        series_id: uuid.UUID | None = None,
        member_id: uuid.UUID | None = None,
        branch_id: uuid.UUID | None = None,
        plan_id: uuid.UUID | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        page: int = 1,
        size: int = 25,
    ) -> tuple[list[TermSummary], int]:
        return await self.repo.list_history_terms(
            org_id,
            business_date=business_date or datetime.now(timezone.utc).date(),
            series_id=series_id,
            member_id=member_id,
            branch_id=branch_id,
            plan_id=plan_id,
            from_date=from_date,
            to_date=to_date,
            page=page,
            size=size,
        )

    async def list_archived_series(
        self,
        org_id: uuid.UUID,
        *,
        business_date: date | None = None,
        page: int = 1,
        size: int = 25,
    ) -> tuple[list[SeriesSummary], int]:
        return await self.repo.list_archived_series(
            org_id,
            business_date=business_date or datetime.now(timezone.utc).date(),
            page=page,
            size=size,
        )

    async def list_slots(
        self,
        org_id: uuid.UUID,
        term_id: uuid.UUID,
        *,
        business_date: date | None = None,
    ) -> list[SlotSummary]:
        return await self.repo.list_slots(
            org_id,
            term_id,
            business_date=business_date or datetime.now(timezone.utc).date(),
        )

    async def list_timeline(
        self,
        org_id: uuid.UUID,
        series_id: uuid.UUID,
        *,
        page: int = 1,
        size: int = 50,
    ) -> tuple[list[TimelineItem], int]:
        return await self.repo.list_timeline(org_id, series_id, page=page, size=size)

    async def get_v2_projection(
        self,
        org_id: uuid.UUID,
        series_id: uuid.UUID,
        *,
        business_date: date | None = None,
    ) -> LifecycleV2Projection | None:
        return await self.repo.get_v2_projection(
            org_id,
            series_id,
            business_date=business_date or datetime.now(timezone.utc).date(),
        )
